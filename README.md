# Telegram Streamer & Stremio Addon

Stream videos from whitelisted Telegram channels into Stremio / VLC / browsers via byte-range links.

**Stack today:** Telegram MTProto (Pyrogram) on **Koyeb** + **Cloudflare Worker** (Stremio addon, Cache API, optional KV). No Hugging Face required. No R2 / no payment card required for the free path.

## Features

- **Byte-range streaming (`206`)** — seek/scrub in Stremio, Infuse, VLC, browsers
- **Large files (2GB / 4GB)** — MTProto user session, not Bot API 20MB limit
- **Stremio addon** — `/manifest.json`, catalogs, `/stream` sources
- **Media index** — scan channels once; browse Tamil / English / multi-audio without live search every time
- **Bot** — `/tamil`, `/english`, `/multi`, `/cache`, `/cached`, `/search`, `/index`
- **Pre-watch cache** — `/cache` warms ~128 MiB into Cloudflare Cache API; list stays in free Workers KV
- **Channel allowlist** — only configured channel IDs are streamable

Default channels:

`-1003916531716`, `-1002502061360`, `-1003967652604`, `-1002708448330`

## Honest limits

- First play of an uncached title is limited by **Telegram** speed (one download lock per backend client — not multi-session parallel)
- Cache API can evict under pressure; `/cached` list in KV still stays and re-warms on next play
- Cloudflare Worker is mainly addon + edge segment cache — not a full CDN of your whole library

## Deploy

### 1. Backend (Koyeb / Docker)

Env (Koyeb):

| Var | Notes |
|-----|--------|
| `API_ID` / `API_HASH` | [my.telegram.org](https://my.telegram.org) |
| `SESSION_STRING` | Preferred (full channel access) |
| `BOT_TOKEN` | Bot commands via webhook |
| `BACKEND_URL` | Public Koyeb URL |
| `WORKER_URL` | Cloudflare Worker URL |
| `ALLOWED_CHANNELS` | Comma-separated channel IDs |
| `INDEX_SECRET` | Optional; protect `/index/rebuild` |

```bash
# local
docker compose up --build
# or: uvicorn main:app --host 0.0.0.0 --port 8000
```

Health: `GET /` · `GET /health` · Bot: `GET /bot`

### 2. Cloudflare Worker

1. Deploy `worker.js` (`npx wrangler deploy` or paste in dashboard)
2. Vars: `TELEGRAM_API_URL` = Koyeb URL, `ALLOWED_CHANNELS` = same list
3. Optional free **KV** binding name: `MEDIA_META` (keeps `/cached` list)
4. **Do not need R2** (R2 requires a payment method)

```bash
npx wrangler deploy
```

Worker URL example: `https://telegram-streamer-addon.<you>.workers.dev`

Install in Stremio: open `/manifest.json` → install addon.

### 3. First-time use

1. Merge/deploy backend → wait for Telegram connect  
2. In bot: `/index`  
3. `/tamil` or `/english` → tap **💾 Cache** before watch (optional)  
4. **Stream** or play from Stremio catalogs  

## Useful API

| Endpoint | Purpose |
|----------|---------|
| `POST /index/rebuild` | Rescan channels into index |
| `GET /index/categories` | Tamil/English/multi lists |
| `GET /search?q=` | Search (index first, else Telegram) |
| `GET /stream/{channel}/{msg}` | Byte-range media |

## License

MIT
