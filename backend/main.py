import os
import re
import asyncio
import time
import requests
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pyrogram import Client
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
BACKEND_URL = os.environ.get(
    "BACKEND_URL",
    "https://retail-mallory-nishokroshan07-1aa75de1.koyeb.app",
).rstrip("/")
ALLOWED_CHANNELS = [
    c.strip()
    for c in os.environ.get("ALLOWED_CHANNELS", "-1003916531716,-1002502061360,-1003967652604,-1002708448330").split(",")
    if c.strip()
]

BOT_USERNAME = ""
BOT_LINK = ""

app = FastAPI(title="Telegram Streamer MTProto Engine", version="4.3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Pyrogram get_file is not safe under concurrent streams on one client —
# interleaved chunks blow past Content-Length ("Too much data for declared Content-Length").
# Hold the lock across a small batch so we pay one acquire per ~4 MiB, not per 1 MiB.
DOWNLOAD_SEM = asyncio.Semaphore(1)
CHUNK_SIZE = 1024 * 1024
PREFETCH_CHUNKS = 4  # fetch 4 consecutive Telegram chunks per lock hold

# Cache get_messages metadata so Range probes / seeks skip repeated peer RPCs.
MSG_CACHE = {}  # (channel_id, message_id) -> (ts, msg)
MSG_CACHE_TTL = 600

# User session = channel search + streaming.
# Bot commands/inline use Telegram HTTP webhooks (no Pyrogram bot_client polling).
tg_client = None
bot_client = None  # unused — webhook mode only

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
    print("⚠️  Using BOT TOKEN only (bot must be admin / member in each channel)")
    tg_client = Client(
        "tg_bot_session",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )
else:
    print("❌ No valid credentials found. Set SESSION_STRING (preferred) or BOT_TOKEN.")

if BOT_TOKEN:
    print("✅ BOT_TOKEN set — bot commands via HTTP webhook")
elif SESSION_STRING:
    print("⚠️  BOT_TOKEN missing — Telegram bot commands/inline disabled (Stremio/API still work)")

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
SEARCH_CACHE_TTL = 120  # seconds
CACHE_HIT, CACHE_MISS = 0, 0  # stats


def _tmdb_search_sync(query: str, year: int = None, media_type: str = "movie"):
    """Blocking TMDB lookup — run via asyncio.to_thread so the event loop stays free."""
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
        resp = requests.get(url, timeout=3)
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        if not results:
            url_no_year = f"https://api.themoviedb.org/3/search/{media_type}?api_key={TMDB_API_KEY}&query={query}"
            resp2 = requests.get(url_no_year, timeout=3)
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


async def tmdb_search(query: str, year: int = None, media_type: str = "movie"):
    """Search TMDB and return first match metadata. Returns None if no key or no match."""
    return await asyncio.to_thread(_tmdb_search_sync, query, year, media_type)


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




# ── BOT API (HTTP webhooks — wakes Koyeb on each message) ─────────────────────

def _bot_api_sync(method: str, **payload):
    if not BOT_TOKEN:
        return {"ok": False}
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=payload,
        timeout=12,
    )
    return r.json()


async def bot_api(method: str, **payload):
    """Non-blocking Bot API call (sync requests off the event loop)."""
    return await asyncio.to_thread(_bot_api_sync, method, **payload)


def _ikb(rows):
    """Build Bot API inline_keyboard reply_markup from [[btn, ...], ...]."""
    return {"inline_keyboard": rows}


async def _search_channel(ch: str, query: str, limit: int = 8):
    """Search one channel; return list of (ch, msg, media) hits."""
    hits = []
    try:
        async for m in tg_client.search_messages(int(ch), query=query, limit=limit):
            media = m.video or m.document or m.audio
            if not media:
                continue
            hits.append((ch, m, media))
            if len(hits) >= limit:
                break
    except Exception as e:
        print(f"Search error {ch}: {e}")
    return hits


async def _bot_search_and_reply(chat_id: int, query: str):
    if not tg_client:
        await bot_api("sendMessage", chat_id=chat_id, text="❌ Search backend not connected.", parse_mode="Markdown")
        return

    print(f"[bot] /search {query!r} chat={chat_id}")
    status = await bot_api(
        "sendMessage",
        chat_id=chat_id,
        text=f"🔍 Searching for **{query}**…",
        parse_mode="Markdown",
    )
    status_id = (status.get("result") or {}).get("message_id")
    max_results = 8

    # Parallel channel search — biggest bot latency win vs sequential waits.
    channel_hits = await asyncio.gather(
        *[_search_channel(ch, query, limit=6) for ch in ALLOWED_CHANNELS]
    )
    found = 0
    for hits in channel_hits:
        for ch, m, media in hits:
            if found >= max_results:
                break
            found += 1
            fname = getattr(media, "file_name", None) or f"file_{m.id}.mp4"
            # Skip TMDB on bot hot path — parse filename only (quality/size/year).
            info = parse_media_info(fname, media.file_size)
            stream_url = f"{WORKER_URL}/stream/{ch}/{m.id}?name={fname}"
            dl_url = f"{WORKER_URL}/dl/{ch}/{m.id}?name={fname}"

            extras = []
            if info.get("year"):
                extras.append(f"📅 {info['year']}")
            if info.get("source") and info["source"] != "Unknown":
                extras.append(f"📀 {info['source']}")
            extras_str = ("\n" + " | ".join(extras)) if extras else ""

            await bot_api(
                "sendMessage",
                chat_id=chat_id,
                text=(
                    f"🎬 **{fname}**\n"
                    f"📌 {info['quality']} | {info['source']} | 📦 {info['size']}"
                    f"{extras_str}"
                ),
                parse_mode="Markdown",
                reply_markup=_ikb([[
                    {"text": "▶️ Stream", "url": stream_url},
                    {"text": "📥 Download", "url": dl_url},
                ]]),
            )
        if found >= max_results:
            break

    if status_id:
        if found:
            await bot_api("deleteMessage", chat_id=chat_id, message_id=status_id)
        else:
            await bot_api(
                "editMessageText",
                chat_id=chat_id,
                message_id=status_id,
                text=f"❌ Nothing found for **{query}** in source channels.",
                parse_mode="Markdown",
            )


async def _bot_handle_message(message: dict):
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return

    text = (message.get("text") or "").strip()
    if not text:
        if message.get("video") or message.get("document") or message.get("audio") or message.get("forward_from_chat"):
            await bot_api(
                "sendMessage",
                chat_id=chat_id,
                text="Forward a video **from a source channel**, or use `/search <title>`.",
                parse_mode="Markdown",
            )
        return

    cmd = text.split()[0].split("@")[0].lower()
    args = text.split()[1:]

    if cmd == "/start":
        print(f"[bot] /start from {(message.get('from') or {}).get('id', '?')}")
        await bot_api(
            "sendMessage",
            chat_id=chat_id,
            text=(
                "🎬 **Telegram Movie Streamer Bot**\n\n"
                "Commands:\n"
                "• `/search <title>` — search all channels\n"
                "• `/channels` — list source channels\n"
                "• `/help` — usage guide"
            ),
            parse_mode="Markdown",
            reply_markup=_ikb([[
                {"text": "🌐 Stremio Addon", "url": f"{WORKER_URL}/manifest.json"},
                {"text": "🔍 Search", "switch_inline_query_current_chat": ""},
            ]]),
            disable_web_page_preview=True,
        )
        return

    if cmd == "/help":
        uname = f"@{BOT_USERNAME}" if BOT_USERNAME else "@your_bot"
        await bot_api(
            "sendMessage",
            chat_id=chat_id,
            text=(
                "📖 **How to use:**\n\n"
                "1. `/search Avengers` — search all source channels\n"
                "2. Type a movie title directly in this chat\n"
                f"3. Inline: type `{uname} movie title` anywhere in Telegram\n"
                "4. Install Stremio Addon via the /start menu"
            ),
            parse_mode="Markdown",
        )
        return

    if cmd == "/channels":
        lines = "\n".join(f"• `{ch}`" for ch in ALLOWED_CHANNELS)
        await bot_api(
            "sendMessage",
            chat_id=chat_id,
            text=f"📢 **Source Channels:**\n\n{lines}",
            parse_mode="Markdown",
        )
        return

    if cmd == "/search":
        query = " ".join(args).strip()
        if not query:
            await bot_api("sendMessage", chat_id=chat_id, text="Usage: `/search <movie title>`", parse_mode="Markdown")
            return
        await _bot_search_and_reply(chat_id, query)
        return

    if chat.get("type") == "private" and len(text) >= 2 and not text.startswith("/"):
        await _bot_search_and_reply(chat_id, text)


async def _bot_handle_inline(inline_query: dict):
    iq_id = inline_query.get("id")
    q = (inline_query.get("query") or "").strip()
    if not iq_id:
        return
    if len(q) < 2:
        await bot_api(
            "answerInlineQuery",
            inline_query_id=iq_id,
            results=[],
            cache_time=5,
            switch_pm_text="Type a movie title…",
            switch_pm_parameter="help",
        )
        return
    if not tg_client:
        await bot_api("answerInlineQuery", inline_query_id=iq_id, results=[], cache_time=5)
        return

    # Parallel channel search; no TMDB — answerInlineQuery has a ~10s budget.
    channel_hits = await asyncio.gather(
        *[_search_channel(ch, q, limit=4) for ch in ALLOWED_CHANNELS]
    )
    results = []
    for hits in channel_hits:
        for ch, m, media in hits:
            if len(results) >= 8:
                break
            fname = getattr(media, "file_name", None) or f"file_{m.id}.mp4"
            info = parse_media_info(fname, media.file_size)
            url = f"{WORKER_URL}/stream/{ch}/{m.id}?name={fname}"
            desc_parts = [p for p in [info["quality"], info.get("source"), info["size"]] if p and p != "Unknown"]
            desc = " | ".join(desc_parts)
            if info.get("year"):
                desc = f"{info['year']} · {desc}"
            results.append({
                "type": "article",
                "id": f"{ch}_{m.id}",
                "title": (info.get("title") or fname)[:64],
                "description": desc[:120],
                "input_message_content": {
                    "message_text": (
                        f"🎬 **{fname}**\n"
                        f"📌 {info['quality']} | {info.get('source', '?')} | 📦 {info['size']}\n"
                        f"🔗 {url}"
                    ),
                    "parse_mode": "Markdown",
                },
                "reply_markup": _ikb([[{"text": "▶️ Stream Now", "url": url}]]),
            })
        if len(results) >= 8:
            break

    await bot_api("answerInlineQuery", inline_query_id=iq_id, results=results, cache_time=60)


# ── FASTAPI ENDPOINTS ─────────────────────────────────────────────────────────

async def _boot_telegram():
    """Connect Telegram AFTER uvicorn is listening so Koyeb health checks pass."""
    global BOT_USERNAME, BOT_LINK

    if tg_client:
        try:
            await tg_client.start()
            mode = "USER SESSION" if SESSION_STRING else "BOT TOKEN"
            print(f"✅ Telegram connected via {mode}")

            try:
                async for _ in tg_client.get_dialogs(limit=200):
                    pass
                print("✅ Dialog cache warmed (peers learned)")
            except Exception as e:
                print(f"⚠️  Could not warm dialog cache: {e}")

            resolved = 0
            for ch in ALLOWED_CHANNELS:
                try:
                    chat = await tg_client.get_chat(int(ch))
                    print(f"✅ Peer resolved: {ch} → {chat.title}")
                    resolved += 1
                except Exception as e:
                    print(f"⚠️  Could not resolve peer {ch}: {e}")

            if resolved:
                print(f"✅ {resolved}/{len(ALLOWED_CHANNELS)} channels resolved successfully")
            else:
                print("⚠️  No channels were resolved. Searches and streaming will likely fail.")
        except Exception as e:
            print(f"❌ Telegram user-session startup error: {e}")

    if BOT_TOKEN:
        try:
            me = await bot_api("getMe")
            if me.get("ok"):
                BOT_USERNAME = (me["result"].get("username") or "")
                BOT_LINK = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else ""
                uname = f"@{BOT_USERNAME}" if BOT_USERNAME else "(no username — set one in @BotFather)"
                print(f"✅ Bot API getMe: {uname}")
                if BOT_LINK:
                    print(f"✅ Open bot: {BOT_LINK}")
            else:
                print(f"⚠️  getMe failed: {me}")

            await bot_api(
                "setMyCommands",
                commands=[
                    {"command": "start", "description": "Start the bot"},
                    {"command": "search", "description": "Search movies/series"},
                    {"command": "channels", "description": "List source channels"},
                    {"command": "help", "description": "How to use"},
                ],
            )

            webhook_url = f"{BACKEND_URL}/telegram/webhook"
            wh = await bot_api(
                "setWebhook",
                url=webhook_url,
                drop_pending_updates=True,
                allowed_updates=["message", "inline_query"],
            )
            if wh.get("ok"):
                print(f"✅ Webhook set: {webhook_url}")
            else:
                print(f"❌ setWebhook failed: {wh}")
        except Exception as e:
            print(f"❌ Bot webhook startup error: {e}")
    else:
        print("⚠️  Bot commands disabled (set BOT_TOKEN to enable)")


@app.on_event("startup")
async def startup():
    # Return immediately so the HTTP port opens; Telegram boot runs in background.
    # Otherwise Koyeb TCP health checks fail during long Pyrogram connect.
    asyncio.create_task(_boot_telegram())
    print("✅ HTTP ready (Telegram boot running in background)")


@app.on_event("shutdown")
async def shutdown():
    if tg_client and getattr(tg_client, "is_connected", False):
        await tg_client.stop()


async def _process_telegram_update(update: dict):
    try:
        if "message" in update:
            await _bot_handle_message(update["message"])
        elif "inline_query" in update:
            await _bot_handle_inline(update["inline_query"])
    except Exception as e:
        print(f"[webhook] handler error: {e}")


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Ack Telegram immediately; process search/commands in background.
    Holding the webhook until search finishes makes the bot feel like a turtle
    and causes Telegram to retry (duplicate replies)."""
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    asyncio.create_task(_process_telegram_update(update))
    return {"ok": True}


@app.get("/")
def root():
    mode = "user_session" if SESSION_STRING else ("bot_token" if BOT_TOKEN else "none")
    return {
        "status": "online",
        "version": "4.3.0",
        "mode": mode,
        "bot_mode": "webhook",
        "connected": getattr(tg_client, "is_connected", False) if tg_client else False,
        "bot": bool(BOT_TOKEN),
        "bot_connected": bool(BOT_TOKEN and BOT_USERNAME),
        "bot_username": BOT_USERNAME or None,
        "bot_link": BOT_LINK or None,
        "channels": ALLOWED_CHANNELS,
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "4.3.0",
        "bot_mode": "webhook",
        "connected": getattr(tg_client, "is_connected", False) if tg_client else False,
        "bot": bool(BOT_TOKEN),
        "bot_connected": bool(BOT_TOKEN and BOT_USERNAME),
        "bot_username": BOT_USERNAME or None,
        "bot_link": BOT_LINK or None,
    }


@app.get("/bot")
def bot_info():
    """Public helper: open this URL to get the Telegram bot deep link."""
    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN not configured on server.")
    if not BOT_USERNAME:
        raise HTTPException(503, "Bot not ready (getMe failed or no username).")
    if not BOT_LINK:
        raise HTTPException(503, "Bot has no username. Set one in @BotFather.")
    return {
        "username": BOT_USERNAME,
        "link": BOT_LINK,
        "bot_mode": "webhook",
        "webhook": f"{BACKEND_URL}/telegram/webhook",
        "commands": ["/start", "/search <title>", "/channels", "/help"],
    }


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

    # Telegram search is literal. "obsession 2026" misses "Obsession (2025)".
    # Also strip common typo-ish extra tokens: try full query, then without year.
    queries = []
    raw = q.strip()
    queries.append(raw)
    no_year = re.sub(r"\b(19|20)\d{2}\b", " ", raw).strip()
    no_year = re.sub(r"\s+", " ", no_year)
    if no_year and no_year.lower() != raw.lower():
        queries.append(no_year)

    results = []
    seen = set()
    targets = [channel_id] if channel_id else ALLOWED_CHANNELS
    do_enrich = enriched.lower() == "true" and bool(TMDB_API_KEY)

    for query in queries:
        # Search all channels in parallel for this query variant.
        channel_hits = await asyncio.gather(
            *[_search_channel(ch, query, limit=20) for ch in targets]
        )
        for hits in channel_hits:
            for ch, msg, media in hits:
                key = (ch, msg.id)
                if key in seen:
                    continue
                seen.add(key)
                fname = getattr(media, "file_name", None) or f"file_{msg.id}.mp4"
                info = parse_media_info(fname, media.file_size)
                if do_enrich:
                    info = await enrich_media_info(info)
                results.append({
                    "channel_id": ch,
                    "message_id": msg.id,
                    "file_name": fname,
                    "file_size": media.file_size,
                    "mime_type": getattr(media, "mime_type", "video/mp4"),
                    "info": info,
                })
        if results:
            break
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

async def _get_cached_message(channel_id: str, message_id: int):
    """Return message object, using a short TTL cache to skip repeated get_messages RPCs."""
    key = (str(channel_id), int(message_id))
    now = time.time()
    cached = MSG_CACHE.get(key)
    if cached:
        ts, msg = cached
        if now - ts < MSG_CACHE_TTL:
            return msg
    msg = await tg_client.get_messages(int(channel_id), message_id)
    MSG_CACHE[key] = (now, msg)
    # Bound cache size (simple FIFO-ish prune)
    if len(MSG_CACHE) > 512:
        oldest = sorted(MSG_CACHE.items(), key=lambda kv: kv[1][0])[:128]
        for k, _ in oldest:
            MSG_CACHE.pop(k, None)
    return msg


async def _fetch_chunk_batch(msg, start_index: int, count: int) -> list:
    """Fetch up to `count` consecutive 1 MiB Telegram chunks under one lock hold."""
    chunks = []
    async with DOWNLOAD_SEM:
        async for raw in tg_client.stream_media(msg, offset=start_index, limit=count):
            chunks.append(bytes(raw))
            if len(chunks) >= count:
                break
    return chunks


async def _read_telegram_range(msg, start: int, length: int):
    """Yield exact byte range. Prefetch several chunks per lock to cut round-trips."""
    offset_chunks = start // CHUNK_SIZE
    skip_front = start % CHUNK_SIZE
    limit_chunks = (skip_front + length + CHUNK_SIZE - 1) // CHUNK_SIZE

    sent = 0
    trim = skip_front
    i = 0
    while i < limit_chunks:
        batch_n = min(PREFETCH_CHUNKS, limit_chunks - i)
        batch = await _fetch_chunk_batch(msg, offset_chunks + i, batch_n)
        if not batch:
            break
        for chunk in batch:
            i += 1
            if trim:
                if trim >= len(chunk):
                    trim -= len(chunk)
                    continue
                chunk = chunk[trim:]
                trim = 0
            if sent + len(chunk) > length:
                chunk = chunk[: length - sent]
            if not chunk:
                return
            sent += len(chunk)
            yield chunk
            if sent >= length:
                return
        if len(batch) < batch_n:
            break


@app.api_route("/stream/{channel_id}/{message_id}", methods=["GET", "HEAD"])
async def stream_api(channel_id: str, message_id: int, request: Request):
    if not tg_client:
        raise HTTPException(500, "No Telegram client configured.")
    for attempt in range(3):
        try:
            try:
                msg = await _get_cached_message(channel_id, message_id)
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
