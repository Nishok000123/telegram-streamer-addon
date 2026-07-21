import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from hydrogram import Client

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = FastAPI()

tg_client = Client(
    "tg_streamer",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

@app.on_event("startup")
async def startup():
    await tg_client.start()

@app.on_event("shutdown")
async def shutdown():
    await tg_client.stop()

@app.get("/")
def home():
    return {"status": "online", "service": "Telegram Direct Streamer MTProto Backend"}

@app.get("/stream/{channel_id}/{message_id}")
async def stream_media(channel_id: str, message_id: int, request: Request):
    try:
        chat_id = int(channel_id) if channel_id.startswith("-100") or channel_id.isdigit() else channel_id
        message = await tg_client.get_messages(chat_id, message_id)
        
        media = message.video or message.document or message.audio
        if not media:
            raise HTTPException(status_code=404, detail="No media found in message")
        
        file_size = media.file_size
        mime_type = media.mime_type or "video/mp4"
        
        range_header = request.headers.get("range")
        start = 0
        end = file_size - 1
        
        if range_header:
            bytes_range = range_header.replace("bytes=", "").split("-")
            start = int(bytes_range[0])
            if len(bytes_range) > 1 and bytes_range[1]:
                end = int(bytes_range[1])
                
        async def media_generator():
            async for chunk in tg_client.stream_media(message, offset=start, limit=(end - start + 1)):
                yield chunk

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Type": mime_type,
            "Access-Control-Allow-Origin": "*",
        }
        
        return StreamingResponse(
            media_generator(),
            status_code=206 if range_header else 200,
            headers=headers
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
