# Telegram Streamer & Stremio Addon

Direct, high-speed streamable link generator and Stremio v3 Addon powered by Telegram Source Channels, Cloudflare Edge Caching, and Hugging Face MTProto streaming backend.

## Features
- **Byte-Range Media Streaming (`206 Partial Content`)**: Instant seek and scrub support for video players (Stremio, Infuse, VLC, Web browsers).
- **Unlimited File Sizes (Up to 2GB/4GB)**: Powered by Hugging Face / MTProto fast streaming backend bypassing the 20MB standard Bot API limit.
- **Stremio Addon Protocol v3**: Built-in `/manifest.json` and `/stream` endpoints with 1-click `stremio://` installer.
- **Whitelisted Source Channels**: Protects bandwidth by restricting access to specified channel IDs (`-1003967652604`, `-1002502061360`, `-1003916531716`).

---

## Deployment Guide

### 1. Deploy Free MTProto Backend (Hugging Face Spaces)
1. Create a free Docker Space at [huggingface.co/new-space](https://huggingface.co/new-space).
2. Upload the contents of the `backend/` directory (`Dockerfile`, `main.py`, `requirements.txt`).
3. Add Environment Secrets in Space Settings:
   - `API_ID`
   - `API_HASH`
   - `BOT_TOKEN`
4. Copy your Space URL (e.g. `https://USERNAME-tg-stream-backend.hf.space`).

### 2. Deploy Cloudflare Worker (Frontend & Addon)
1. Deploy `worker.js` to Cloudflare Workers (or use `npx wrangler deploy`).
2. Set Environment Variables:
   - `TELEGRAM_API_URL` = `https://USERNAME-tg-stream-backend.hf.space`
   - `TELEGRAM_BOT_TOKEN` = `YOUR_BOT_TOKEN`
   - `ALLOWED_CHANNELS` = `-1003967652604,-1002502061360,-1003916531716`

---

## License
MIT
