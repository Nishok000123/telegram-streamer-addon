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
INDEX_SECRET = os.environ.get("INDEX_SECRET", "").strip()
INDEX_PER_CHANNEL = int(os.environ.get("INDEX_PER_CHANNEL", "250"))
ALLOWED_CHANNELS = [
    c.strip()
    for c in os.environ.get("ALLOWED_CHANNELS", "-1003916531716,-1002502061360,-1003967652604,-1002708448330").split(",")
    if c.strip()
]

BOT_USERNAME = ""
BOT_LINK = ""

app = FastAPI(title="Telegram Streamer MTProto Engine", version="4.4.0")
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

# Media library index (scan once, browse forever until rebuild).
MEDIA_INDEX = {
    "updated_at": 0,
    "total": 0,
    "items": [],
}
INDEX_BUILDING = False
HOT_LIMIT = 40  # auto-suggest prewarm count per category

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


def parse_media_info(file_name: str, file_size: int, channel_id: str = "") -> dict:
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

    lang = detect_language(file_name, channel_hint=channel_id or "")
    info["language"] = lang["language"]
    info["languages"] = lang["languages"]
    info["multi_audio"] = lang["multi_audio"]
    return info


TAMIL_RE = re.compile(
    r"(?:^|[^a-z])(?:tamil|tamizh|tam(?:il)?|தமிழ்)(?:[^a-z]|$)|tamil[\s._-]?audio|audio[\s._-]?tamil",
    re.IGNORECASE,
)
HINDI_RE = re.compile(r"(?:^|[^a-z])(?:hindi|hin)(?:[^a-z]|$)|hindi[\s._-]?audio", re.IGNORECASE)
TELUGU_RE = re.compile(r"(?:^|[^a-z])(?:telugu|tel)(?:[^a-z]|$)", re.IGNORECASE)
MALAYALAM_RE = re.compile(r"(?:^|[^a-z])(?:malayalam|mal)(?:[^a-z]|$)", re.IGNORECASE)
KANNADA_RE = re.compile(r"(?:^|[^a-z])(?:kannada|kan)(?:[^a-z]|$)", re.IGNORECASE)
ENGLISH_RE = re.compile(r"(?:^|[^a-z])(?:english|eng)(?:[^a-z]|$)|eng[\s._-]?audio", re.IGNORECASE)
MULTI_RE = re.compile(r"multi[\s._-]?audio|dual[\s._-]?audio|\bmulti\b|\bdual\b", re.IGNORECASE)

# Channel ID → forced language hint (Tam Mob Hollywood often Tamil-dub / multi)
CHANNEL_LANG_HINT = {
    "-1002708448330": "tamil",  # Tam Mob
}


def detect_language(file_name: str, channel_hint: str = "") -> dict:
    """Heuristic language tags from filename + optional channel hint."""
    n = file_name or ""
    langs = []
    if TAMIL_RE.search(n):
        langs.append("tamil")
    if HINDI_RE.search(n):
        langs.append("hindi")
    if TELUGU_RE.search(n):
        langs.append("telugu")
    if MALAYALAM_RE.search(n):
        langs.append("malayalam")
    if KANNADA_RE.search(n):
        langs.append("kannada")
    if ENGLISH_RE.search(n):
        langs.append("english")

    multi = bool(MULTI_RE.search(n)) or len(langs) >= 2
    ch = str(channel_hint or "")
    hint = (CHANNEL_LANG_HINT.get(ch) or "").lower()

    if not langs and hint == "tamil":
        langs = ["tamil", "english"]
        multi = True

    if not langs:
        # No Indian lang tags → treat as English (Hollywood dumps)
        langs = ["english"]

    # Primary: Tamil wins if present (user preference), else first tag
    if "tamil" in langs:
        primary = "tamil"
    elif "english" in langs and not any(x in langs for x in ("hindi", "telugu", "malayalam", "kannada")):
        primary = "english"
    else:
        primary = langs[0]

    return {
        "language": primary,
        "languages": langs,
        "multi_audio": multi,
    }


def _popularity_score(item: dict) -> float:
    info = item.get("info") or {}
    score = 0.0
    q = (info.get("quality") or "").lower()
    if "4k" in q or "2160" in q:
        score += 40
    elif "1080" in q:
        score += 28
    elif "720" in q:
        score += 14
    src = (info.get("source") or "").lower()
    if "bluray" in src or "web-dl" in src or "webdl" in src:
        score += 10
    elif "webrip" in src:
        score += 6
    year = info.get("year")
    if year:
        # Prefer recent years
        score += max(0, min(20, (int(year) - 2000)))
    if info.get("multi_audio"):
        score += 8
    if info.get("language") == "tamil":
        score += 5
    # Newer telegram posts slightly higher
    score += min(15, (item.get("message_id") or 0) % 1000) / 100.0
    return score


def _index_item(ch: str, msg, media) -> dict:
    fname = getattr(media, "file_name", None) or f"file_{msg.id}.mp4"
    info = parse_media_info(fname, media.file_size, channel_id=ch)
    title = info.get("title") or fname
    title = re.sub(r"[._]+", " ", str(title)).strip()
    info["title"] = title
    return {
        "id": f"{ch}:{msg.id}",
        "channel_id": ch,
        "message_id": msg.id,
        "file_name": fname,
        "file_size": media.file_size,
        "mime_type": getattr(media, "mime_type", "video/mp4"),
        "date": int(msg.date.timestamp()) if getattr(msg, "date", None) else 0,
        "info": info,
        "r2_cached": False,
        "popularity": 0.0,
    }


async def rebuild_media_index(per_channel: int = None) -> dict:
    """Scan source channels and rebuild in-memory MEDIA_INDEX."""
    global MEDIA_INDEX, INDEX_BUILDING
    if not tg_client:
        raise RuntimeError("Telegram client not connected")
    if INDEX_BUILDING:
        return {"status": "busy", "message": "Index rebuild already running"}
    INDEX_BUILDING = True
    limit = per_channel or INDEX_PER_CHANNEL
    items = []
    seen = set()
    try:
        for ch in ALLOWED_CHANNELS:
            try:
                async for msg in tg_client.get_chat_history(int(ch), limit=limit):
                    media = msg.video or msg.document or msg.audio
                    if not media:
                        continue
                    key = (ch, msg.id)
                    if key in seen:
                        continue
                    seen.add(key)
                    item = _index_item(ch, msg, media)
                    item["popularity"] = _popularity_score(item)
                    items.append(item)
            except Exception as e:
                print(f"Index scan error {ch}: {e}")
        items.sort(key=lambda x: x.get("date") or 0, reverse=True)
        MEDIA_INDEX = {
            "updated_at": int(time.time()),
            "total": len(items),
            "per_channel": limit,
            "items": items,
        }
        print(f"✅ Media index rebuilt: {len(items)} items")
        return {"status": "ok", "total": len(items), "updated_at": MEDIA_INDEX["updated_at"]}
    finally:
        INDEX_BUILDING = False


def _filter_index(
    language: str = None,
    sort: str = "latest",
    limit: int = 40,
    media_type: str = "movie",
    multi_only: bool = False,
):
    items = list(MEDIA_INDEX.get("items") or [])
    if media_type and media_type != "all":
        items = [i for i in items if (i.get("info") or {}).get("media_type") == media_type]
    if language:
        lang = language.lower()
        if lang == "tamil":
            # Tamil primary OR multi/dual that includes Tamil
            items = [
                i for i in items
                if (i.get("info") or {}).get("language") == "tamil"
                or "tamil" in ((i.get("info") or {}).get("languages") or [])
            ]
        elif lang == "english":
            items = [
                i for i in items
                if (i.get("info") or {}).get("language") == "english"
                or (
                    "english" in ((i.get("info") or {}).get("languages") or [])
                    and (i.get("info") or {}).get("language") != "tamil"
                )
            ]
        else:
            items = [i for i in items if (i.get("info") or {}).get("language") == lang]
    if multi_only:
        items = [i for i in items if (i.get("info") or {}).get("multi_audio")]
    if sort == "popular":
        items.sort(key=lambda x: x.get("popularity") or 0, reverse=True)
    else:
        items.sort(key=lambda x: x.get("date") or 0, reverse=True)
    return items[: max(1, min(limit, 200))]


def build_category_bundle(limit: int = None):
    n = limit or HOT_LIMIT
    return {
        "updated_at": MEDIA_INDEX.get("updated_at") or 0,
        "total_indexed": MEDIA_INDEX.get("total") or 0,
        "tamil_latest": _filter_index("tamil", "latest", n),
        "tamil_popular": _filter_index("tamil", "popular", n),
        "english_latest": _filter_index("english", "latest", n),
        "english_popular": _filter_index("english", "popular", n),
        "multi_audio_latest": _filter_index(None, "latest", n, multi_only=True),
        "hot_prewarm": (
            _filter_index("tamil", "latest", min(15, n))
            + _filter_index("tamil", "popular", min(10, n))
            + _filter_index("english", "popular", min(10, n))
        ),
    }


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
    max_results = 5

    scored = []

    # Prefer index — score & require title tokens (less junk)
    for i in (MEDIA_INDEX.get("items") or []):
        s = _match_score(query, i)
        if s > 0:
            scored.append((s, i))

    if not scored:
        channel_hits = await asyncio.gather(
            *[_search_channel(ch, query, limit=5) for ch in ALLOWED_CHANNELS]
        )
        for hits in channel_hits:
            for ch, m, media in hits:
                fname = getattr(media, "file_name", None) or f"file_{m.id}.mp4"
                info = parse_media_info(fname, media.file_size, channel_id=ch)
                item = {
                    "id": f"{ch}:{m.id}",
                    "channel_id": ch,
                    "message_id": m.id,
                    "file_name": fname,
                    "file_size": media.file_size,
                    "info": info,
                }
                s = _match_score(query, item)
                if s > 0:
                    scored.append((s, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    # de-dupe
    seen = set()
    items = []
    for _, item in scored:
        key = (item.get("channel_id"), item.get("message_id"))
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
        if len(items) >= max_results:
            break

    if status_id:
        if items:
            await bot_api("deleteMessage", chat_id=chat_id, message_id=status_id)
        else:
            await bot_api(
                "editMessageText",
                chat_id=chat_id,
                message_id=status_id,
                text=f"❌ No good match for **{query}**. Try exact title, or forward the file from a source channel.",
                parse_mode="Markdown",
            )
            return

    await bot_api(
        "sendMessage",
        chat_id=chat_id,
        text=f"✅ **{len(items)}** match(es). Tap **🔗 Get link** only when you want the URL.",
        parse_mode="Markdown",
    )
    for item in items:
        await _bot_send_index_item(chat_id, item)


def _match_score(query: str, item: dict) -> float:
    """Require query tokens in filename/title — cuts unmatched spam."""
    q_tokens = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 1]
    if not q_tokens:
        return 0.0
    info = item.get("info") or {}
    hay = f"{item.get('file_name') or ''} {info.get('title') or ''}".lower()
    hits = sum(1 for t in q_tokens if t in hay)
    if hits == 0:
        return 0.0
    # Strict: all tokens, or all-but-one when query has 3+ words
    need = len(q_tokens) if len(q_tokens) < 3 else len(q_tokens) - 1
    if hits < need:
        return 0.0
    score = float(hits * 10)
    title = (info.get("title") or "").lower()
    if title and q_tokens[0] in title:
        score += 5
    if info.get("year") and str(info["year"]) in (query or ""):
        score += 8
    return score


# Remember filenames for Get-link callbacks (callback_data is 64-byte capped)
LINK_NAME_CACHE = {}


async def _bot_send_index_item(chat_id: int, item: dict):
    info = item.get("info") or {}
    fname = item.get("file_name") or "file"
    ch = item.get("channel_id")
    mid = item.get("message_id")
    LINK_NAME_CACHE[(str(ch), str(mid))] = fname
    langs = ", ".join(info.get("languages") or ([info.get("language")] if info.get("language") else []))
    extras = []
    if info.get("year"):
        extras.append(f"📅 {info['year']}")
    if info.get("multi_audio"):
        extras.append("🎧 Multi")
    if langs:
        extras.append(f"🗣 {langs}")
    if info.get("source") and info["source"] != "Unknown":
        extras.append(f"📀 {info['source']}")
    extras_str = ("\n" + " | ".join(extras)) if extras else ""
    await bot_api(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"🎬 **{(info.get('title') or fname)[:80]}**\n"
            f"📌 {info.get('quality', '?')} | {info.get('source', '?')} | 📦 {info.get('size', '?')}"
            f"{extras_str}\n"
            f"`{ch}:{mid}`"
        ),
        parse_mode="Markdown",
        reply_markup=_ikb([[
            {"text": "🔗 Get link", "callback_data": f"ln:{ch}:{mid}"},
            {"text": "💾 Cache", "callback_data": f"pw:{ch}:{mid}"},
        ]]),
    )


def _forward_channel_ref(message: dict):
    """Return (channel_id, message_id, title) from a forwarded channel post."""
    origin = message.get("forward_origin") or {}
    if origin.get("type") == "channel":
        chat = origin.get("chat") or {}
        return str(chat.get("id") or ""), origin.get("message_id"), chat.get("title")
    fchat = message.get("forward_from_chat") or {}
    if fchat.get("id") and message.get("forward_from_message_id"):
        return str(fchat.get("id")), message.get("forward_from_message_id"), fchat.get("title")
    return "", None, None


def _message_file_name(message: dict) -> str:
    for key in ("document", "video", "audio"):
        media = message.get(key)
        if media and media.get("file_name"):
            return media["file_name"]
    video = message.get("video")
    if video:
        return f"video_{video.get('file_unique_id', 'x')}.mp4"
    return "stream.mkv"


async def _bot_handle_forwarded_media(chat_id: int, message: dict):
    """Forward from allowlisted channel → auto-cache + Get link on click."""
    ch, mid, title = _forward_channel_ref(message)
    if not ch or not mid:
        await bot_api(
            "sendMessage",
            chat_id=chat_id,
            text=(
                "Forward the **original post from a source channel** "
                "(not a download/re-upload). Or use `/search <title>`."
            ),
            parse_mode="Markdown",
        )
        return
    if ch not in ALLOWED_CHANNELS:
        await bot_api(
            "sendMessage",
            chat_id=chat_id,
            text=(
                f"❌ Channel `{ch}` not in allowlist.\n"
                f"Allowed: `{', '.join(ALLOWED_CHANNELS)}`"
            ),
            parse_mode="Markdown",
        )
        return

    fname = _message_file_name(message)
    media = message.get("video") or message.get("document") or message.get("audio") or {}
    fsize = media.get("file_size") or 0
    info = parse_media_info(fname, fsize, channel_id=ch)
    item = {
        "id": f"{ch}:{mid}",
        "channel_id": ch,
        "message_id": mid,
        "file_name": fname,
        "file_size": fsize,
        "info": info,
    }
    # Soft-add into index so later /search finds it
    existing = {(i.get("channel_id"), i.get("message_id")) for i in (MEDIA_INDEX.get("items") or [])}
    if (ch, mid) not in existing:
        item["date"] = int(time.time())
        item["popularity"] = _popularity_score(item)
        MEDIA_INDEX.setdefault("items", []).insert(0, item)
        MEDIA_INDEX["total"] = len(MEDIA_INDEX["items"])

    await bot_api(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"📥 Got forward from **{title or ch}**\n"
            f"🎬 `{fname}`\n"
            f"💾 Auto-caching first ~128 MiB…"
        ),
        parse_mode="Markdown",
    )
    ok, detail = await _request_worker_prewarm(ch, str(mid), fname)
    await bot_api(
        "sendMessage",
        chat_id=chat_id,
        text=("✅ " if ok else "⚠️ ") + detail,
        parse_mode="Markdown",
    )
    await _bot_send_index_item(chat_id, item)


async def _resolve_stream_url(channel_id: str, message_id: str) -> str:
    fname = LINK_NAME_CACHE.get((str(channel_id), str(message_id)))
    if not fname:
        for i in (MEDIA_INDEX.get("items") or []):
            if str(i.get("channel_id")) == str(channel_id) and str(i.get("message_id")) == str(message_id):
                fname = i.get("file_name")
                break
    fname = fname or "stream.mkv"
    return f"{WORKER_URL}/stream/{channel_id}/{message_id}?name={fname}"


async def _push_index_to_worker():
    """POST category bundle to Worker (KV mirror if bound)."""
    try:
        body = build_category_bundle()
        body["items"] = MEDIA_INDEX.get("items") or []
        await asyncio.to_thread(
            requests.post,
            f"{WORKER_URL}/admin/index",
            json=body,
            headers={"X-Index-Secret": INDEX_SECRET} if INDEX_SECRET else {},
            timeout=60,
        )
    except Exception as e:
        print(f"⚠️  push index to worker failed: {e}")


async def _request_worker_prewarm(channel_id: str, message_id: str, file_name: str = None):
    try:
        payload = {"channel_id": str(channel_id), "message_id": int(message_id), "max_mb": 128}
        if file_name:
            payload["file_name"] = file_name
        r = await asyncio.to_thread(
            requests.post,
            f"{WORKER_URL}/admin/prewarm",
            json=payload,
            headers={"X-Index-Secret": INDEX_SECRET} if INDEX_SECRET else {},
            timeout=180,
        )
        data = r.json() if "json" in (r.headers.get("content-type") or "") else {"raw": r.text}
        if r.ok and data.get("ok"):
            mode = data.get("mode", "head")
            mb = round((data.get("bytes_warmed") or data.get("bytes") or 0) / (1024 * 1024), 1)
            note = data.get("note") or ""
            return True, f"Cached `{channel_id}:{message_id}` ({mb} MiB, {mode}). {note}"
        return False, f"Cache failed: `{data}`"
    except Exception as e:
        return False, f"Cache error: `{e}`"


async def _request_cached_list():
    try:
        r = await asyncio.to_thread(
            requests.get,
            f"{WORKER_URL}/admin/cached",
            headers={"X-Index-Secret": INDEX_SECRET} if INDEX_SECRET else {},
            timeout=30,
        )
        return r.json() if r.ok else {"ok": False, "items": []}
    except Exception:
        return {"ok": False, "items": []}


async def _bot_handle_callback(callback: dict):
    data = (callback.get("data") or "").strip()
    cq_id = callback.get("id")
    msg = callback.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")

    if data.startswith("ln:") and chat_id:
        parts = data.split(":")
        if len(parts) >= 3:
            ch, mid = parts[1], parts[2]
            if cq_id:
                await bot_api("answerCallbackQuery", callback_query_id=cq_id, text="Link ready")
            url = await _resolve_stream_url(ch, mid)
            await bot_api(
                "sendMessage",
                chat_id=chat_id,
                text=(
                    f"🔗 **Direct stream link**\n`{ch}:{mid}`\n\n"
                    f"`{url}`\n\n"
                    f"[Open stream]({url})"
                ),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        return

    if data.startswith("pw:") and chat_id:
        parts = data.split(":")
        if len(parts) >= 3:
            ch, mid = parts[1], parts[2]
            if cq_id:
                await bot_api("answerCallbackQuery", callback_query_id=cq_id, text="Caching…")
            fname = LINK_NAME_CACHE.get((str(ch), str(mid)))
            await bot_api(
                "sendMessage",
                chat_id=chat_id,
                text=f"💾 Caching `{ch}:{mid}` (first ~128 MiB)…",
                parse_mode="Markdown",
            )
            ok, detail = await _request_worker_prewarm(ch, mid, fname)
            await bot_api(
                "sendMessage",
                chat_id=chat_id,
                text=("✅ " if ok else "❌ ") + detail,
                parse_mode="Markdown",
            )
        return

    if cq_id:
        await bot_api("answerCallbackQuery", callback_query_id=cq_id)


async def _bot_handle_message(message: dict):
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return

    # Forwarded channel video/file → auto-cache (easiest path)
    is_forward = bool(message.get("forward_origin") or message.get("forward_from_chat"))
    has_media = bool(message.get("video") or message.get("document") or message.get("audio"))
    if is_forward and has_media:
        await _bot_handle_forwarded_media(chat_id, message)
        return

    text = (message.get("text") or "").strip()
    if not text:
        if has_media:
            await bot_api(
                "sendMessage",
                chat_id=chat_id,
                text=(
                    "To cache + stream: **forward** the post from a source channel "
                    "(keep forward header). Or `/search <title>`."
                ),
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
                "Easiest: **forward a movie** from a source channel → auto-cache.\n\n"
                "Commands:\n"
                "• `/tamil` / `/english` / `/multi` — indexed lists\n"
                "• `/cache <ch:msg>` — cache manually\n"
                "• `/cached` — list cached\n"
                "• `/index` — rebuild index\n"
                "• `/search <title>` — matched search only\n"
                "• `/channels` — source channels"
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
                "1. **Forward** a file from source channel → auto-cache\n"
                "2. Tap **🔗 Get link** only when you need the URL\n"
                "3. `/tamil` / `/english` — browse index\n"
                "4. `/search Title` — strict matches only\n"
                f"5. Inline: `{uname} title`\n"
                "6. `/index` if lists empty"
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

    if cmd == "/index":
        await bot_api("sendMessage", chat_id=chat_id, text="📇 Rebuilding media index…")
        try:
            result = await rebuild_media_index()
            await _push_index_to_worker()
            cats = build_category_bundle()
            await bot_api(
                "sendMessage",
                chat_id=chat_id,
                text=(
                    f"✅ Index ready: **{result.get('total', 0)}** files\n"
                    f"Tamil latest: {len(cats['tamil_latest'])} · "
                    f"English latest: {len(cats['english_latest'])} · "
                    f"Multi: {len(cats['multi_audio_latest'])}"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            await bot_api("sendMessage", chat_id=chat_id, text=f"❌ Index failed: `{e}`", parse_mode="Markdown")
        return

    if cmd in ("/tamil", "/english", "/multi"):
        sort = "popular" if (args and args[0].lower() in ("popular", "pop", "top")) else "latest"
        if cmd == "/multi":
            items = _filter_index(None, sort, 8, multi_only=True)
            label = f"Multi-audio ({sort})"
        elif cmd == "/tamil":
            items = _filter_index("tamil", sort, 8)
            label = f"Tamil ({sort})"
        else:
            items = _filter_index("english", sort, 8)
            label = f"English ({sort})"
        if not items:
            await bot_api(
                "sendMessage",
                chat_id=chat_id,
                text=f"📭 No **{label}** yet. Run `/index` first.",
                parse_mode="Markdown",
            )
            return
        await bot_api("sendMessage", chat_id=chat_id, text=f"🎬 **{label}** — top {len(items)}", parse_mode="Markdown")
        for item in items:
            await _bot_send_index_item(chat_id, item)
        return

    if cmd in ("/prewarm", "/cache"):
        target = " ".join(args).strip()
        if not target or ":" not in target:
            await bot_api(
                "sendMessage",
                chat_id=chat_id,
                text=(
                    "Usage: `/cache <channel_id>:<message_id>`\n"
                    "Or **forward** the file from a source channel (auto-cache).\n"
                    "Or tap **💾 Cache** on a listed title."
                ),
                parse_mode="Markdown",
            )
            return
        ch, _, mid = target.partition(":")
        await bot_api(
            "sendMessage",
            chat_id=chat_id,
            text=f"💾 Caching `{ch}:{mid}` (first ~128 MiB)…",
            parse_mode="Markdown",
        )
        ok, detail = await _request_worker_prewarm(ch.strip(), mid.strip())
        await bot_api(
            "sendMessage",
            chat_id=chat_id,
            text=("✅ " if ok else "❌ ") + detail,
            parse_mode="Markdown",
        )
        return

    if cmd == "/cached":
        data = await _request_cached_list()
        items = data.get("items") or []
        if not items:
            await bot_api(
                "sendMessage",
                chat_id=chat_id,
                text="📭 Nothing cached yet. **Forward** a file from a source channel.",
                parse_mode="Markdown",
            )
            return
        lines = []
        for it in items[:20]:
            mid = it.get("id") or "?"
            name = it.get("file_name") or mid
            mode = it.get("mode") or "?"
            mb = round((it.get("bytes_warmed") or 0) / (1024 * 1024), 1)
            lines.append(f"• `{mid}` — {name[:40]} ({mb} MiB, {mode})")
        await bot_api(
            "sendMessage",
            chat_id=chat_id,
            text=f"💾 **Cached ({data.get('total', len(items))}):**\n\n" + "\n".join(lines),
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

    # Prefer strict index matches first
    scored = []
    for i in (MEDIA_INDEX.get("items") or []):
        s = _match_score(q, i)
        if s > 0:
            scored.append((s, i))
    if not scored:
        channel_hits = await asyncio.gather(
            *[_search_channel(ch, q, limit=4) for ch in ALLOWED_CHANNELS]
        )
        for hits in channel_hits:
            for ch, m, media in hits:
                fname = getattr(media, "file_name", None) or f"file_{m.id}.mp4"
                info = parse_media_info(fname, media.file_size, channel_id=ch)
                item = {
                    "id": f"{ch}:{m.id}",
                    "channel_id": ch,
                    "message_id": m.id,
                    "file_name": fname,
                    "info": info,
                }
                s = _match_score(q, item)
                if s > 0:
                    scored.append((s, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    seen = set()
    for _, item in scored:
        ch, mid = item["channel_id"], item["message_id"]
        key = (ch, mid)
        if key in seen:
            continue
        seen.add(key)
        fname = item.get("file_name") or f"file_{mid}.mp4"
        info = item.get("info") or {}
        LINK_NAME_CACHE[(str(ch), str(mid))] = fname
        desc_parts = [p for p in [info.get("quality"), info.get("source"), info.get("size")] if p and p != "Unknown"]
        desc = " | ".join(desc_parts)
        if info.get("year"):
            desc = f"{info['year']} · {desc}"
        # No raw URL in text — open bot / Get link pattern via url button only when user picks result
        url = f"{WORKER_URL}/stream/{ch}/{mid}?name={fname}"
        results.append({
            "type": "article",
            "id": f"{ch}_{mid}",
            "title": (info.get("title") or fname)[:64],
            "description": desc[:120],
            "input_message_content": {
                "message_text": (
                    f"🎬 **{(info.get('title') or fname)[:80]}**\n"
                    f"📌 {info.get('quality', '?')} | {info.get('source', '?')} | 📦 {info.get('size', '?')}\n"
                    f"`{ch}:{mid}`\n"
                    f"Tap button for stream link."
                ),
                "parse_mode": "Markdown",
            },
            "reply_markup": _ikb([[
                {"text": "🔗 Get link", "url": url},
                {"text": "💬 Open bot", "url": f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else WORKER_URL},
            ]]),
        })
        if len(results) >= 5:
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
                    {"command": "tamil", "description": "Latest / popular Tamil (+ multi)"},
                    {"command": "english", "description": "Latest / popular English"},
                    {"command": "multi", "description": "Multi / dual audio"},
                    {"command": "cache", "description": "Cache a movie before watching"},
                    {"command": "cached", "description": "List cached movies"},
                    {"command": "prewarm", "description": "Alias for /cache"},
                    {"command": "index", "description": "Rebuild media index"},
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
                allowed_updates=["message", "inline_query", "callback_query"],
            )
            if wh.get("ok"):
                print(f"✅ Webhook set: {webhook_url}")
            else:
                print(f"❌ setWebhook failed: {wh}")
        except Exception as e:
            print(f"❌ Bot webhook startup error: {e}")
    else:
        print("⚠️  Bot commands disabled (set BOT_TOKEN to enable)")

    # Build media index after Telegram is up (non-blocking caller already)
    if tg_client and getattr(tg_client, "is_connected", False):
        try:
            await rebuild_media_index()
            await _push_index_to_worker()
        except Exception as e:
            print(f"⚠️  Startup index rebuild failed: {e}")


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
        elif "callback_query" in update:
            await _bot_handle_callback(update["callback_query"])
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
        "version": "4.4.0",
        "mode": mode,
        "bot_mode": "webhook",
        "connected": getattr(tg_client, "is_connected", False) if tg_client else False,
        "bot": bool(BOT_TOKEN),
        "bot_connected": bool(BOT_TOKEN and BOT_USERNAME),
        "bot_username": BOT_USERNAME or None,
        "bot_link": BOT_LINK or None,
        "channels": ALLOWED_CHANNELS,
        "index_total": MEDIA_INDEX.get("total") or 0,
        "index_updated_at": MEDIA_INDEX.get("updated_at") or 0,
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "4.4.0",
        "bot_mode": "webhook",
        "connected": getattr(tg_client, "is_connected", False) if tg_client else False,
        "bot": bool(BOT_TOKEN),
        "bot_connected": bool(BOT_TOKEN and BOT_USERNAME),
        "bot_username": BOT_USERNAME or None,
        "bot_link": BOT_LINK or None,
        "index_total": MEDIA_INDEX.get("total") or 0,
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
        "commands": [
            "/start", "/tamil", "/english", "/multi",
            "/cache", "/cached", "/index", "/search <title>", "/channels", "/help",
        ],
    }


def _check_index_secret(request: Request):
    if not INDEX_SECRET:
        return
    got = request.headers.get("X-Index-Secret") or request.query_params.get("secret") or ""
    if got != INDEX_SECRET:
        raise HTTPException(401, "Invalid index secret")


@app.post("/index/rebuild")
async def index_rebuild(request: Request, per_channel: int = None):
    _check_index_secret(request)
    if not tg_client:
        raise HTTPException(500, "Telegram client not connected")
    result = await rebuild_media_index(per_channel)
    await _push_index_to_worker()
    return {**result, "categories": {
        k: len(v) if isinstance(v, list) else v
        for k, v in build_category_bundle().items()
        if k != "hot_prewarm"
    }}


@app.get("/index")
async def index_get(
    language: str = None,
    sort: str = "latest",
    limit: int = 40,
    media_type: str = "movie",
    multi: str = "false",
):
    if not MEDIA_INDEX.get("items"):
        return {"updated_at": 0, "total": 0, "results": [], "hint": "POST /index/rebuild first"}
    items = _filter_index(
        language=language,
        sort=sort,
        limit=limit,
        media_type=media_type,
        multi_only=multi.lower() == "true",
    )
    return {
        "updated_at": MEDIA_INDEX.get("updated_at"),
        "total_indexed": MEDIA_INDEX.get("total"),
        "total": len(items),
        "results": items,
    }


@app.get("/index/categories")
async def index_categories(limit: int = 40):
    if not MEDIA_INDEX.get("items"):
        return {"updated_at": 0, "total_indexed": 0, "hint": "POST /index/rebuild first"}
    return build_category_bundle(limit)


@app.get("/index/full")
async def index_full(request: Request):
    """Full index dump for Worker/R2 sync."""
    _check_index_secret(request)
    return MEDIA_INDEX


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

    # Prefer indexed results when index warm
    qlow = q.lower().strip()
    if MEDIA_INDEX.get("items") and not channel_id:
        indexed = []
        seen = set()
        for item in MEDIA_INDEX["items"]:
            fname = (item.get("file_name") or "").lower()
            title = ((item.get("info") or {}).get("title") or "").lower()
            if qlow in fname or qlow in title:
                key = (item["channel_id"], item["message_id"])
                if key in seen:
                    continue
                seen.add(key)
                indexed.append({
                    "channel_id": item["channel_id"],
                    "message_id": item["message_id"],
                    "file_name": item["file_name"],
                    "file_size": item.get("file_size"),
                    "mime_type": item.get("mime_type", "video/mp4"),
                    "info": item.get("info") or {},
                })
            if len(indexed) >= 40:
                break
        if indexed:
            body = {"query": q, "total": len(indexed), "results": indexed, "source": "index"}
            SEARCH_CACHE[cache_key] = (now, body)
            return body

    # Telegram search is literal. "obsession 2026" misses "Obsession (2025)".
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
                info = parse_media_info(fname, media.file_size, channel_id=ch)
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
    body = {"query": q, "total": len(results), "results": results, "source": "telegram"}
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
