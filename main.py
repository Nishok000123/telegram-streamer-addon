import os
import asyncio
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, Message,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent
)
from pyrogram.errors import FloodWait, ChannelPrivate, MessageIdInvalid


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
WORKER_URL = os.environ.get("WORKER_URL", "https://telegram-streamer-addon.nishokroshan076632.workers.dev").rstrip("/")
ALLOWED_CHANNELS = [
    c.strip()
    for c in os.environ.get("ALLOWED_CHANNELS", "-1003967652604,-1002502061360,-1003916531716").split(",")
    if c.strip()
]

app = FastAPI(title="Telegram Streamer MTProto Engine", version="4.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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


def parse_media_info(file_name: str, file_size: int) -> dict:
    n = file_name.lower()
    quality = "4K UHD" if ("2160p" in n or "4k" in n) else \
              "1080p FHD" if "1080p" in n else \
              "720p HD" if "720p" in n else \
              "480p SD" if "480p" in n else "Unknown"
    codec = "HEVC" if ("hevc" in n or "x265" in n or "h265" in n) else "H.264"
    audio = "Multi-Audio" if ("dual" in n or "multi" in n or "hindi" in n or "tamil" in n) else "Standard"
    size_mb = round(file_size / (1024 * 1024), 2)
    size_str = f"{size_mb} MB" if size_mb < 1024 else f"{round(size_mb/1024, 2)} GB"
    return {"quality": quality, "codec": codec, "audio": audio, "size": size_str, "file_name": file_name}


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
                    info = parse_media_info(fname, media.file_size)
                    stream_url = f"{WORKER_URL}/stream/{ch}/{m.id}?name={fname}"
                    dl_url = f"{WORKER_URL}/dl/{ch}/{m.id}?name={fname}"
                    stremio_url = f"stremio://{WORKER_URL.replace('https://','')}/stream/movie/tg:{ch}:{m.id}.json"

                    await msg.reply_text(
                        f"🎬 **{fname}**\n"
                        f"📌 Quality: `{info['quality']}` | 📦 Size: `{info['size']}`\n"
                        f"🎧 Audio: `{info['audio']}` | ⚡ Codec: `{info['codec']}`",
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
        info = parse_media_info(fname, media.file_size)
        stream_url = f"{WORKER_URL}/stream/{target}/{mid}?name={fname}"
        dl_url = f"{WORKER_URL}/dl/{target}/{mid}?name={fname}"
        await msg.reply_text(
            f"⚡ **Stream link ready!**\n"
            f"🎬 `{fname}`\n"
            f"📦 {info['size']} | 📌 {info['quality']}",
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
                    info = parse_media_info(fname, media.file_size)
                    url = f"{WORKER_URL}/stream/{ch}/{m.id}?name={fname}"
                    results.append(InlineQueryResultArticle(
                        title=fname,
                        description=f"{info['quality']} | {info['size']}",
                        input_message_content=InputTextMessageContent(
                            f"🎬 **{fname}**\n📌 {info['quality']} | 📦 {info['size']}\n🔗 {url}"
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
async def search_api(q: str = Query(..., min_length=2), channel_id: str = None):
    if not tg_client:
        raise HTTPException(500, "No Telegram client configured.")
    results = []
    targets = [channel_id] if channel_id else ALLOWED_CHANNELS
    for ch in targets:
        try:
            async for msg in tg_client.search_messages(int(ch), query=q, limit=20):
                media = msg.video or msg.document or msg.audio
                if not media:
                    continue
                fname = getattr(media, "file_name", None) or f"file_{msg.id}.mp4"
                results.append({
                    "channel_id": ch,
                    "message_id": msg.id,
                    "file_name": fname,
                    "file_size": media.file_size,
                    "mime_type": getattr(media, "mime_type", "video/mp4"),
                    "info": parse_media_info(fname, media.file_size),
                })
        except Exception as e:
            print(f"Search error {ch}: {e}")
    return {"query": q, "total": len(results), "results": results}

@app.get("/recent")
async def recent_api(channel_id: str = None, limit: int = 20):
    if not tg_client:
        return {"total": 0, "items": []}
    results = []
    targets = [channel_id] if channel_id else ALLOWED_CHANNELS
    for ch in targets:
        try:
            async for msg in tg_client.get_chat_history(int(ch), limit=limit):
                media = msg.video or msg.document or msg.audio
                if not media:
                    continue
                fname = getattr(media, "file_name", None) or f"file_{msg.id}.mp4"
                results.append({
                    "channel_id": ch,
                    "message_id": msg.id,
                    "file_name": fname,
                    "file_size": media.file_size,
                    "mime_type": getattr(media, "mime_type", "video/mp4"),
                    "info": parse_media_info(fname, media.file_size),
                })
        except Exception as e:
            print(f"Recent error {ch}: {e}")
    return {"total": len(results), "items": results}

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
            mime = getattr(media, "mime_type", "video/mp4")
            range_hdr = request.headers.get("range")
            start, end = 0, file_size - 1
            if range_hdr:
                parts = range_hdr.replace("bytes=", "").split("-")
                start = int(parts[0])
                if len(parts) > 1 and parts[1]:
                    end = int(parts[1])

            async def gen():
                async for chunk in tg_client.stream_media(msg, offset=start, limit=(end - start + 1)):
                    yield chunk

            return StreamingResponse(gen(), status_code=206 if range_hdr else 200, headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(end - start + 1),
                "Content-Type": mime,
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=31536000",
            })
        except FloodWait as fw:
            await asyncio.sleep(fw.value)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, str(e))
    raise HTTPException(429, "Rate limited by Telegram.")
