/**
 * Cloudflare Worker: Enterprise Telegram Direct Stream Generator & Stremio Addon
 * Fully Automated & Zero-Maintenance Engine v2.5
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

    // 1. Stremio Manifest Endpoint
    if (path === '/manifest.json') {
      return handleStremioManifest(origin);
    }

    // 2. Stremio Catalog Endpoint (/catalog/movie/telegram.json)
    if (path.startsWith('/catalog/')) {
      return handleStremioCatalog(path, url, env, origin);
    }

    // 3. Stremio Stream Endpoint (/stream/movie/id.json)
    if (path.startsWith('/stream/') && path.endsWith('.json')) {
      return handleStremioStreamRequest(path, url, env, origin);
    }

    // 4. API Search Proxy (/api/search?q=...)
    if (path === '/api/search' || path === '/api/recent') {
      return handleApiProxy(path, url, env);
    }

    // 5. Dashboard UI
    if (path === '/' || path === '/configure') {
      return handleDashboardUI(origin, env);
    }

    // 6. Media Stream Proxy Endpoint
    if (path.startsWith('/stream/')) {
      return handleMediaStream(request, env, ctx, url);
    }

    // 7. Direct Download Endpoint
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
    
    // Using MTProto FastAPI Backend (Koyeb / Render / VPS)
    if (API_BASE.includes('koyeb') || API_BASE.includes('render') || API_BASE.includes('hf.space') || !API_BASE.includes('api.telegram.org')) {
      downloadUrl = `${API_BASE}/stream/${channelId || '-1003967652604'}/${fileId}`;
    } else {
      if (!BOT_TOKEN) {
        return new Response('Error: TELEGRAM_BOT_TOKEN environment variable is missing.', { status: 500 });
      }
      const fileInfoUrl = `${API_BASE}/bot${BOT_TOKEN}/getFile?file_id=${encodeURIComponent(fileId)}`;
      const fileInfoRes = await fetch(fileInfoUrl, {
        headers: { 'User-Agent': 'Cloudflare-Telegram-Proxy/2.5' },
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
    version: '2.5.0',
    name: 'Telegram Media Auto-Streamer',
    description: 'Automated video streaming addon backed by Telegram Source Channels, Auto Search & Cloudflare Edge Caching.',
    resources: ['stream', 'catalog'],
    types: ['movie', 'series', 'other'],
    idPrefixes: ['tg:', 'tt'],
    catalogs: [
      {
        type: 'movie',
        id: 'telegram-recent-movies',
        name: 'Telegram Source Movies',
      },
    ],
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

async function handleStremioCatalog(path, url, env, origin) {
  const API_BASE = (env.TELEGRAM_API_URL || 'https://api.telegram.org').replace(/\/$/, '');
  let metas = [];

  try {
    if (API_BASE.includes('koyeb') || API_BASE.includes('render') || API_BASE.includes('hf.space')) {
      const res = await fetch(`${API_BASE}/recent?limit=20`);
      if (res.ok) {
        const data = await res.json();
        metas = (data.items || []).map((item) => ({
          id: `tg:${item.channel_id}:${item.message_id}`,
          type: 'movie',
          name: item.file_name,
          poster: 'https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=500&auto=format&fit=crop&q=60',
          description: `Quality: ${item.info.quality} | Size: ${item.info.formatted_size} | Audio: ${item.info.audio}`,
        }));
      }
    }
  } catch (e) {
    console.log('Catalog fetch error:', e);
  }

  return new Response(JSON.stringify({ metas }, null, 2), {
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'max-age=300, public',
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
        title: `Telegram Direct Stream\nChannel: ${channelId} | 1080p/4K Enabled`,
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

async function handleApiProxy(path, url, env) {
  const API_BASE = (env.TELEGRAM_API_URL || 'https://api.telegram.org').replace(/\/$/, '');
  const query = url.searchParams.get('q') || '';
  
  try {
    let targetUrl = `${API_BASE}/recent`;
    if (path === '/api/search' && query) {
      targetUrl = `${API_BASE}/search?q=${encodeURIComponent(query)}`;
    }
    const res = await fetch(targetUrl);
    const data = await res.json();
    return new Response(JSON.stringify(data), {
      headers: {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
      },
    });
  } catch (e) {
    return new Response(JSON.stringify({ error: e.message }), { status: 500 });
  }
}

function handleDashboardUI(origin, env) {
  const allowedChannels = (env.ALLOWED_CHANNELS || DEFAULT_CHANNELS.join(', ')).split(',');
  const apiStatus = env.TELEGRAM_API_URL ? `Auto Backend (${env.TELEGRAM_API_URL})` : 'Standard Bot API';

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
      max-width: 950px;
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
    .search-item {
      background: rgba(0,0,0,0.4);
      border: 1px solid var(--border-color);
      padding: 10px;
      border-radius: 8px;
      margin-bottom: 8px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .tag { font-size: 0.75rem; padding: 2px 6px; border-radius: 4px; background: rgba(0, 242, 254, 0.2); color: var(--accent-cyan); }
    video { width: 100%; border-radius: 12px; margin-top: 1rem; background: #000; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <span class="badge">Zero-Touch Automation Engine v2.5</span>
      <h1>Telegram Streamable Link Generator</h1>
      <p class="subtitle">Search, Stream, and Direct-Download from Telegram Source Channels</p>
    </div>

    <div class="grid">
      <div class="card">
        <h3>🔍 Auto Channel Movie Search</h3>
        <input type="text" id="searchInput" placeholder="Type movie title (e.g. Avatar, Inception)..." onkeyup="if(event.key==='Enter') searchChannel()">
        <button class="btn" onclick="searchChannel()">Search Channels</button>
        
        <div id="searchResults" style="margin-top:1rem; max-height:220px; overflow-y:auto;"></div>
      </div>

      <div class="card">
        <h3>⚡ Direct Message ID Streamer</h3>
        <form id="genForm" onsubmit="generateLink(event)">
          <label>Source Channel</label>
          <select id="channelSelect">
            ${allowedChannels.map((c) => `<option value="${c.trim()}">${c.trim()}</option>`).join('')}
          </select>

          <label>Message ID (e.g. 1234)</label>
          <input type="text" id="fileIdInput" placeholder="1234" required>

          <label>File Name</label>
          <input type="text" id="fileNameInput" value="movie.mp4">

          <button type="submit" class="btn">Generate Direct Stream URL</button>
        </form>
      </div>
    </div>

    <div class="card" style="margin-top:1.5rem;">
      <h3>🎬 Stremio Addon Integration</h3>
      <label>Stremio Manifest URL</label>
      <div class="result-box">${origin}/manifest.json</div>
      <a href="stremio://${origin.replace(/^https?:\/\//, '')}/manifest.json" class="btn btn-secondary" style="display:block; text-align:center; text-decoration:none; margin-top:10px;">
        ➕ One-Click Install to Stremio
      </a>
    </div>

    <div id="outputArea" class="card" style="margin-top:1.5rem; display:none;">
      <h3>🎯 Generated Stream URL</h3>
      <div class="result-box" id="resultUrl"></div>
      <button class="btn btn-secondary" onclick="copyResult()">📋 Copy Stream Link</button>
      <button class="btn btn-secondary" onclick="playInBrowser()">▶️ Test Play Video</button>

      <div id="videoPreviewCard" style="display:none; margin-top:1rem;">
        <video id="player" controls playsinline></video>
      </div>
    </div>
  </div>

  <script>
    async function searchChannel() {
      const q = document.getElementById('searchInput').value.trim();
      if (!q) return;
      const resContainer = document.getElementById('searchResults');
      resContainer.innerHTML = '<div style="color:var(--text-muted);">Searching channels...</div>';

      try {
        const res = await fetch('/api/search?q=' + encodeURIComponent(q));
        const data = await res.json();
        
        if (!data.results || data.results.length === 0) {
          resContainer.innerHTML = '<div style="color:var(--text-muted);">No movies found matching query.</div>';
          return;
        }

        let html = '';
        data.results.forEach(item => {
          const streamUrl = '${origin}/stream/' + item.channel_id + '/' + item.message_id + '?name=' + encodeURIComponent(item.file_name);
          html += '<div class="search-item">' +
            '<div>' +
              '<div style="font-weight:600; font-size:0.9rem;">' + item.file_name + '</div>' +
              '<div style="margin-top:4px;"><span class="tag">' + item.info.quality + '</span> <span style="font-size:0.8rem; color:var(--text-muted);">' + item.info.formatted_size + '</span></div>' +
            '</div>' +
            '<button class="btn" style="width:auto; padding:6px 12px; font-size:0.8rem;" onclick="selectStream(\'' + streamUrl + '\')">Select</button>' +
          '</div>';
        });
        resContainer.innerHTML = html;
      } catch (e) {
        resContainer.innerHTML = '<div style="color:red;">Error searching backend.</div>';
      }
    }

    function selectStream(url) {
      document.getElementById('resultUrl').innerText = url;
      document.getElementById('outputArea').style.display = 'block';
    }

    function generateLink(e) {
      e.preventDefault();
      const channel = document.getElementById('channelSelect').value;
      const fileId = document.getElementById('fileIdInput').value.trim();
      const fileName = document.getElementById('fileNameInput').value.trim() || 'movie.mp4';
      
      const streamUrl = '${origin}/stream/' + channel + '/' + encodeURIComponent(fileId) + '?name=' + encodeURIComponent(fileName);
      selectStream(streamUrl);
    }

    function copyResult() {
      navigator.clipboard.writeText(document.getElementById('resultUrl').innerText);
      alert('Stream link copied to clipboard!');
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
