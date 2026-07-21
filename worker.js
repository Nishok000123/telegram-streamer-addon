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

const DEFAULT_CHANNELS = ['-1003916531716', '-1002502061360', '-1003967652604', '-1002708448330'];

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

  // Do NOT use caches.default for media:
  // - HEAD / empty upstream bodies were being stored as GET with Content-Length: 0
  // - Stremio always sends Range; a poisoned 0-byte object yields 416 bytes */0
  // - Multi-GB videos are a poor fit for the Cache API anyway

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
    const upstreamHeaders = {};
    if (rangeHeader) upstreamHeaders['Range'] = rangeHeader;

    const upstream = await fetch(downloadUrl, {
      method: request.method === 'HEAD' ? 'HEAD' : 'GET',
      headers: upstreamHeaders,
    });

    if (!upstream.ok && upstream.status !== 206) {
      return new Response(`Upstream error: ${upstream.status}`, { status: upstream.status });
    }

    // Prefer real Telegram mime (usually video/x-matroska). Stremio used to force
    // name=stream.mp4 which lied as video/mp4 and broke players on MKV files.
    const upstreamType = upstream.headers.get('Content-Type');
    const contentType = (upstreamType && upstreamType.startsWith('video/'))
      ? upstreamType
      : mimeType;

    const resHeaders = new Headers({
      'Access-Control-Allow-Origin': '*',
      'Accept-Ranges': 'bytes',
      'Content-Type': contentType,
      // Short TTL only — never immutable year-long for seekable video
      'Cache-Control': 'public, max-age=60',
      'CF-Cache-Status': 'DYNAMIC',
      'Content-Disposition': forceDownload
        ? `attachment; filename="${encodeURIComponent(fileName)}"`
        : `inline; filename="${encodeURIComponent(fileName)}"`,
    });

    if (upstream.headers.has('Content-Length')) resHeaders.set('Content-Length', upstream.headers.get('Content-Length'));
    if (upstream.headers.has('Content-Range')) resHeaders.set('Content-Range', upstream.headers.get('Content-Range'));

    const status = upstream.status === 206 ? 206 : (rangeHeader && upstream.ok ? 206 : upstream.status);

    // HEAD must not attach a body (and must not invent one from upstream).
    if (request.method === 'HEAD') {
      return new Response(null, { status, headers: resHeaders });
    }

    return new Response(upstream.body, { status, headers: resHeaders });

  } catch (err) {
    return new Response(`Error: ${err.message}`, { status: 500 });
  }
}

function stremioManifest(origin) {
  return new Response(JSON.stringify({
    id: 'io.darkwave.stream',
    version: '4.5.2',
    name: '🌊 DarkWave Stream',
    description: 'Private high-speed streaming from curated Telegram sources — powered by MTProto & Cloudflare Edge.',
    logo: 'https://i.imgur.com/5ZNRcqH.png',
    resources: ['stream', 'catalog'],
    types: ['movie', 'series', 'other'],
    idPrefixes: ['tg:'],
    catalogs: [
      { type: 'movie', id: 'darkwave-latest', name: '🎬 DarkWave Latest Movies' },
      { type: 'series', id: 'darkwave-series', name: '📺 DarkWave Series' },
    ],
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
  const catalogPath = url.pathname; // e.g. /catalog/movie/darkwave-latest/search=avengers.json

  // Stremio sends search in path format: /catalog/{type}/{id}/search={query}.json
  // Or as query param: /catalog/{type}/{id}.json?search=query
  let searchParam = url.searchParams.get('search');
  const searchMatch = catalogPath.match(/\/search=(.+)\.json$/);
  if (searchMatch) {
    searchParam = decodeURIComponent(searchMatch[1]);
  }

  // Detect type from URL path: /catalog/{type}/{id}...
  const parts = catalogPath.replace('.json', '').split('/').filter(Boolean);
  const catalogType = parts.length >= 2 ? parts[1] : 'movie'; // movie or series
  const catalogId = parts.length >= 3 ? parts[2] : '';

  let metas = [];

  try {
    if (!API_BASE) return emptyCatalog();

    if (searchParam) {
      // Search mode — proxy to backend /search endpoint
      const searchUrl = `${API_BASE}/search?q=${encodeURIComponent(searchParam)}&enriched=true`;
      const res = await fetch(searchUrl);
      if (res.ok) {
        const data = await res.json();
        metas = buildMetasFromItems(data.results || [], catalogType);
      }
    } else {
      // Browse mode — fetch recent items
      const res = await fetch(`${API_BASE}/recent?limit=50&enriched=true`);
      if (res.ok) {
        const data = await res.json();
        metas = buildMetasFromItems(data.items || [], catalogType);
      }
    }
  } catch (e) {
    console.error('Catalog error:', e);
  }

  return new Response(JSON.stringify({ metas }), {
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': searchParam ? 'no-cache' : 'max-age=300',
    },
  });
}

function buildMetasFromItems(items, catalogType) {
  return items
    .filter(item => {
      const mt = item.info?.media_type || 'movie';
      // Show all items if 'other', filter by type for movie/series catalogs
      if (catalogType === 'other') return true;
      return mt === catalogType;
    })
    .map(item => {
      const info = item.info || {};
      const tmdb = info.tmdb || {};
      const quality = info.quality || 'HD';
      const source = info.source || '';
      const year = info.year ? String(info.year) : '';
      const size = info.size || '';
      const mediaType = info.media_type || 'movie';

      // Build description line
      const parts = [quality, source, size].filter(Boolean);
      let desc = parts.join(' | ');
      if (year) desc = `${year} · ${desc}`;

      // Use TMDB poster if available
      const poster = tmdb.poster || null;
      const background = tmdb.backdrop || poster;

      const meta = {
        id: `tg:${item.channel_id}:${item.message_id}`,
        type: mediaType === 'series' ? 'series' : 'movie',
        name: tmdb.title || info.title || item.file_name || 'Unknown',
        description: tmdb.overview || desc,
        poster,
        background,
        logo: poster,
        year,
        releaseInfo: year,
        imdbRating: tmdb.vote_average ? String(tmdb.vote_average) : undefined,
        posterShape: 'poster',
      };

      if (mediaType === 'series' && info.season != null) {
        meta.season = info.season;
        meta.episode = info.episode;
      }

      return meta;
    });
}

function emptyCatalog() {
  return new Response(JSON.stringify({ metas: [] }), {
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'max-age=60',
    },
  });
}

async function stremioStream(path, url, env, origin) {
  const id = decodeURIComponent(path.replace(/^\/stream\/[^/]+\//, '').replace(/\.json$/, ''));
  let streams = [];

  if (id.startsWith('tg:')) {
    const parts = id.split(':');
    if (parts.length >= 3) {
      // Most Telegram sources are MKV; mp4 label lied to players. Mime comes from backend.
      const streamUrl = `${origin}/stream/${parts[1]}/${parts[2]}?name=stream.mkv`;
      streams.push({
        name: '🌊 DarkWave',
        title: 'Edge Stream | Telegram MTProto',
        url: streamUrl,
        behaviorHints: {
          bingeGroup: `darkwave-${parts[1]}-${parts[2]}`,
          filename: 'stream.mkv',
          notWebReady: true,
        },
      });
    }
  }

  return new Response(JSON.stringify({ streams }), {
    headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-cache' },
  });
}
