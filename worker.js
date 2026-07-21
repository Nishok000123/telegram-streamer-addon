/**
 * Cloudflare Worker: Telegram Direct Stream Proxy & Stremio Addon
 * Private - No public web dashboard. Streams only to authenticated Stremio/VLC clients.
 */

const MIME_TYPES = {
  mp4: 'video/mp4', mkv: 'video/x-matroska', webm: 'video/webm',
  avi: 'video/x-msvideo', mov: 'video/quicktime', m4v: 'video/x-m4v',
  ts: 'video/mp2t', flv: 'video/x-flv', mp3: 'audio/mpeg',
  flac: 'audio/flac', m4a: 'audio/mp4', ogg: 'audio/ogg', aac: 'audio/aac',
};

const DEFAULT_CHANNELS = ['-1003967652604', '-1002502061360', '-1003916531716'];

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const origin = url.origin;

    // CORS preflight
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

    // Stremio Manifest
    if (path === '/manifest.json') return stremioManifest(origin);

    // Stremio Catalog
    if (path.startsWith('/catalog/')) return stremioCatalog(url, env, origin);

    // Stremio Stream endpoint (/stream/movie/tg:channelId:msgId.json)
    if (path.startsWith('/stream/') && path.endsWith('.json')) return stremioStream(path, url, env, origin);

    // Media proxy stream (/stream/{channel_id}/{message_id})
    if (path.startsWith('/stream/')) return mediaProxy(request, env, ctx, url, false);

    // Direct download (/dl/{channel_id}/{message_id})
    if (path.startsWith('/dl/')) return mediaProxy(request, env, ctx, url, true);

    // Block everything else — no public dashboard
    return new Response('403 Forbidden', { status: 403 });
  },
};

async function mediaProxy(request, env, ctx, url, forceDownload) {
  const API_BASE = (env.TELEGRAM_API_URL || 'https://api.telegram.org').replace(/\/$/, '');
  const BOT_TOKEN = env.TELEGRAM_BOT_TOKEN;

  const parts = url.pathname.split('/').filter(Boolean);
  const channelId = parts.length >= 3 ? parts[1] : null;
  const fileOrMsgId = parts.length >= 3 ? parts[2] : parts[1];

  const allowedChannels = env.ALLOWED_CHANNELS
    ? env.ALLOWED_CHANNELS.split(',').map(c => c.trim())
    : DEFAULT_CHANNELS;

  if (channelId && !allowedChannels.includes(channelId)) {
    return new Response('403 Forbidden', { status: 403 });
  }

  const fileId = url.searchParams.get('file_id') || fileOrMsgId;
  const fileName = url.searchParams.get('name') || 'video.mp4';
  const ext = fileName.split('.').pop().toLowerCase();
  const mimeType = MIME_TYPES[ext] || 'video/mp4';

  if (!fileId) return new Response('Missing file_id', { status: 400 });

  // Check edge cache
  const cache = caches.default;
  const cacheKey = new Request(request.url, { headers: request.headers, method: 'GET' });
  const cached = await cache.match(cacheKey);
  if (cached) {
    const h = new Headers(cached.headers);
    h.set('CF-Cache-Status', 'HIT');
    return new Response(cached.body, { status: cached.status, headers: h });
  }

  try {
    let downloadUrl;

    // Route to Koyeb MTProto backend if configured
    if (!API_BASE.includes('api.telegram.org')) {
      downloadUrl = `${API_BASE}/stream/${channelId || allowedChannels[0]}/${fileId}`;
    } else {
      if (!BOT_TOKEN) return new Response('BOT_TOKEN missing', { status: 500 });
      const infoRes = await fetch(`${API_BASE}/bot${BOT_TOKEN}/getFile?file_id=${encodeURIComponent(fileId)}`);
      const info = await infoRes.json();
      if (!info.ok || !info.result?.file_path) return new Response('File not found', { status: 404 });
      downloadUrl = `${API_BASE}/file/bot${BOT_TOKEN}/${info.result.file_path}`;
    }

    const rangeHeader = request.headers.get('Range');
    const upstream = await fetch(downloadUrl, {
      method: request.method,
      headers: rangeHeader ? { Range: rangeHeader } : {},
    });

    if (!upstream.ok && upstream.status !== 206) {
      return new Response(`Upstream error: ${upstream.status}`, { status: upstream.status });
    }

    const resHeaders = new Headers({
      'Access-Control-Allow-Origin': '*',
      'Accept-Ranges': 'bytes',
      'Content-Type': mimeType,
      'Cache-Control': 'public, max-age=31536000, immutable',
      'CF-Cache-Status': 'MISS',
      'Content-Disposition': forceDownload
        ? `attachment; filename="${encodeURIComponent(fileName)}"`
        : `inline; filename="${encodeURIComponent(fileName)}"`,
    });

    if (upstream.headers.has('Content-Length')) resHeaders.set('Content-Length', upstream.headers.get('Content-Length'));
    if (upstream.headers.has('Content-Range')) resHeaders.set('Content-Range', upstream.headers.get('Content-Range'));

    const status = (upstream.status === 206 || rangeHeader) ? 206 : 200;
    const response = new Response(upstream.body, { status, headers: resHeaders });
    ctx.waitUntil(cache.put(cacheKey, response.clone()));
    return response;

  } catch (err) {
    return new Response(`Error: ${err.message}`, { status: 500 });
  }
}

function stremioManifest(origin) {
  return new Response(JSON.stringify({
    id: 'org.telegram.direct.streaming.addon',
    version: '4.0.0',
    name: 'Telegram Direct Streamer',
    description: 'Private high-speed Telegram channel video streaming via Cloudflare Edge.',
    resources: ['stream', 'catalog'],
    types: ['movie', 'series', 'other'],
    idPrefixes: ['tg:'],
    catalogs: [{ type: 'movie', id: 'telegram-recent', name: 'Telegram Source Movies' }],
    behaviorHints: { configurable: false, configurationRequired: false },
  }, null, 2), {
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'max-age=3600',
    },
  });
}

async function stremioCatalog(url, env, origin) {
  const API_BASE = (env.TELEGRAM_API_URL || '').replace(/\/$/, '');
  let metas = [];
  try {
    if (API_BASE) {
      const res = await fetch(`${API_BASE}/recent?limit=20`);
      if (res.ok) {
        const data = await res.json();
        metas = (data.items || []).map(item => ({
          id: `tg:${item.channel_id}:${item.message_id}`,
          type: 'movie',
          name: item.file_name,
          description: `${item.info.quality} | ${item.info.size} | ${item.info.audio}`,
        }));
      }
    }
  } catch (e) {}
  return new Response(JSON.stringify({ metas }), {
    headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=300' },
  });
}

async function stremioStream(path, url, env, origin) {
  const id = decodeURIComponent(path.replace(/^\/stream\/[^/]+\//, '').replace(/\.json$/, ''));
  let streams = [];

  if (id.startsWith('tg:')) {
    const parts = id.split(':');
    if (parts.length >= 3) {
      const streamUrl = `${origin}/stream/${parts[1]}/${parts[2]}?name=stream.mp4`;
      streams.push({
        name: '⚡ Telegram Edge Stream',
        title: `Direct Stream | 1080p/4K`,
        url: streamUrl,
        behaviorHints: { notSupported: false, isFree: true },
      });
    }
  }

  return new Response(JSON.stringify({ streams }), {
    headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-cache' },
  });
}
