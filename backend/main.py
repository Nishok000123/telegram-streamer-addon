import os
import re
import asyncio
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from hydrogram import Client, filters
from hydrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, InlineQuery, InlineQueryResultArticle, InputTextMessageContent
from hydrogram.errors import FloodWait, RPCError, ChannelPrivate, ChatAdminRequired, UserNotParticipant, MessageIdInvalid

API_ID_RAW = os.environ.get("API_ID", "").strip()
API_ID = int(API_ID_RAW) if API_ID_RAW.isdigit() else 0
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WORKER_URL = os.environ.get("WORKER_URL", "https://telegram-streamer-addon.nishokroshan076632.workers.dev").rstrip('/')

ALLOWED_CHANNELS = [c.strip() for c in os.environ.get("ALLOWED_CHANNELS", "-1003967652604,-1002502061360,-1003916531716").split(",") if c.strip()]

app = FastAPI(title="Telegram Streamer MTProto Engine & Bot Interface", version="3.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

tg_client = None
if API_ID > 0 and API_HASH and BOT_TOKEN:
    try:
        tg_client = Client(
            "tg_streamer_engine",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            in_memory=True
        )
    except Exception as init_err:
        print(f"❌ Hydrogram Client Init Error: {init_err}")

def parse_media_info(file_name: str, file_size: int):
    name_lower = file_name.lower()
    quality = "720p"
    if "2160p" in name_lower or "4k" in name_lower:
        quality = "4K UHD"
    elif "1080p" in name_lower:
        quality = "1080p FHD"
    elif "720p" in name_lower:
        quality = "720p HD"
    elif "480p" in name_lower:
        quality = "480p SD"

    codec = "HEVC" if "hevc" in name_lower or "x265" in name_lower or "h265" in name_lower else "H.264"
    audio = "Multi-Audio" if "dual" in name_lower or "multi" in name_lower or "hindi" in name_lower or "tamil" in name_lower else "Standard"

    size_mb = round(file_size / (1024 * 1024), 2)
    size_str = f"{size_mb} MB" if size_mb < 1024 else f"{round(size_mb / 1024, 2)} GB"

    return {
        "quality": quality,
        "codec": codec,
        "audio": audio,
        "formatted_size": size_str,
        "file_name": file_name
    }

# ================= TELEGRAM BOT COMMAND HANDLERS =================

if tg_client:
    @tg_client.on_message(filters.command("start"))
    async def start_command(client: Client, message: Message):
        welcome_text = (
            "✨ **Welcome to Telegram Movie Streamer & Stremio Bot** ✨\n\n"
            "I can generate high-speed direct streamable links, Stremio addon streams, and instant download URLs for movies stored in source channels!\n\n"
            "🔍 **Commands:**\n"
            "• `/search <movie name>` — Search across channels\n"
            "• `/channels` — List source channels\n"
            "• `/help` — How to use this bot\n\n"
            "⚠️ **IMPORTANT SETUP REQUIREMENT:**\n"
            "Make sure to add this bot as **Admin** in your source channels so it can access your files!"
        )
        buttons = InlineKeyboardMarkup([
          [
            InlineKeyboardButton("🎬 Stremio Addon", url=f"{WORKER_URL}/manifest.json"),
            InlineKeyboardButton("🌐 Web Dashboard", url=WORKER_URL)
          ],
          [
            InlineKeyboardButton("🔍 Search Movies", switch_inline_query_current_chat=""),
            InlineKeyboardButton("📢 Source Channels", callback_data="show_channels")
          ]
        ])
        await message.reply_text(welcome_text, reply_markup=buttons, disable_web_page_preview=True)

    @tg_client.on_message(filters.command("help"))
    async def help_command(client: Client, message: Message):
        help_text = (
            "📖 **How to Use Telegram Movie Streamer Bot:**\n\n"
            "1️⃣ **Add Bot to Channel:** Add `@yourbotname` as an **Administrator** in your source channels.\n"
            "2️⃣ **Search Movies:** Type `/search Avatar` or use inline search by typing `@botusername Avatar` in any chat.\n"
            "3️⃣ **Forward Video:** Forward any movie file from allowed channels to get direct play & download links.\n"
            "4️⃣ **Stremio Addon:** Copy the Stremio Manifest link from `/start` menu and add it into Stremio app."
        )
        await message.reply_text(help_text)

    @tg_client.on_message(filters.command("channels"))
    async def channels_command(client: Client, message: Message):
        ch_list = "\n".join([f"• `{ch}`" for ch in ALLOWED_CHANNELS])
        await message.reply_text(f"📢 **Whitelisted Source Channels:**\n\n{ch_list}\n\n⚠️ *Make sure the bot is an Admin in all these channels!*")

    @tg_client.on_message(filters.command("search"))
    async def search_command(client: Client, message: Message):
        query = " ".join(message.command[1:]).strip()
        if not query:
            await message.reply_text("⚠️ **Usage:** `/search <movie name>`\nExample: `/search Avatar`")
            return

        msg = await message.reply_text(f"🔍 Searching channels for **{query}**...")
        found = False

        for ch in ALLOWED_CHANNELS:
            try:
                chat_id = int(ch) if ch.startswith("-100") or ch.isdigit() else ch
                async for m in client.search_messages(chat_id, query=query, limit=5):
                    media = m.video or m.document or m.audio
                    if media:
                        found = True
                        file_name = getattr(media, "file_name", None) or f"video_{m.id}.mp4"
                        info = parse_media_info(file_name, media.file_size)
                        stream_url = f"{WORKER_URL}/stream/{ch}/{m.id}?name={file_name}"
                        dl_url = f"{WORKER_URL}/dl/{ch}/{m.id}?name={file_name}"

                        caption = (
                            f"🎬 **{file_name}**\n\n"
                            f"📌 **Quality:** `{info['quality']}`\n"
                            f"📦 **Size:** `{info['formatted_size']}`\n"
                            f"🎧 **Audio:** `{info['audio']}`\n"
                            f"⚡ **Codec:** `{info['codec']}`"
                        )
                        kb = InlineKeyboardMarkup([
                          [
                            InlineKeyboardButton("▶️ Direct Stream", url=stream_url),
                            InlineKeyboardButton("📥 Download", url=dl_url)
                          ],
                          [
                            InlineKeyboardButton("🍿 Play in Stremio", url=f"stremio://{WORKER_URL.replace('https://', '').replace('http://', '')}/stream/movie/tg:{ch}:{m.id}.json")
                          ]
                        ])
                        await message.reply_text(caption, reply_markup=kb)
            except ChannelPrivate:
                print(f"⚠️ Bot is not in channel {ch} or channel is private. Add bot as Admin.")
            except Exception as e:
                print(f"Error searching {ch}: {e}")

        if not found:
            await msg.edit_text(f"❌ No movies found matching **{query}** in source channels.\n\n💡 *Note: Ensure the bot is added as Admin in your channels!*")
        else:
            await msg.delete()

    @tg_client.on_message(filters.media | filters.forwarded)
    async def auto_link_generator(client: Client, message: Message):
        media = message.video or message.document or message.audio
        if not media:
            return

        chat_id = str(message.chat.id)
        forward_chat = str(message.forward_from_chat.id) if message.forward_from_chat else ""

        if chat_id in ALLOWED_CHANNELS or forward_chat in ALLOWED_CHANNELS:
            target_ch = forward_chat if forward_chat in ALLOWED_CHANNELS else chat_id
            msg_id = message.forward_from_message_id if message.forward_from_message_id else message.id
            file_name = getattr(media, "file_name", None) or "video.mp4"
            info = parse_media_info(file_name, media.file_size)

            stream_url = f"{WORKER_URL}/stream/{target_ch}/{msg_id}?name={file_name}"
            dl_url = f"{WORKER_URL}/dl/{target_ch}/{msg_id}?name={file_name}"

            caption = (
                f"⚡ **Direct Streamable Link Generated!**\n\n"
                f"🎬 **File:** `{file_name}`\n"
                f"📊 **Size:** `{info['formatted_size']}` | **Quality:** `{info['quality']}`"
            )
            kb = InlineKeyboardMarkup([
              [
                InlineKeyboardButton("▶️ Direct Stream", url=stream_url),
                InlineKeyboardButton("📥 Download", url=dl_url)
              ]
            ])
            await message.reply_text(caption, reply_markup=kb)

    @tg_client.on_inline_query()
    async def inline_search(client: Client, inline_query: InlineQuery):
        query = inline_query.query.strip()
        if not query or len(query) < 2:
            return

        results = []
        for ch in ALLOWED_CHANNELS:
            try:
                chat_id = int(ch) if ch.startswith("-100") or ch.isdigit() else ch
                async for m in client.search_messages(chat_id, query=query, limit=5):
                    media = m.video or m.document or message.audio
                    if media:
                        file_name = getattr(media, "file_name", None) or f"video_{m.id}.mp4"
                        info = parse_media_info(file_name, media.file_size)
                        stream_url = f"{WORKER_URL}/stream/{ch}/{m.id}?name={file_name}"

                        results.append(
                            InlineQueryResultArticle(
                                title=file_name,
                                description=f"Quality: {info['quality']} | Size: {info['formatted_size']}",
                                input_message_content=InputTextMessageContent(
                                    f"🎬 **{file_name}**\n\n"
                                    f"📌 **Quality:** `{info['quality']}`\n"
                                    f"📦 **Size:** `{info['formatted_size']}`\n\n"
                                    f"🔗 **Stream Link:** {stream_url}"
                                ),
                                reply_markup=InlineKeyboardMarkup([
                                  [InlineKeyboardButton("▶️ Stream Now", url=stream_url)]
                                ])
                            )
                        )
            except Exception as e:
                print(f"Inline search error: {e}")

        await inline_query.answer(results, cache_time=300)


# ================= FASTAPI WEB API ENDPOINTS =================

@app.on_event("startup")
async def startup():
    print("🚀 Starting Telegram MTProto Engine & Bot Handlers...")
    if tg_client:
        try:
            await tg_client.start()
            print("✅ Telegram Bot Connected & Handlers Active!")
        except Exception as e:
            print(f"⚠️ Telegram Client Startup Error: {e}")

@app.on_event("shutdown")
async def shutdown():
    if tg_client and getattr(tg_client, "is_connected", False):
        await tg_client.stop()

@app.get("/")
def health_check():
    return {
        "status": "online",
        "engine": "Hydrogram MTProto Direct Streamer & Telegram Bot Interface",
        "version": "3.2.0",
        "worker_url": WORKER_URL,
        "credentials_configured": tg_client is not None,
        "connected": tg_client.is_connected if (tg_client and hasattr(tg_client, 'is_connected')) else False,
        "channels": ALLOWED_CHANNELS,
        "instructions": "Ensure @yourbot is added as Administrator in all source channels."
    }

@app.get("/health")
def health():
    return {
        "status": "ok",
        "connected": tg_client.is_connected if (tg_client and hasattr(tg_client, 'is_connected')) else False
    }

@app.get("/search")
async def search_channels(q: str = Query(..., min_length=2), channel_id: str = None):
    if not tg_client:
        raise HTTPException(status_code=500, detail="Telegram credentials missing or invalid.")

    results = []
    target_channels = [channel_id] if channel_id else ALLOWED_CHANNELS

    for ch in target_channels:
        try:
            chat_id = int(ch) if ch.startswith("-100") or ch.isdigit() else ch
            async for message in tg_client.search_messages(chat_id, query=q, limit=20):
                media = message.video or message.document or message.audio
                if media:
                    file_name = getattr(media, "file_name", None) or f"video_{message.id}.mp4"
                    info = parse_media_info(file_name, media.file_size)
                    results.append({
                        "channel_id": str(ch),
                        "message_id": message.id,
                        "file_name": file_name,
                        "file_size": media.file_size,
                        "mime_type": getattr(media, "mime_type", "video/mp4"),
                        "info": info
                    })
        except ChannelPrivate:
            print(f"⚠️ ChannelPrivate error for {ch}: Bot is not an Admin in channel.")
        except Exception as e:
            print(f"Error searching channel {ch}: {e}")

    return {"query": q, "total": len(results), "results": results}

@app.get("/recent")
async def get_recent_media(channel_id: str = None, limit: int = 20):
    if not tg_client:
        return {"total": 0, "items": [], "warning": "Credentials missing or invalid"}

    results = []
    target_channels = [channel_id] if channel_id else ALLOWED_CHANNELS

    for ch in target_channels:
        try:
            chat_id = int(ch) if ch.startswith("-100") or ch.isdigit() else ch
            async for message in tg_client.get_chat_history(chat_id, limit=limit):
                media = message.video or message.document or message.audio
                if media:
                    file_name = getattr(media, "file_name", None) or f"media_{message.id}.mp4"
                    info = parse_media_info(file_name, media.file_size)
                    results.append({
                        "channel_id": str(ch),
                        "message_id": message.id,
                        "file_name": file_name,
                        "file_size": media.file_size,
                        "mime_type": getattr(media, "mime_type", "video/mp4"),
                        "info": info
                    })
        except Exception as e:
            print(f"Error fetching history for channel {ch}: {e}")

    return {"total": len(results), "items": results}

@app.get("/stream/{channel_id}/{message_id}")
async def stream_media(channel_id: str, message_id: int, request: Request):
    if not tg_client:
        raise HTTPException(status_code=500, detail="Telegram credentials missing or invalid.")

    retry_count = 0
    max_retries = 3

    while retry_count < max_retries:
        try:
            chat_id = int(channel_id) if channel_id.startswith("-100") or channel_id.isdigit() else chat_id
            
            try:
                message = await tg_client.get_messages(chat_id, message_id)
            except ChannelPrivate:
                raise HTTPException(status_code=403, detail=f"Bot is not an Admin in channel {channel_id}. Add the bot as Admin in Telegram.")
            except MessageIdInvalid:
                raise HTTPException(status_code=404, detail=f"Message ID {message_id} not found in channel {channel_id}.")

            if not message:
                raise HTTPException(status_code=404, detail="Message not found")

            media = message.video or message.document or message.audio
            if not media:
                raise HTTPException(status_code=404, detail="No streamable video or audio file found in this message")

            file_size = media.file_size
            mime_type = getattr(media, "mime_type", None) or "video/mp4"

            range_header = request.headers.get("range")
            start = 0
            end = file_size - 1

            if range_header:
                bytes_range = range_header.replace("bytes=", "").split("-")
                start = int(bytes_range[0])
                if len(bytes_range) > 1 and bytes_range[1]:
                    end = int(bytes_range[1])

            async def media_generator():
                try:
                    async for chunk in tg_client.stream_media(message, offset=start, limit=(end - start + 1)):
                        yield chunk
                except Exception as stream_err:
                    print(f"Streaming error chunk: {stream_err}")

            headers = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(end - start + 1),
                "Content-Type": mime_type,
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Cache-Control": "public, max-age=31536000, immutable",
            }

            return StreamingResponse(
                media_generator(),
                status_code=206 if range_header else 200,
                headers=headers
            )

        except FloodWait as fw:
            print(f"⚠️ Telegram Rate Limit FloodWait: Sleeping for {fw.value}s")
            await asyncio.sleep(fw.value)
            retry_count += 1
        except HTTPException:
            raise
        except Exception as e:
            print(f"Error serving stream {channel_id}/{message_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    raise HTTPException(status_code=429, detail="Telegram Rate Limit Exceeded.")
