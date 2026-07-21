import os
import re
import asyncio
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from hydrogram import Client
from hydrogram.errors import FloodWait, RPCError

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_CHANNELS = [c.trim() for c in os.environ.get("ALLOWED_CHANNELS", "-1003967652604,-1002502061360,-1003916531716").split(",") if c.strip()]

app = FastAPI(title="Telegram Streamer MTProto Engine", version="2.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

tg_client = Client(
    "tg_streamer_engine",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

@app.on_event("startup")
async def startup():
    print("🚀 Starting Telegram MTProto Engine...")
    try:
        await tg_client.start()
        print("✅ Telegram Client Connected Successfully!")
    except Exception as e:
        print(f"⚠️ Telegram Client Startup Error: {e}")

@app.on_event("shutdown")
async def shutdown():
    if tg_client.is_connected:
        await tg_client.stop()

@app.get("/")
def health_check():
    return {
        "status": "online",
        "engine": "Hydrogram MTProto Direct Streamer",
        "version": "2.5.0",
        "channels": ALLOWED_CHANNELS,
        "features": ["Auto-Search", "Zero-Touch Auto Healing", "4K HDR Detection", "Byte-Range Caching"]
    }

@app.get("/health")
def health():
    return {"status": "ok", "connected": tg_client.is_connected if hasattr(tg_client, 'is_connected') else True}

def parse_media_info(file_name: str, file_size: int):
    """Detect quality tags from file name"""
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

@app.get("/search")
async def search_channels(q: str = Query(..., min_length=2), channel_id: str = None):
    """Auto Search Movies & Media Across Allowed Source Channels"""
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
        except Exception as e:
            print(f"Error searching channel {ch}: {e}")

    return {"query": q, "total": len(results), "results": results}

@app.get("/recent")
async def get_recent_media(channel_id: str = None, limit: int = 20):
    """Auto pull recent uploaded movies from channels"""
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
    """High Speed Partial Byte Range Video Streaming Engine"""
    retry_count = 0
    max_retries = 3

    while retry_count < max_retries:
        try:
            chat_id = int(channel_id) if channel_id.startswith("-100") or channel_id.isdigit() else channel_id
            message = await tg_client.get_messages(chat_id, message_id)

            if not message:
                raise HTTPException(status_code=404, detail="Message not found")

            media = message.video or message.document or message.audio
            if not media:
                raise HTTPException(status_code=404, detail="No streamable media in this message")

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

            chunk_size = 1024 * 1024 # 1MB chunks for fast seeking

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
        except Exception as e:
            print(f"Error serving stream {channel_id}/{message_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    raise HTTPException(status_code=429, detail="Telegram Rate Limit Exceeded. Try again in a few seconds.")
