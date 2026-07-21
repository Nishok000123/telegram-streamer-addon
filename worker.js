/**
 * Cloudflare Worker: Enterprise Telegram Direct Stream Generator & Stremio Addon
 * Optimized for media streaming, edge byte-range caching, Stremio protocol v3,
 * and custom Telegram Bot API / MTProto backend support.
 */

const MIME_TYPES = {
  mp4: 'video/mp4',
  mkv: 'video/x-matroska',
  webm: 'video/webm',
  avi: 'video/x-msvideo',
  mov: 'video/quicktime',
  m4v: 'video/x-m4v',
  ts: 'video/mp2t',
  flv: 'video/x-flv',
  mp3: 'audio/mpeg',
  flac: 'audio/flac',
  m4a: 'audio/mp4',
  ogg: 'audio/ogg',
  aac: 'audio/aac',
};

const DEFAULT_CHANNELS = ['-1003967652604', '-1002502061360', '-1003916531716'];

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const origin = url.origin;

    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type, Range, Authorization',
          'Access-Control-Max-Age': '86400',
        },
      });
    }

    const path = url.pathname;

    if (path === '/manifest.json') {
      return handleStremioManifest(origin);
    }

    if (path.startsWith('/stream/') && path.endsWith('.json')) {
      return handleStremioStreamRequest(path, url, env, origin);
    }

    if (path === '/' || path === '/configure') {
      return handleDashboardUI(origin, env);
    }

    if (path.startsWith('/stream/')) {
      return handleMediaStream(request, env, ctx, url);
    }

    if (path.startsWith('/dl/')) {
      return handleMediaStream(request, env, ctx, url, true);
    }

    return new Response('Not Found', { status: 404 });
  },
};

async function handleMediaStream(request, env, ctx, url, forceDownload = false) {
  const BOT_TOKEN = env.TELEGRAM_BOT_TOKEN;
  const API_BASE = (env.TELEGRAM_API_URL || 'https://api.telegram.org').replace(/\/$/, '');

  const parts = url.pathname.split('/').filter(Boolean);
  let channelId = null;
  let fileOrMsgId = null;

  if (parts.length >= 3) {
    channelId = parts[1];
    fileOrMsgId = parts[2];
  } else if (parts.length === 2) {
    fileOrMsgId = parts[1];
  }

  const allowedChannels = env.ALLOWED_CHANNELS
    ? env.ALLOWED_CHANNELS.split(',').map((c) => c.trim())
    : DEFAULT_CHANNELS;

  if (channelId && !allowedChannels.includes(channelId)) {
    return new Response(`Forbidden: Channel ${channelId} is not in allowed list.`, { status: 403 });
  }

  let fileId = url.searchParams.get('file_id') || fileOrMsgId;
  const fileName = url.searchParams.get('name') || 'video.mp4';
  const ext = fileName.split('.').pop().toLowerCase();
  const mimeType = MIME_TYPES[ext] || 'video/mp4';

  if (!fileId) {
    return new Response('Invalid request: Missing file_id or message_id', { status: 400 });
  }

  const cache = caches.default;
  const cacheKey = new Request(request.url, {
    headers: request.headers,
    method: 'GET',
  });

  let cachedResponse = await cache.match(cacheKey);
  if (cachedResponse) {
    const responseHeaders = new Headers(cachedResponse.headers);
    responseHeaders.set('CF-Cache-Status', 'HIT');
    return new Response(cachedResponse.body, {
      status: cachedResponse.status,
      statusText: cachedResponse.statusText,
      headers: responseHeaders,
    });
  }

  try {
    let downloadUrl = '';
    
    // Check if using Hugging Face / Custom Fast API MTProto Backend
    if (API_BASE.includes('hf.space') || API_BASE.includes('koyeb') || API_BASE.includes('render') || !API_BASE.includes('api.telegram.org')) {
      downloadUrl = `${API_BASE}/stream/${channelId || '-1003967652604'}/${fileId}`;
    } else {
      if (!BOT_TOKEN) {
        return new Response('Error: TELEGRAM_BOT_TOKEN environment variable is missing.', { status: 500 });
      }
      const fileInfoUrl = `${API_BASE}/bot${BOT_TOKEN}/getFile?file_id=${encodeURIComponent(fileId)}`;
      const fileInfoRes = await fetch(fileInfoUrl, {
        headers: { 'User-Agent': 'Cloudflare-Telegram-Proxy/2.0' },
      });

      if (!fileInfoRes.ok) {
        return new Response(`Telegram API Error: ${await fileInfoRes.text()}`, { status: fileInfoRes.status });
      }

      const fileInfoJson = await fileInfoRes.json();
      if (!fileInfoJson.ok || !fileInfoJson.result || !fileInfoJson.result.file_path) {
        return new Response(`Telegram File Not Found: ${JSON.stringify(fileInfoJson)}`, { status: 404 });
      }

      downloadUrl = `${API_BASE}/file/bot${BOT_TOKEN}/${fileInfoJson.result.file_path}`;
    }

    const reqHeaders = new Headers();
    const rangeHeader = request.headers.get('Range');
    if (rangeHeader) reqHeaders.set('Range', rangeHeader);

    const tgResponse = await fetch(downloadUrl, {
      method: request.method,
      headers: reqHeaders,
    });

    if (!tgResponse.ok && tgResponse.status !== 206) {
      return new Response(`Failed to fetch media: HTTP ${tgResponse.status}`, { status: tgResponse.status });
    }

    const responseHeaders = new Headers({
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
      'Accept-Ranges': 'bytes',
      'Content-Type': mimeType,
      'Cache-Control': 'public, max-age=31536000, immutable',
      'CF-Cache-Status': 'MISS',
    });

    if (forceDownload) {
      responseHeaders.set('Content-Disposition', `attachment; filename="${encodeURIComponent(fileName)}"`);
    } else {
      responseHeaders.set('Content-Disposition', `inline; filename="${encodeURIComponent(fileName)}"`);
    }

    if (tgResponse.headers.has('Content-Length')) {
      responseHeaders.set('Content-Length', tgResponse.headers.get('Content-Length'));
    }
    if (tgResponse.headers.has('Content-Range')) {
      responseHeaders.set('Content-Range', tgResponse.headers.get('Content-Range'));
    }

    const status = tgResponse.status === 206 || rangeHeader ? 206 : 200;
    const streamResponse = new Response(tgResponse.body, {
      status,
      statusText: status === 206 ? 'Partial Content' : 'OK',
      headers: responseHeaders,
    });

    ctx.waitUntil(cache.put(cacheKey, streamResponse.clone()));
    return streamResponse;

  } catch (err) {
    return new Response(`Streaming Worker Exception: ${err.message}`, { status: 500 });
  }
}

function handleStremioManifest(origin) {
  const manifest = {
    id: 'org.telegram.direct.streaming.addon',
    version: '1.0.0',
    name: 'Telegram Media Direct Streamer',
    description: 'Direct high-speed video streaming addon backed by Telegram Source Channels & Cloudflare Edge Caching.',
    resources: ['stream'],
    types: ['movie', 'series', 'other'],
    idPrefixes: ['tg:', 'tt'],
    catalogs: [],
    behaviorHints: {
      configurable: true,
      configurationRequired: false,
    },
  };

  return new Response(JSON.stringify(manifest, null, 2), {
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'max-age=3600, public',
    },
  });
}

async function handleStremioStreamRequest(path, url, env, origin) {
  const cleanPath = path.replace(/^\/stream\//, '').replace(/\.json$/, '');
  const parts = cleanPath.split('/');
  const id = decodeURIComponent(parts[1] || '');

  const fileId = url.searchParams.get('file_id');
  const channelId = url.searchParams.get('channel_id') || '-1003967652604';

  let directStreamUrl = '';

  if (fileId) {
    directStreamUrl = `${origin}/stream/${channelId}/media?file_id=${encodeURIComponent(fileId)}&name=stream.mp4`;
  } else if (id.startsWith('tg:')) {
    const tgParts = id.split(':');
    if (tgParts.length >= 3) {
      directStreamUrl = `${origin}/stream/${tgParts[1]}/${tgParts[2]}?name=stream.mp4`;
    } else {
      directStreamUrl = `${origin}/stream/${tgParts[1]}?name=stream.mp4`;
    }
  } else {
    directStreamUrl = `${origin}/stream/-1003967652604/test?name=demo.mp4`;
  }

  const streamsResponse = {
    streams: [
      {
        name: '⚡ Telegram Direct Edge',
        title: `Direct High-Speed Stream\nChannel: ${channelId} | 1080p/4K Enabled`,
        url: directStreamUrl,
        behaviorHints: {
          notSupported: false,
          isFree: true,
        },
      },
    ],
  };

  return new Response(JSON.stringify(streamsResponse, null, 2), {
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'no-cache',
    },
  });
}

function handleDashboardUI(origin, env) {
  const allowedChannels = (env.ALLOWED_CHANNELS || DEFAULT_CHANNELS.join(', ')).split(',');
  const apiStatus = env.TELEGRAM_API_URL ? `Custom API Backend (${env.TELEGRAM_API_URL})` : 'Standard Bot API (20MB Max per file)';

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Telegram Direct Stream Generator & Stremio Addon</title>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-dark: #0b0f19;
      --card-bg: rgba(23, 31, 51, 0.75);
      --accent-cyan: #00f2fe;
      --accent-blue: #4facfe;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --border-color: rgba(255, 255, 255, 0.1);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Outfit', sans-serif;
      background: radial-gradient(circle at top right, #1e1b4b, var(--bg-dark));
      color: var(--text-main);
      min-height: 100vh;
      padding: 2rem 1rem;
      display: flex;
      justify-content: center;
      align-items: center;
    }
    .container {
      width: 100%;
      max-width: 900px;
      background: var(--card-bg);
      backdrop-filter: blur(16px);
      border: 1px solid var(--border-color);
      border-radius: 24px;
      padding: 2.5rem;
      box-shadow: 0 20px 50px rgba(0,0,0,0.5);
    }
    .header { text-align: center; margin-bottom: 2rem; }
    .badge {
      display: inline-block;
      padding: 6px 16px;
      border-radius: 20px;
      background: rgba(0, 242, 254, 0.1);
      color: var(--accent-cyan);
      font-size: 0.85rem;
      font-weight: 600;
      letter-spacing: 1px;
      text-transform: uppercase;
      margin-bottom: 1rem;
      border: 1px solid rgba(0, 242, 254, 0.3);
    }
    h1 {
      font-size: 2.4rem;
      font-weight: 700;
      background: linear-gradient(135deg, #ffffff, #93c5fd);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      margin-bottom: 0.5rem;
    }
    p.subtitle { color: var(--text-muted); font-size: 1rem; }
    .grid { display: grid; grid-template-columns: 1fr; gap: 1.5rem; }
    @media(min-width: 768px) { .grid { grid-template-columns: 1fr 1fr; } }
    .card {
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid var(--border-color);
      border-radius: 16px;
      padding: 1.5rem;
    }
    .card h3 { font-size: 1.2rem; margin-bottom: 1rem; color: var(--accent-cyan); }
    label { display: block; font-size: 0.9rem; color: var(--text-muted); margin-bottom: 6px; }
    input, select {
      width: 100%;
      padding: 12px 16px;
      background: rgba(0,0,0,0.4);
      border: 1px solid var(--border-color);
      border-radius: 10px;
      color: #fff;
      font-family: inherit;
      font-size: 0.95rem;
      margin-bottom: 1rem;
      outline: none;
    }
    input:focus, select:focus { border-color: var(--accent-cyan); }
    .btn {
      width: 100%;
      padding: 14px;
      border: none;
      border-radius: 12px;
      background: linear-gradient(135deg, var(--accent-cyan), var(--accent-blue));
      color: #000;
      font-weight: 700;
      font-size: 1rem;
      cursor: pointer;
    }
    .btn-secondary {
      background: rgba(255,255,255,0.1);
      color: #fff;
      border: 1px solid var(--border-color);
      margin-top: 10px;
    }
    .result-box {
      margin-top: 1rem;
      background: #000;
      border-radius: 12px;
      padding: 1rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.85rem;
      word-break: break-all;
      border: 1px solid rgba(0,242,254,0.2);
    }
    .channel-tag {
      display: inline-block;
      background: rgba(255,255,255,0.05);
      padding: 4px 10px;
      border-radius: 6px;
      font-size: 0.8rem;
      font-family: 'JetBrains Mono', monospace;
      margin: 4px 2px;
      border: 1px solid var(--border-color);
    }
    video { width: 100%; border-radius: 12px; margin-top: 1rem; background: #000; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <span class="badge">Cloudflare Edge Proxy v2.0</span>
      <h1>Telegram Streamable Link Generator</h1>
      <p class="subtitle">Generate high-speed, direct byte-range streamable links & Stremio Addon streams</p>
    </div>

    <div class="grid">
      <div class="card">
        <h3>⚡ Link Generator</h3>
        <form id="genForm" onsubmit="generateLink(event)">
          <label>Source Channel</label>
          <select id="channelSelect">
            ${allowedChannels.map((c) => `<option value="${c.trim()}">${c.trim()}</option>`).join('')}
          </select>

          <label>Message ID (or File ID)</label>
          <input type="text" id="fileIdInput" placeholder="e.g. 1234 or BQACAg..." required>

          <label>File Name</label>
          <input type="text" id="fileNameInput" value="movie.mp4">

          <button type="submit" class="btn">Generate Direct Stream URL</button>
        </form>

        <div id="outputArea" style="display:none;">
          <div class="result-box" id="resultUrl"></div>
          <button class="btn btn-secondary" onclick="copyResult()">📋 Copy Stream Link</button>
          <button class="btn btn-secondary" onclick="playInBrowser()">▶️ Test Play Video</button>
        </div>
      </div>

      <div class="card">
        <h3>🎬 Stremio Addon Integration</h3>
        <p style="font-size:0.9rem; color:var(--text-muted); margin-bottom:1rem;">
          Install into Stremio app to play directly from Telegram!
        </p>

        <label>Stremio Manifest URL</label>
        <div class="result-box">${origin}/manifest.json</div>
        <a href="stremio://${origin.replace(/^https?:\/\//, '')}/manifest.json" class="btn btn-secondary" style="display:block; text-align:center; text-decoration:none; margin-top:10px;">
          ➕ One-Click Install to Stremio
        </a>

        <h3 style="margin-top:1.5rem;">⚙️ System Engine</h3>
        <p style="font-size:0.85rem; color:var(--text-muted);">${apiStatus}</p>

        <label style="margin-top:1rem;">Whitelisted Channels:</label>
        <div>
          ${allowedChannels.map((c) => `<span class="channel-tag">${c.trim()}</span>`).join('')}
        </div>
      </div>
    </div>

    <div id="videoPreviewCard" class="card" style="margin-top:1.5rem; display:none;">
      <h3>▶️ Live HTML5 Stream Preview</h3>
      <video id="player" controls playsinline></video>
    </div>
  </div>

  <script>
    function generateLink(e) {
      e.preventDefault();
      const channel = document.getElementById('channelSelect').value;
      const fileId = document.getElementById('fileIdInput').value.trim();
      const fileName = document.getElementById('fileNameInput').value.trim() || 'video.mp4';
      
      const streamUrl = '${origin}/stream/' + channel + '/' + encodeURIComponent(fileId) + '?name=' + encodeURIComponent(fileName);
      
      document.getElementById('resultUrl').innerText = streamUrl;
      document.getElementById('outputArea').style.display = 'block';
    }

    function copyResult() {
      navigator.clipboard.writeText(document.getElementById('resultUrl').innerText);
      alert('Stream link copied!');
    }

    function playInBrowser() {
      const url = document.getElementById('resultUrl').innerText;
      const player = document.getElementById('player');
      player.src = url;
      document.getElementById('videoPreviewCard').style.display = 'block';
      player.play();
    }
  </script>
</body>
</html>`;

  return new Response(html, {
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-cache',
    },
  });
}
