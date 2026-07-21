import os
import re
import asyncio
import time
import requests
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, Message,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent
)
from pyrogram.errors import FloodWait, ChannelPrivate, MessageIdInvalid


# ── Override MIN_CHANNEL_ID to accept modern large channel IDs ──
# Older Pyrogram versions (pre-2.1) have MIN_CHANNEL_ID = -1002147483647
# which rejects channel IDs more negative than that threshold.
# Modern Telegram channels often exceed this limit.
import pyrogram.utils
if pyrogram.utils.MIN_CHANNEL_ID > -100999999999999:
    pyrogram.utils.MIN_CHANNEL_ID = -100999999999999


# ── Monkey-patch Pyrogram's handle_updates to catch ValueError on unresolvable peers ──
_original_handle_updates = Client.handle_updates


async def _safe_handle_updates(self, updates):
    try:
        await _original_handle_updates(self, updates)
    except ValueError as e:
        if "Peer id invalid" in str(e):
            print(f"⚠️ [update] Skipped update for unresolvable peer: {e}")
        else:
            raise


Client.handle_updates = _safe_handle_updates


API_ID_RAW = os.environ.get("API_ID", "").strip()
API_ID = int(API_ID_RAW) if API_ID_RAW.isdigit() else 0
API_HASH = os.environ.get("API_HASH", "").strip()
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
WORKER_URL = os.environ.get("WORKER_URL", "https://telegram-streamer-addon.nishokroshan076632.workers.dev").rstrip("/")
ALLOWED_CHANNELS = [
    c.strip()
    for c in os.environ.get("ALLOWED_CHANNELS", "-1003916531716,-1002502061360,-1003967652604,-1002708448330").split(",")
    if c.strip()
]

app = FastAPI(title="Telegram Streamer MTProto Engine", version="4.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Pyrogram get_file is not safe under concurrent streams on one client —
# interleaved chunks blow past Content-Length ("Too much data for declared Content-Length").
DOWNLOAD_SEM = asyncio.Semaphore(1)
CHUNK_SIZE = 1024 * 1024

# Use SESSION_STRING (user account) if available, else fall back to BOT_TOKEN
tg_client = None
if SESSION_STRING and API_ID and API_HASH:
    print("✅ Using USER SESSION (full channel access, no admin required)")
    tg_client = Client(
        "tg_user_session",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True,
    )
elif BOT_TOKEN and API_ID and API_HASH:
    print("⚠️  Using BOT TOKEN (bot must be admin in each channel)")
    tg_client = Client(
        "tg_bot_session",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )
else:
    print("❌ No valid credentials found. Set SESSION_STRING (preferred) or BOT_TOKEN.")


# ── Content Parsing ────────────────────────────────────────────────────────────

SERIES_PATTERN = re.compile(r"(.*)[.\s]S(\d{1,2})E(\d{1,2})", re.IGNORECASE)
YEAR_PATTERN = re.compile(r"(19\d{2}|20\d{2})")
SOURCE_PATTERNS = {
    "WEB-DL": r"web[.-]?dl|webrip",
    "BluRay": r"bluray|blu[.\s]ray|bdrip",
    "HDRip": r"hdrip|hd[.\s]rip",
    "DVDRip": r"dvdrip|dvd[.\s]rip",
    "DVDScr": r"dvdscr|dvdscreener",
    "Cam": r"camrip|cam[.\s]rip|hdcam",
    "TeleSync": r"telesync|tsrip|hdts",
    "PPV": r"ppvrip|ppv",
    "WEBRip": r"webrip",
    "HDRemux": r"hdremux|remux",
}


def parse_media_info(file_name: str, file_size: int) -> dict:
    n = file_name.lower()
    base = os.path.splitext(file_name)[0]

    # Quality
    quality = (
        "4K UHD" if ("2160p" in n or "4k" in n) else
        "1080p FHD" if "1080p" in n else
        "720p HD" if "720p" in n else
        "480p SD" if "480p" in n else "Unknown"
    )

    # Codec
    codec = "HEVC" if ("hevc" in n or "x265" in n or "h265" in n) else "H.264"

    # Audio
    audio = "Multi-Audio" if ("dual" in n or "multi" in n or "hindi" in n or "tamil" in n) else "Standard"

    # Size
    size_mb = round(file_size / (1024 * 1024), 2)
    size_str = f"{size_mb} MB" if size_mb < 1024 else f"{round(size_mb / 1024, 2)} GB"

    # Year
    years = YEAR_PATTERN.findall(base)
    year = int(years[-1]) if years else None

    # Source type
    source = "Unknown"
    for src, pattern in SOURCE_PATTERNS.items():
        if re.search(pattern, n):
            source = src
            break

    # Series detection
    series_match = SERIES_PATTERN.search(base)
    media_type = "series" if series_match else "movie"

    # Title (remove year, source, quality, etc for clean name)
    title = base
    if series_match:
        title = series_match.group(1).replace(".", " ").replace("_", " ").strip()

    # Release group (often in [...] or -GROUP suffix before extension)
    group_match = re.search(r"[\[\{\(]([^\]\}\)]+)[\]\}\)]\s*$", base)
    release_group = group_match.group(1) if group_match else None

    info = {
        "quality": quality,
        "codec": codec,
        "audio": audio,
        "size": size_str,
        "file_name": file_name,
        "year": year,
        "source": source,
        "media_type": media_type,
        "release_group": release_group,
        "title": title,
    }

    if series_match:
        season = int(series_match.group(2))
        episode = int(series_match.group(3))
        info["season"] = season
        info["episode"] = episode
        e_title = base[series_match.end():].replace(".", " ").replace("_", " ").strip()
        info["episode_title"] = e_title if e_title else None

    return info


# ── TMDB Metadata Cache ───────────────────────────────────────────────────────

TMDB_CACHE = {}  # key -> (timestamp, data)
TMDB_CACHE_TTL = 86400  # 24 hours

# ── Search Result Cache ────────────────────────────────────────────────────────

SEARCH_CACHE = {}  # (query, channel_id) -> (timestamp, data)
SEARCH_CACHE_TTL = 60  # 60 seconds
CACHE_HIT, CACHE_MISS = 0, 0  # stats


async def tmdb_search(query: str, year: int = None, media_type: str = "movie"):
    """Search TMDB and return first match metadata. Returns None if no key or no match."""
    if not TMDB_API_KEY:
        return None
    cache_key = f"{media_type}:{query.lower().strip()}:{year}"
    now = time.time()
    if cache_key in TMDB_CACHE:
        ts, data = TMDB_CACHE[cache_key]
        if now - ts < TMDB_CACHE_TTL:
            return data
    try:
        url = f"https://api.themoviedb.org/3/search/{media_type}?api_key={TMDB_API_KEY}&query={query}"
        if year:
            url += f"&year={year}"
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        if not results:
            # Try without year
            url_no_year = f"https://api.themoviedb.org/3/search/{media_type}?api_key={TMDB_API_KEY}&query={query}"
            resp2 = requests.get(url_no_year, timeout=5)
            if resp2.status_code == 200:
                results = resp2.json().get("results", [])
            if not results:
                return None
        best = results[0]
        data = {
            "tmdb_id": best["id"],
            "title": best.get("title") or best.get("name", ""),
            "overview": best.get("overview", "")[:500],
            "poster": f"https://image.tmdb.org/t/p/w500{best['poster_path']}" if best.get("poster_path") else None,
            "backdrop": f"https://image.tmdb.org/t/p/w1280{best['backdrop_path']}" if best.get("backdrop_path") else None,
            "vote_average": best.get("vote_average"),
            "release_date": best.get("release_date") or best.get("first_air_date", ""),
            "media_type": media_type,
        }
        TMDB_CACHE[cache_key] = (now, data)
        return data
    except Exception as e:
        print(f"TMDB error: {e}")
        return None


# ── Build Rich Metadata ───────────────────────────────────────────────────────

async def enrich_media_info(info: dict):
    """Add TMDB metadata to parsed media info."""
    if not TMDB_API_KEY:
        return info
    query = info.get("title") or info.get("file_name", "")
    # Map internal media_type to TMDB API endpoint names
    raw_mt = info.get("media_type", "movie")
    tmdb_mt = "tv" if raw_mt == "series" else "movie"
    year = info.get("year")
    tmdb = await tmdb_search(query, year, tmdb_mt)
    if tmdb:
        info["tmdb"] = tmdb
        info["tmdb"]["media_type"] = raw_mt  # Preserve internal type
    return info




# ── BOT COMMAND HANDLERS ──────────────────────────────────────────────────────

if tg_client:
    @tg_client.on_message(filters.command("start"))
    async def cmd_start(client: Client, msg: Message):
        await msg.reply_text(
            "🎬 **Telegram Movie Streamer Bot**\n\n"
            "Commands:\n"
            "• `/search <title>` — search all channels\n"
            "• `/channels` — list source channels\n"
            "• `/help` — usage guide",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🌐 Stremio Addon", url=f"{WORKER_URL}/manifest.json"),
                InlineKeyboardButton("🔍 Search", switch_inline_query_current_chat=""),
            ]]),
            disable_web_page_preview=True,
        )

    @tg_client.on_message(filters.command("help"))
    async def cmd_help(client: Client, msg: Message):
        await msg.reply_text(
            "📖 **How to use:**\n\n"
            "1. `/search Avengers` — search all source channels\n"
            "2. Forward any video from a source channel here to get a stream link\n"
            "3. Inline: type `@botname movie title` anywhere in Telegram\n"
            "4. Install Stremio Addon via the /start menu"
        )

    @tg_client.on_message(filters.command("channels"))
    async def cmd_channels(client: Client, msg: Message):
        lines = "\n".join(f"• `{ch}`" for ch in ALLOWED_CHANNELS)
        await msg.reply_text(f"📢 **Source Channels:**\n\n{lines}")

    @tg_client.on_message(filters.command("search"))
    async def cmd_search(client: Client, msg: Message):
        query = " ".join(msg.command[1:]).strip()
        if not query:
            await msg.reply_text("Usage: `/search <movie title>`")
            return

        status = await msg.reply_text(f"🔍 Searching for **{query}**…")
        found = 0

        for ch in ALLOWED_CHANNELS:
            try:
                chat_id = int(ch)
                async for m in client.search_messages(chat_id, query=query, limit=10):
                    media = m.video or m.document or m.audio
                    if not media:
                        continue
                    found += 1
                    fname = getattr(media, "file_name", None) or f"file_{m.id}.mp4"
                    info = await enrich_media_info(parse_media_info(fname, media.file_size))
                    stremio_type = "series" if info.get("media_type") == "series" else "movie"
                    stream_url = f"{WORKER_URL}/stream/{ch}/{m.id}?name={fname}"
                    dl_url = f"{WORKER_URL}/dl/{ch}/{m.id}?name={fname}"
                    stremio_url = f"stremio://{WORKER_URL.replace('https://','')}/stream/{stremio_type}/tg:{ch}:{m.id}.json"

                    extras = []
                    if info.get("year"):
                        extras.append(f"📅 {info['year']}")
                    if info.get("source") and info["source"] != "Unknown":
                        extras.append(f"📀 {info['source']}")
                    extras_str = " | ".join(extras)
                    if extras_str:
                        extras_str = "\n" + extras_str

                    tmdb_block = ""
                    tmdb = info.get("tmdb")
                    if tmdb:
                        rating = tmdb.get("vote_average", "")
                        rating_str = f"⭐ {rating}/10" if rating else ""
                        tmdb_block = f"\n{rating_str}" if rating_str else ""

                    await msg.reply_text(
                        f"🎬 **{fname}**\n"
                        f"📌 {info['quality']} | {info['source']} | 📦 {info['size']}"
                        f"{extras_str}"
                        f"{tmdb_block}",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("▶️ Stream", url=stream_url),
                            InlineKeyboardButton("📥 Download", url=dl_url),
                        ], [
                            InlineKeyboardButton("🍿 Stremio", url=stremio_url),
                        ]]),
                    )
            except Exception as e:
                print(f"Search error {ch}: {e}")

        if found:
            await status.delete()
        else:
            await status.edit_text(f"❌ Nothing found for **{query}** in source channels.")

    @tg_client.on_message(filters.media | filters.forwarded)
    async def auto_stream_link(client: Client, msg: Message):
        media = msg.video or msg.document or msg.audio
        if not media:
            return
        chat_id = str(msg.chat.id)
        fwd_chat = str(msg.forward_from_chat.id) if msg.forward_from_chat else ""
        target = fwd_chat if fwd_chat in ALLOWED_CHANNELS else (chat_id if chat_id in ALLOWED_CHANNELS else None)
        if not target:
            return
        mid = msg.forward_from_message_id or msg.id
        fname = getattr(media, "file_name", None) or "video.mp4"
        info = await enrich_media_info(parse_media_info(fname, media.file_size))
        stream_url = f"{WORKER_URL}/stream/{target}/{mid}?name={fname}"
        dl_url = f"{WORKER_URL}/dl/{target}/{mid}?name={fname}"
        extras = []
        if info.get("year"):
            extras.append(f"📅 {info['year']}")
        if info.get("source") and info["source"] != "Unknown":
            extras.append(info["source"])
        extras_str = f" | {' | '.join(extras)}" if extras else ""
        await msg.reply_text(
            f"⚡ **Stream link ready!**\n"
            f"🎬 `{fname}`\n"
            f"📦 {info['size']} | 📌 {info['quality']}{extras_str}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("▶️ Stream", url=stream_url),
                InlineKeyboardButton("📥 Download", url=dl_url),
            ]]),
        )

    @tg_client.on_inline_query()
    async def inline_search(client: Client, iq: InlineQuery):
        q = iq.query.strip()
        if len(q) < 2:
            return
        results = []
        for ch in ALLOWED_CHANNELS:
            try:
                async for m in client.search_messages(int(ch), query=q, limit=5):
                    media = m.video or m.document or m.audio
                    if not media:
                        continue
                    fname = getattr(media, "file_name", None) or f"file_{m.id}.mp4"
                    info = await enrich_media_info(parse_media_info(fname, media.file_size))
                    url = f"{WORKER_URL}/stream/{ch}/{m.id}?name={fname}"
                    desc_parts = [p for p in [info['quality'], info.get('source'), info['size']] if p and p != 'Unknown']
                    desc = " | ".join(desc_parts)
                    if info.get("year"):
                        desc = f"{info['year']} · {desc}"
                    results.append(InlineQueryResultArticle(
                        title=info.get('title') or fname,
                        description=desc,
                        input_message_content=InputTextMessageContent(
                            f"🎬 **{fname}**\n📌 {info['quality']} | {info.get('source','?')} | 📦 {info['size']}\n🔗 {url}"
                        ),
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("▶️ Stream Now", url=url)
                        ]]),
                    ))
            except Exception as e:
                print(f"Inline error {ch}: {e}")
        await iq.answer(results[:15], cache_time=300)


# ── FASTAPI ENDPOINTS ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    if tg_client:
        try:
            await tg_client.start()
            mode = "USER SESSION" if SESSION_STRING else "BOT TOKEN"
            print(f"✅ Telegram connected via {mode}")

            # Warm the peer cache by fetching dialogs — populates storage with peer info
            # This helps resolve_peer() find channels by their internal peer ID
            try:
                async for _ in tg_client.get_dialogs(limit=200):
                    pass
                print("✅ Dialog cache warmed (peers learned)")
            except Exception as e:
                print(f"⚠️  Could not warm dialog cache: {e}")

            # Resolve peers so Pyrogram caches them — required before any search/get_chat_history call
            resolved = 0
            for ch in ALLOWED_CHANNELS:
                try:
                    chat = await tg_client.get_chat(int(ch))
                    print(f"✅ Peer resolved: {ch} → {chat.title}")
                    resolved += 1
                except ValueError as e:
                    if "Peer id invalid" in str(e):
                        print(f"⚠️  Peer {ch} rejected by Pyrogram's MIN_CHANNEL_ID threshold.")
                        print(f"    → Upgrade pyrogram>=2.1.32 or ensure your account has joined this channel.")
                    else:
                        print(f"⚠️  Could not resolve peer {ch}: {e}")
                except Exception as e:
                    print(f"⚠️  Could not resolve peer {ch}: {e}")

            if resolved:
                print(f"✅ {resolved}/{len(ALLOWED_CHANNELS)} channels resolved successfully")
            else:
                print(f"⚠️  No channels were resolved. Searches and streaming will likely fail.")

        except Exception as e:
            print(f"❌ Startup error: {e}")

@app.on_event("shutdown")
async def shutdown():
    if tg_client and getattr(tg_client, "is_connected", False):
        await tg_client.stop()

@app.get("/")
def root():
    mode = "user_session" if SESSION_STRING else ("bot_token" if BOT_TOKEN else "none")
    return {
        "status": "online",
        "version": "4.0.0",
        "mode": mode,
        "connected": getattr(tg_client, "is_connected", False) if tg_client else False,
        "channels": ALLOWED_CHANNELS,
    }

@app.get("/health")
def health():
    return {"status": "ok", "connected": getattr(tg_client, "is_connected", False) if tg_client else False}

@app.get("/search")
async def search_api(q: str = Query(..., min_length=2), channel_id: str = None, enriched: str = "false"):
    if not tg_client:
        raise HTTPException(500, "No Telegram client configured.")
    global CACHE_HIT, CACHE_MISS
    cache_key = (q.lower().strip(), channel_id, enriched)
    now = time.time()
    if cache_key in SEARCH_CACHE:
        ts, cached = SEARCH_CACHE[cache_key]
        if now - ts < SEARCH_CACHE_TTL:
            CACHE_HIT += 1
            return cached
    CACHE_MISS += 1
    results = []
    targets = [channel_id] if channel_id else ALLOWED_CHANNELS
    for ch in targets:
        try:
            async for msg in tg_client.search_messages(int(ch), query=q, limit=20):
                media = msg.video or msg.document or msg.audio
                if not media:
                    continue
                fname = getattr(media, "file_name", None) or f"file_{msg.id}.mp4"
                info = parse_media_info(fname, media.file_size)
                if enriched.lower() == "true" and TMDB_API_KEY:
                    info = await enrich_media_info(info)
                results.append({
                    "channel_id": ch,
                    "message_id": msg.id,
                    "file_name": fname,
                    "file_size": media.file_size,
                    "mime_type": getattr(media, "mime_type", "video/mp4"),
                    "info": info,
                })
        except Exception as e:
            print(f"Search error {ch}: {e}")
    body = {"query": q, "total": len(results), "results": results}
    SEARCH_CACHE[cache_key] = (now, body)
    return body

@app.get("/recent")
async def recent_api(channel_id: str = None, limit: int = 20, enriched: str = "false"):
    if not tg_client:
        return {"total": 0, "items": []}
    global CACHE_HIT, CACHE_MISS
    cache_key = ("recent", channel_id, limit, enriched)
    now = time.time()
    if cache_key in SEARCH_CACHE:
        ts, cached = SEARCH_CACHE[cache_key]
        if now - ts < SEARCH_CACHE_TTL:
            CACHE_HIT += 1
            return cached
    CACHE_MISS += 1
    results = []
    targets = [channel_id] if channel_id else ALLOWED_CHANNELS
    for ch in targets:
        try:
            async for msg in tg_client.get_chat_history(int(ch), limit=limit):
                media = msg.video or msg.document or msg.audio
                if not media:
                    continue
                fname = getattr(media, "file_name", None) or f"file_{msg.id}.mp4"
                info = parse_media_info(fname, media.file_size)
                if enriched.lower() == "true" and TMDB_API_KEY:
                    info = await enrich_media_info(info)
                results.append({
                    "channel_id": ch,
                    "message_id": msg.id,
                    "file_name": fname,
                    "file_size": media.file_size,
                    "mime_type": getattr(media, "mime_type", "video/mp4"),
                    "info": info,
                })
        except Exception as e:
            print(f"Recent error {ch}: {e}")
    body = {"total": len(results), "items": results}
    SEARCH_CACHE[cache_key] = (now, body)
    return body

@app.get("/api/tmdb-search")
async def tmdb_search_api(q: str = Query(..., min_length=2), year: int = None, media_type: str = "movie"):
    """Search TMDB for movie/series metadata. Returns enriched info or empty."""
    if not TMDB_API_KEY:
        raise HTTPException(400, "TMDB_API_KEY not configured.")
    data = await tmdb_search(q, year, media_type)
    if not data:
        raise HTTPException(404, "No match found.")
    return data

async def _fetch_one_chunk(msg, chunk_index: int) -> bytes:
    """Fetch a single 1 MiB Telegram chunk under the download lock, then release."""
    async with DOWNLOAD_SEM:
        async for raw in tg_client.stream_media(msg, offset=chunk_index, limit=1):
            return bytes(raw)
    return b""


async def _read_telegram_range(msg, start: int, length: int):
    """Yield exact byte range. Lock only per Telegram chunk so seeks are not blocked."""
    offset_chunks = start // CHUNK_SIZE
    skip_front = start % CHUNK_SIZE
    limit_chunks = (skip_front + length + CHUNK_SIZE - 1) // CHUNK_SIZE

    sent = 0
    trim = skip_front
    for i in range(limit_chunks):
        chunk = await _fetch_one_chunk(msg, offset_chunks + i)
        if not chunk:
            break
        if trim:
            if trim >= len(chunk):
                trim -= len(chunk)
                continue
            chunk = chunk[trim:]
            trim = 0
        if sent + len(chunk) > length:
            chunk = chunk[: length - sent]
        if not chunk:
            break
        sent += len(chunk)
        yield chunk
        if sent >= length:
            break


@app.api_route("/stream/{channel_id}/{message_id}", methods=["GET", "HEAD"])
async def stream_api(channel_id: str, message_id: int, request: Request):
    if not tg_client:
        raise HTTPException(500, "No Telegram client configured.")
    for attempt in range(3):
        try:
            chat_id = int(channel_id)
            try:
                msg = await tg_client.get_messages(chat_id, message_id)
            except ChannelPrivate:
                raise HTTPException(403, f"Cannot access channel {channel_id}. Use SESSION_STRING mode.")
            except MessageIdInvalid:
                raise HTTPException(404, f"Message {message_id} not found.")

            media = msg.video or msg.document or msg.audio
            if not media:
                raise HTTPException(404, "No streamable media in message.")

            file_size = media.file_size
            if not file_size or file_size <= 0:
                raise HTTPException(404, "Media has unknown size.")
            mime = getattr(media, "mime_type", "video/mp4")
            range_hdr = request.headers.get("range")
            start, end = 0, file_size - 1
            if range_hdr:
                try:
                    unit, _, rng = range_hdr.partition("=")
                    if unit.strip().lower() != "bytes":
                        raise ValueError("unsupported range unit")
                    first, _, second = rng.strip().partition("-")
                    if first == "" and second:
                        suffix = int(second)
                        start = max(0, file_size - suffix)
                    else:
                        start = int(first) if first else 0
                        end = int(second) if second else file_size - 1
                except ValueError:
                    raise HTTPException(416, "Invalid Range header")
                if start >= file_size:
                    return Response(status_code=416, headers={
                        "Content-Range": f"bytes */{file_size}",
                        "Access-Control-Allow-Origin": "*",
                    })
                end = min(end, file_size - 1)
                if end < start:
                    end = file_size - 1

            length = end - start + 1
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Content-Type": mime,
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=3600",
            }
            if range_hdr:
                headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            status = 206 if range_hdr else 200

            # HEAD only needs size/range metadata — do not touch Telegram download.
            if request.method == "HEAD":
                return Response(status_code=status, headers=headers)

            # Small probes (Stremio/VLC Range bytes=0-1 etc): buffer exact body.
            # Avoids StreamingResponse + Content-Length races under FloodWait.
            if length <= CHUNK_SIZE:
                buf = bytearray()
                try:
                    async for piece in _read_telegram_range(msg, start, length):
                        buf.extend(piece)
                except FloodWait as fw:
                    await asyncio.sleep(fw.value)
                    continue
                return Response(bytes(buf[:length]), status_code=status, headers=headers)

            async def gen(s=start, n=length):
                try:
                    async for piece in _read_telegram_range(msg, s, n):
                        yield piece
                except FloodWait as fw:
                    print(f"FloodWait during stream: {fw.value}s")
                except Exception as e:
                    print(f"Stream body error: {e}")

            return StreamingResponse(gen(), status_code=status, headers=headers)
        except FloodWait as fw:
            await asyncio.sleep(fw.value)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, str(e))
    raise HTTPException(429, "Rate limited by Telegram.")
