/**
 * Cloudflare Worker: Telegram stream proxy + Stremio stream-only addon.
 * No catalog — attaches Telegram sources to Cinemeta titles (tt…).
 */

const MIME_TYPES = {
  mp4: 'video/mp4', mkv: 'video/x-matroska', webm: 'video/webm',
  avi: 'video/x-msvideo', mov: 'video/quicktime', m4v: 'video/x-m4v',
  ts: 'video/mp2t', flv: 'video/x-flv', mp3: 'audio/mpeg',
  flac: 'audio/flac', m4a: 'audio/mp4', ogg: 'audio/ogg', aac: 'audio/aac',
};

const DEFAULT_CHANNELS = ['-1003916531716', '-1002502061360', '-1003967652604', '-1002708448330'];
const CINEMETA = 'https://v3-cinemeta.strem.io';

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

    if (path === '/manifest.json') return stremioManifest();

    // Stream sources for Stremio (/stream/movie/tt….json)
    if (path.startsWith('/stream/') && path.endsWith('.json')) {
      return stremioStream(path, env, origin);
    }

    // Media proxy (/stream/{channel_id}/{message_id})
    if (path.startsWith('/stream/')) return mediaProxy(request, env, url, false);
    if (path.startsWith('/dl/')) return mediaProxy(request, env, url, true);

    return new Response('403 Forbidden', { status: 403 });
  },
};

function stremioManifest() {
  return new Response(JSON.stringify({
    id: 'io.darkwave.stream',
    version: '5.0.1',
    name: '🌊 DarkWave Stream',
    description: 'Telegram stream sources for movies & series — no catalog, sources only.',
    logo: 'https://i.imgur.com/5ZNRcqH.png',
    resources: ['stream'],
    types: ['movie', 'series'],
    idPrefixes: ['tt', 'tg:'],
    catalogs: [],
    behaviorHints: { configurable: false, configurationRequired: false },
  }, null, 2), {
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'max-age=300',
    },
  });
}

async function stremioStream(path, env, origin) {
  // /stream/{type}/{id}.json
  const parts = path.replace(/\.json$/, '').split('/').filter(Boolean);
  const type = parts[1] || 'movie';
  const id = decodeURIComponent(parts.slice(2).join('/') || '');

  let streams = [];

  try {
    if (id.startsWith('tg:')) {
      streams = streamsFromTgId(id, origin);
    } else if (id.startsWith('tt')) {
      streams = await streamsFromImdb(id, type, env, origin);
    }
  } catch (e) {
    console.error('stream error:', e);
  }

  return new Response(JSON.stringify({ streams }), {
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'no-cache',
    },
  });
}

function streamsFromTgId(id, origin) {
  const parts = id.split(':');
  if (parts.length < 3) return [];
  const channelId = parts[1];
  const messageId = parts[2];
  return [makeStream(origin, channelId, messageId, 'DarkWave', 'Telegram direct')];
}

async function streamsFromImdb(id, type, env, origin) {
  const API_BASE = (env.TELEGRAM_API_URL || '').replace(/\/$/, '');
  if (!API_BASE) return [];

  const imdb = id.split(':')[0];
  const season = type === 'series' && id.includes(':') ? Number(id.split(':')[1]) : null;
  const episode = type === 'series' && id.includes(':') ? Number(id.split(':')[2]) : null;

  const meta = await fetchCinemeta(type === 'series' ? 'series' : 'movie', imdb);
  if (!meta?.name) return [];

  const year = meta.year ? String(meta.year).slice(0, 4) : '';
  const queries = buildSearchQueries(meta.name, year, season, episode);

  const results = [];
  const seen = new Set();
  for (const q of queries) {
    const res = await fetch(`${API_BASE}/search?q=${encodeURIComponent(q)}`);
    if (!res.ok) continue;
    const data = await res.json();
    for (const item of data.results || []) {
      const key = `${item.channel_id}:${item.message_id}`;
      if (seen.has(key)) continue;
      if (!matchesMeta(item, meta.name, year, season, episode, type)) continue;
      seen.add(key);
      results.push(item);
    }
    if (results.length >= 12) break;
  }

  return results.map((item) => {
    const info = item.info || {};
    const label = [info.quality, info.source, info.size].filter(Boolean).join(' · ') || item.file_name || 'Telegram';
    return makeStream(origin, item.channel_id, item.message_id, '🌊 DarkWave', label, item.file_name);
  });
}

async function fetchCinemeta(type, imdbId) {
  const res = await fetch(`${CINEMETA}/meta/${type}/${imdbId}.json`);
  if (!res.ok) return null;
  const data = await res.json();
  return data.meta || null;
}

function buildSearchQueries(name, year, season, episode) {
  const clean = String(name).replace(/[^\w\s]/g, ' ').replace(/\s+/g, ' ').trim();
  const qs = [clean];
  if (year) qs.push(`${clean} ${year}`);
  if (season != null && episode != null) {
    const s = String(season).padStart(2, '0');
    const e = String(episode).padStart(2, '0');
    qs.unshift(`${clean} S${s}E${e}`);
    qs.unshift(`${clean} ${s}x${e}`);
  }
  return qs;
}

function matchesMeta(item, name, year, season, episode, type) {
  const fname = (item.file_name || '').toLowerCase();
  const info = item.info || {};
  const title = (info.title || '').toLowerCase();
  const hay = `${fname} ${title}`;

  const isEpisode = /s\s*\d{1,2}\s*e\s*\d{1,2}|\d{1,2}\s*x\s*\d{1,2}/i.test(fname)
    || info.media_type === 'series';
  if (type === 'movie' && isEpisode) return false;

  const tokens = String(name).toLowerCase().replace(/[^\w\s]/g, ' ').split(/\s+/).filter((t) => t.length > 1);
  if (!tokens.length) return false;
  for (const t of tokens) {
    if (!new RegExp(`(?:^|[^a-z0-9])${escapeRe(t)}(?:[^a-z0-9]|$)`, 'i').test(hay)) return false;
  }

  // Block "Sharp Obsession" / "Dark Obsession" when query is just "Obsession"
  if (tokens.length === 1) {
    const t = tokens[0];
    const stripped = hay
      .replace(/\[[^\]]*]/g, ' ')
      .replace(/\([^)]*\)/g, ' ')
      .replace(/\.(mkv|mp4|avi|mov|m4v|webm)$/i, ' ');
    const m = stripped.match(new RegExp(`(?:^|[^a-z0-9])([a-z]{2,}(?:[._\\s-]+[a-z0-9]+){0,4})[._\\s-]+${escapeRe(t)}(?:[^a-z0-9]|$)`, 'i'));
    if (m && m[1]) {
      const prefix = m[1].toLowerCase().replace(/[._-]+/g, ' ').trim();
      const allowed = new Set(['the', 'a', 'an']);
      if (![...prefix.split(/\s+/)].every((w) => allowed.has(w))) return false;
    }
  }

  if (type === 'series' && season != null && episode != null) {
    if (info.season != null && info.episode != null) {
      return Number(info.season) === Number(season) && Number(info.episode) === Number(episode);
    }
    const s = String(season).padStart(2, '0');
    const e = String(episode).padStart(2, '0');
    return new RegExp(`s\\s*${s}\\s*e\\s*${e}|${Number(season)}\\s*x\\s*${e}`, 'i').test(fname);
  }

  if (year) {
    const y = String(year);
    const yearsInName = fname.match(/\b(?:19|20)\d{2}\b/g) || [];
    if (yearsInName.length && !yearsInName.includes(y)) return false;
  }
  return true;
}

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function makeStream(origin, channelId, messageId, name, title, fileName) {
  const safeName = (fileName && /\.[a-z0-9]{2,4}$/i.test(fileName))
    ? fileName.split(/[/\\]/).pop()
    : 'stream.mkv';
  return {
    name,
    title,
    url: `${origin}/stream/${channelId}/${messageId}?name=${encodeURIComponent(safeName)}`,
    behaviorHints: {
      bingeGroup: `darkwave-${channelId}-${messageId}`,
      filename: safeName,
    },
  };
}

async function mediaProxy(request, env, url, forceDownload) {
  const API_BASE = (env.TELEGRAM_API_URL || 'https://api.telegram.org').replace(/\/$/, '');
  const BOT_TOKEN = env.TELEGRAM_BOT_TOKEN;

  const parts = url.pathname.split('/').filter(Boolean);
  const channelId = parts.length >= 3 ? parts[1] : null;
  const fileOrMsgId = parts.length >= 3 ? parts[2] : parts[1];

  const allowedChannels = env.ALLOWED_CHANNELS
    ? env.ALLOWED_CHANNELS.split(',').map((c) => c.trim())
    : DEFAULT_CHANNELS;

  if (channelId && !allowedChannels.includes(channelId)) {
    return new Response('403 Forbidden', { status: 403 });
  }

  const fileId = url.searchParams.get('file_id') || fileOrMsgId;
  const fileName = url.searchParams.get('name') || 'video.mkv';
  const ext = fileName.split('.').pop().toLowerCase();
  const mimeType = MIME_TYPES[ext] || 'video/x-matroska';

  if (!fileId) return new Response('Missing file_id', { status: 400 });

  try {
    let downloadUrl;
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
    if (rangeHeader) upstreamHeaders.Range = rangeHeader;

    const upstream = await fetch(downloadUrl, {
      method: request.method === 'HEAD' ? 'HEAD' : 'GET',
      headers: upstreamHeaders,
    });

    if (!upstream.ok && upstream.status !== 206) {
      return new Response(`Upstream error: ${upstream.status}`, { status: upstream.status });
    }

    const upstreamType = upstream.headers.get('Content-Type');
    const contentType = (upstreamType && upstreamType.startsWith('video/'))
      ? upstreamType
      : mimeType;

    const resHeaders = new Headers({
      'Access-Control-Allow-Origin': '*',
      'Accept-Ranges': 'bytes',
      'Content-Type': contentType,
      'Cache-Control': 'public, max-age=60',
      'CF-Cache-Status': 'DYNAMIC',
      'Content-Disposition': forceDownload
        ? `attachment; filename="${encodeURIComponent(fileName)}"`
        : `inline; filename="${encodeURIComponent(fileName)}"`,
    });

    if (upstream.headers.has('Content-Length')) {
      resHeaders.set('Content-Length', upstream.headers.get('Content-Length'));
    }
    if (upstream.headers.has('Content-Range')) {
      resHeaders.set('Content-Range', upstream.headers.get('Content-Range'));
    }

    const status = upstream.status === 206 ? 206 : (rangeHeader && upstream.ok ? 206 : upstream.status);

    if (request.method === 'HEAD') {
      return new Response(null, { status, headers: resHeaders });
    }

    return new Response(upstream.body, { status, headers: resHeaders });
  } catch (err) {
    return new Response(`Error: ${err.message}`, { status: 500 });
  }
}
