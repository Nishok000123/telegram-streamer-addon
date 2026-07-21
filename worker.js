/**
 * Cloudflare Worker: Telegram stream proxy + Stremio addon.
 * No R2 (no payment). Index from Koyeb; optional KV registry; Cache API segments.
 * Bot /cache prewarms head segments so play starts fast; list stays in KV.
 */

const MIME_TYPES = {
  mp4: 'video/mp4', mkv: 'video/x-matroska', webm: 'video/webm',
  avi: 'video/x-msvideo', mov: 'video/quicktime', m4v: 'video/x-m4v',
  ts: 'video/mp2t', flv: 'video/x-flv', mp3: 'audio/mpeg',
  flac: 'audio/flac', m4a: 'audio/mp4', ogg: 'audio/ogg', aac: 'audio/aac',
};

const DEFAULT_CHANNELS = ['-1003916531716', '-1002502061360', '-1003967652604', '-1002708448330'];
const CINEMETA = 'https://v3-cinemeta.strem.io';
const INDEX_KEY = 'index:categories';
const CACHED_LIST_KEY = 'cached:list';
const SEGMENT_SIZE = 1024 * 1024;
const SEGMENT_TTL = 2592000; // 30 days — keep warm as long as CF allows
const DEFAULT_PREWARM_MB = 128; // cache first ~128 MiB before watch

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const origin = url.origin;

    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS, POST',
          'Access-Control-Allow-Headers': 'Content-Type, Range, Authorization, X-Index-Secret',
          'Access-Control-Max-Age': '86400',
        },
      });
    }

    const path = url.pathname;

    if (path === '/manifest.json') return stremioManifest();

    if (path === '/admin/index' && request.method === 'POST') {
      return adminSaveIndex(request, env);
    }
    if (path === '/admin/prewarm' && request.method === 'POST') {
      return adminPrewarm(request, env, ctx);
    }
    if (path === '/admin/prewarm-hot' && request.method === 'POST') {
      return adminPrewarmHot(request, env, ctx);
    }
    if (path === '/admin/cached') {
      return adminListCached(env);
    }
    if (path === '/index/categories') {
      return serveCategories(env);
    }

    if (path.startsWith('/catalog/')) {
      return stremioCatalog(path, url, env, origin);
    }

    if (path.startsWith('/stream/') && path.endsWith('.json')) {
      return stremioStream(path, env, origin);
    }

    if (path.startsWith('/stream/')) return mediaProxy(request, env, url, false, ctx);
    if (path.startsWith('/dl/')) return mediaProxy(request, env, url, true, ctx);

    return new Response('403 Forbidden', { status: 403 });
  },
};

function stremioManifest() {
  return new Response(JSON.stringify({
    id: 'io.darkwave.stream',
    version: '5.2.0',
    name: '🌊 DarkWave Stream',
    description: 'Telegram sources + Tamil/English index. Bot /cache warms play (no R2).',
    logo: 'https://i.imgur.com/5ZNRcqH.png',
    resources: ['catalog', 'stream'],
    types: ['movie', 'series'],
    idPrefixes: ['tt', 'tg:'],
    catalogs: [
      { type: 'movie', id: 'tamil-latest', name: 'Tamil Latest (+ multi)' },
      { type: 'movie', id: 'tamil-popular', name: 'Tamil Popular' },
      { type: 'movie', id: 'english-latest', name: 'English Latest' },
      { type: 'movie', id: 'english-popular', name: 'English Popular' },
      { type: 'movie', id: 'multi-audio', name: 'Multi / Dual Audio' },
    ],
    behaviorHints: { configurable: false, configurationRequired: false },
  }, null, 2), {
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'max-age=300',
    },
  });
}

function checkSecret(request, env) {
  const need = env.INDEX_SECRET || '';
  if (!need) return true;
  const got = request.headers.get('X-Index-Secret') || '';
  return got === need;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
    },
  });
}

function guessMime(fileName) {
  const ext = String(fileName).split('.').pop().toLowerCase();
  return MIME_TYPES[ext] || 'video/x-matroska';
}

function cachedMetaKey(channelId, messageId) {
  return `cached:${channelId}:${messageId}`;
}

function segCacheKey(channelId, messageId, index) {
  return new Request(`https://tg-seg-cache.internal/seg/${channelId}/${messageId}/${index}`);
}

function metaCacheKey(channelId, messageId) {
  return new Request(`https://tg-seg-cache.internal/meta/${channelId}/${messageId}`);
}

async function adminSaveIndex(request, env) {
  if (!checkSecret(request, env)) return new Response('Unauthorized', { status: 401 });
  const body = await request.json();
  const cats = {
    updated_at: body.updated_at || Math.floor(Date.now() / 1000),
    total_indexed: body.total_indexed || (body.items || []).length,
    tamil_latest: body.tamil_latest || [],
    tamil_popular: body.tamil_popular || [],
    english_latest: body.english_latest || [],
    english_popular: body.english_popular || [],
    multi_audio_latest: body.multi_audio_latest || [],
    hot_prewarm: body.hot_prewarm || [],
  };
  if (env.MEDIA_META) {
    // KV stays — no expiry on catalog mirror
    await env.MEDIA_META.put(INDEX_KEY, JSON.stringify(cats));
  }
  return json({ ok: true, stored: env.MEDIA_META ? 'kv' : 'memory-skip', total_indexed: cats.total_indexed });
}

async function serveCategories(env) {
  const cats = await loadCategories(env);
  if (!cats) {
    return json({ updated_at: 0, hint: 'no index yet — run bot /index or POST /admin/index' });
  }
  return json(cats);
}

async function loadCategories(env) {
  if (env.MEDIA_META) {
    const raw = await env.MEDIA_META.get(INDEX_KEY);
    if (raw) {
      try { return JSON.parse(raw); } catch (_) { /* fall through */ }
    }
  }
  // Fallback: live from Koyeb (no payment storage needed)
  const API_BASE = (env.TELEGRAM_API_URL || '').replace(/\/$/, '');
  if (!API_BASE) return null;
  try {
    const res = await fetch(`${API_BASE}/index/categories?limit=40`);
    if (!res.ok) return null;
    return await res.json();
  } catch (_) {
    return null;
  }
}

async function adminListCached(env) {
  if (!env.MEDIA_META) {
    return json({ ok: true, items: [], hint: 'Bind KV MEDIA_META to keep a persistent cache list' });
  }
  const raw = await env.MEDIA_META.get(CACHED_LIST_KEY);
  const list = raw ? JSON.parse(raw) : [];
  const items = [];
  for (const id of list.slice(0, 100)) {
    const cut = String(id).indexOf(':');
    if (cut < 0) continue;
    const ch = id.slice(0, cut);
    const mid = id.slice(cut + 1);
    const meta = await env.MEDIA_META.get(cachedMetaKey(ch, mid));
    if (meta) items.push(JSON.parse(meta));
    else items.push({ id });
  }
  return json({ ok: true, total: list.length, items });
}

async function adminPrewarm(request, env, ctx) {
  if (!checkSecret(request, env)) return new Response('Unauthorized', { status: 401 });
  const body = await request.json();
  const channelId = String(body.channel_id || '');
  const messageId = String(body.message_id || '');
  const fileName = body.file_name || 'stream.mkv';
  const maxMb = Number(body.max_mb || env.PREWARM_MB || DEFAULT_PREWARM_MB);
  if (!channelId || !messageId) return json({ ok: false, error: 'channel_id + message_id required' }, 400);

  const result = await prewarmOne(env, channelId, messageId, fileName, maxMb, ctx);
  return json(result, result.ok ? 200 : 502);
}

async function adminPrewarmHot(request, env, ctx) {
  if (!checkSecret(request, env)) return new Response('Unauthorized', { status: 401 });
  const cats = await loadCategories(env);
  if (!cats) return json({ ok: false, error: 'no index' }, 404);
  const list = (cats.hot_prewarm || []).slice(0, 8);
  const results = [];
  for (const item of list) {
    const r = await prewarmOne(env, item.channel_id, item.message_id, item.file_name, DEFAULT_PREWARM_MB, ctx);
    results.push({ id: item.id || `${item.channel_id}:${item.message_id}`, ...r });
  }
  return json({ ok: true, count: results.length, results });
}

async function resolveFileSize(downloadUrl) {
  let fileSize = 0;
  let contentType = 'video/x-matroska';
  const head = await fetch(downloadUrl, { method: 'HEAD' });
  if (head.ok || head.status === 206) {
    const cl = head.headers.get('Content-Length');
    if (cl) fileSize = parseInt(cl, 10) || 0;
    const ut = head.headers.get('Content-Type');
    if (ut && ut.startsWith('video/')) contentType = ut;
  }
  if (!fileSize) {
    const probe = await fetch(downloadUrl, { headers: { Range: 'bytes=0-0' } });
    const cr = probe.headers.get('Content-Range');
    if (cr && cr.includes('/')) fileSize = parseInt(cr.split('/').pop(), 10) || 0;
    const ut = probe.headers.get('Content-Type');
    if (ut && ut.startsWith('video/')) contentType = ut;
    try { await probe.arrayBuffer(); } catch (_) { /* ignore */ }
  }
  return { fileSize, contentType };
}

async function putSegment(channelId, messageId, index, buf, contentType) {
  const cache = caches.default;
  const key = segCacheKey(channelId, messageId, index);
  const res = new Response(buf, {
    status: 200,
    headers: {
      'Content-Type': contentType,
      'Content-Length': String(buf.byteLength),
      'Cache-Control': `public, max-age=${SEGMENT_TTL}`,
      'X-Segment-Index': String(index),
    },
  });
  await cache.put(key, res);
}

async function prewarmOne(env, channelId, messageId, fileName, maxMb, ctx) {
  const API_BASE = (env.TELEGRAM_API_URL || '').replace(/\/$/, '');
  if (!API_BASE) return { ok: false, error: 'TELEGRAM_API_URL missing' };
  const name = fileName || 'stream.mkv';
  const downloadUrl = `${API_BASE}/stream/${channelId}/${messageId}?name=${encodeURIComponent(name)}`;

  try {
    const { fileSize, contentType } = await resolveFileSize(downloadUrl);
    if (!fileSize) return { ok: false, error: 'unknown file size' };

    const maxBytes = Math.min(fileSize, Math.max(8, maxMb) * SEGMENT_SIZE);
    const totalSeg = Math.ceil(fileSize / SEGMENT_SIZE);
    const warmSeg = Math.ceil(maxBytes / SEGMENT_SIZE);
    let bytes = 0;
    let warmed = 0;

    for (let i = 0; i < warmSeg; i++) {
      const start = i * SEGMENT_SIZE;
      const end = Math.min(start + SEGMENT_SIZE - 1, fileSize - 1);
      const upstream = await fetch(downloadUrl, {
        headers: { Range: `bytes=${start}-${end}` },
      });
      if (!upstream.ok && upstream.status !== 206) {
        return { ok: false, error: `segment ${i} upstream ${upstream.status}`, warmed, bytes };
      }
      const buf = new Uint8Array(await upstream.arrayBuffer());
      await putSegment(channelId, messageId, i, buf, contentType);
      bytes += buf.byteLength;
      warmed += 1;
    }

    // Persist file meta in edge cache
    const metaRes = new Response(null, {
      status: 200,
      headers: {
        'X-File-Size': String(fileSize),
        'Content-Type': contentType,
        'Cache-Control': `public, max-age=${SEGMENT_TTL}`,
      },
    });
    await caches.default.put(metaCacheKey(channelId, messageId), metaRes);

    const record = {
      id: `${channelId}:${messageId}`,
      channel_id: channelId,
      message_id: messageId,
      file_name: name,
      file_size: fileSize,
      content_type: contentType,
      segments_warmed: warmed,
      segments_total: totalSeg,
      bytes_warmed: bytes,
      cached_at: Math.floor(Date.now() / 1000),
      // Head cache — rest fills on watch (write-through)
      mode: warmed >= totalSeg ? 'full' : 'head',
    };

    if (env.MEDIA_META) {
      // No expirationTtl → list stays until you delete
      await env.MEDIA_META.put(cachedMetaKey(channelId, messageId), JSON.stringify(record));
      const raw = await env.MEDIA_META.get(CACHED_LIST_KEY);
      const list = raw ? JSON.parse(raw) : [];
      const id = record.id;
      if (!list.includes(id)) list.unshift(id);
      await env.MEDIA_META.put(CACHED_LIST_KEY, JSON.stringify(list.slice(0, 500)));
    }

    return {
      ok: true,
      ...record,
      note: record.mode === 'head'
        ? `Cached first ${warmed} MiB. Rest fills while you watch.`
        : 'Fully cached in edge segments.',
    };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

async function stremioCatalog(path, url, env, origin) {
  const parts = path.replace(/\.json$/, '').split('/').filter(Boolean);
  let catalogId = parts[2] || '';
  let search = url.searchParams.get('search') || '';
  const searchPath = parts.find((p) => p.startsWith('search='));
  if (searchPath) search = decodeURIComponent(searchPath.slice(7));

  const cats = await loadCategories(env);
  let items = [];
  if (cats) {
    if (catalogId === 'tamil-latest') items = cats.tamil_latest || [];
    else if (catalogId === 'tamil-popular') items = cats.tamil_popular || [];
    else if (catalogId === 'english-latest') items = cats.english_latest || [];
    else if (catalogId === 'english-popular') items = cats.english_popular || [];
    else if (catalogId === 'multi-audio') items = cats.multi_audio_latest || [];
  }

  if (search) {
    const q = search.toLowerCase();
    items = items.filter((i) => {
      const fname = (i.file_name || '').toLowerCase();
      const title = ((i.info || {}).title || '').toLowerCase();
      return fname.includes(q) || title.includes(q);
    });
  }

  const metas = items.slice(0, 100).map((item) => {
    const info = item.info || {};
    const langs = (info.languages || []).join(', ');
    const name = info.title || item.file_name || 'Untitled';
    return {
      id: `tg:${item.channel_id}:${item.message_id}`,
      type: info.media_type === 'series' ? 'series' : 'movie',
      name,
      releaseInfo: info.year ? String(info.year) : undefined,
      poster: (info.tmdb && info.tmdb.poster) || undefined,
      description: [info.quality, info.source, info.size, langs, info.multi_audio ? 'Multi-audio' : '']
        .filter(Boolean).join(' · '),
    };
  });

  return json({ metas }, 200);
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
      // Cache source lists briefly so reopen / binge is snappy
      'Cache-Control': 'public, max-age=60',
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

  // Cinemeta year is often wrong/offset (e.g. Obsession tagged 2026, files say 2025).
  const year = extractYear(meta.year || meta.releaseInfo);
  const queries = buildSearchQueries(meta.name, year, season, episode);

  // Two-wave parallel search: primary queries first, then year±1 fallback only if needed.
  // Avoids hammering Telegram with 4×channels RPCs every play click.
  const primary = queries.slice(0, Math.min(2, queries.length));
  const fallback = queries.slice(primary.length);

  async function fetchQuery(q) {
    try {
      const res = await fetch(`${API_BASE}/search?q=${encodeURIComponent(q)}`);
      if (!res.ok) return [];
      const data = await res.json();
      return data.results || [];
    } catch (_) {
      return [];
    }
  }

  const results = [];
  const seen = new Set();
  function ingest(items) {
    for (const item of items) {
      const key = `${item.channel_id}:${item.message_id}`;
      if (seen.has(key)) continue;
      if (!matchesMeta(item, meta.name, year, season, episode, type)) continue;
      seen.add(key);
      results.push(item);
    }
  }

  const primaryHits = await Promise.all(primary.map(fetchQuery));
  for (const items of primaryHits) ingest(items);

  if (results.length < 3 && fallback.length) {
    const more = await Promise.all(fallback.map(fetchQuery));
    for (const items of more) ingest(items);
  }

  // Prefer exact/near year, then quality
  results.sort((a, b) => scoreItem(b, year) - scoreItem(a, year));

  return results.slice(0, 12).map((item) => {
    const info = item.info || {};
    const label = [info.quality, info.source, info.size].filter(Boolean).join(' · ') || item.file_name || 'Telegram';
    return makeStream(origin, item.channel_id, item.message_id, '🌊 DarkWave', label, item.file_name);
  });
}

function extractYear(v) {
  if (v == null || v === '') return '';
  const m = String(v).match(/\b((?:19|20)\d{2})\b/);
  return m ? m[1] : '';
}

function scoreItem(item, year) {
  const fname = (item.file_name || '').toLowerCase();
  const info = item.info || {};
  let s = 0;
  if (year) {
    const y = Number(year);
    const years = [...fname.matchAll(/\b((?:19|20)\d{2})\b/g)].map((m) => Number(m[1]));
    if (years.includes(y)) s += 100;
    else if (years.some((fy) => Math.abs(fy - y) <= 1)) s += 60;
    else if (info.year && Math.abs(Number(info.year) - y) <= 1) s += 40;
  }
  if (/2160p|4k/.test(fname)) s += 30;
  else if (/1080p/.test(fname)) s += 20;
  else if (/720p/.test(fname)) s += 10;
  if (/bluray|blu.?ray|web-?dl|webrip/i.test(fname)) s += 5;
  return s;
}

async function fetchCinemeta(type, imdbId) {
  const urls = [
    `${CINEMETA}/meta/${type}/${imdbId}.json`,
    `https://cinemeta-live.strem.io/meta/${type}/${imdbId}.json`,
  ];
  for (const url of urls) {
    try {
      const res = await fetch(url);
      if (!res.ok) continue;
      const data = await res.json();
      if (data?.meta?.name) return data.meta;
    } catch (_) {
      /* try next */
    }
  }
  return null;
}

function buildSearchQueries(name, year, season, episode) {
  const clean = String(name).replace(/[^\w\s]/g, ' ').replace(/\s+/g, ' ').trim();
  const qs = [];
  if (season != null && episode != null) {
    const s = String(season).padStart(2, '0');
    const e = String(episode).padStart(2, '0');
    qs.push(`${clean} S${s}E${e}`);
    qs.push(`${clean} ${Number(season)}x${e}`);
  }
  if (year) qs.push(`${clean} ${year}`);
  // Also try year-1 / year+1 — Cinemeta often off by one
  if (year) {
    const y = Number(year);
    if (y > 1900) {
      qs.push(`${clean} ${y - 1}`);
      qs.push(`${clean} ${y + 1}`);
    }
  }
  qs.push(clean);
  return [...new Set(qs.filter(Boolean))];
}

function matchesMeta(item, name, year, season, episode, type) {
  const fname = (item.file_name || '').toLowerCase();
  const info = item.info || {};
  const hay = `${fname} ${(info.title || '').toLowerCase()}`;

  // Drop obvious TV episodes from movie stream lists
  if (type === 'movie') {
    if (/s\s*\d{1,2}\s*e\s*\d{1,2}|\d{1,2}\s*x\s*\d{1,2}/i.test(fname)) return false;
    if (info.media_type === 'series' && info.season != null) return false;
  }

  const tokens = String(name)
    .toLowerCase()
    .replace(/[^\w\s]/g, ' ')
    .split(/\s+/)
    .filter((t) => t.length > 1);
  if (!tokens.length) return false;
  // Every title token must appear in the filename
  for (const t of tokens) {
    if (!hay.includes(t)) return false;
  }

  // Soft reject clear "OtherWord Obsession" when query is single-word title,
  // but NEVER reject if year matches (±1) — better show a source than none.
  if (tokens.length === 1 && type === 'movie') {
    const t = tokens[0];
    const yearOk = yearMatches(fname, info, year);
    if (!yearOk) {
      const stripped = fname
        .replace(/\[[^\]]*]/g, ' ')
        .replace(/\([^)]*\)/g, ' ')
        .replace(/\.(mkv|mp4|avi|mov|m4v|webm)$/i, ' ')
        .replace(/[._]+/g, ' ')
        .trim();
      if (!stripped.startsWith(t) && !stripped.startsWith(`the ${t}`) && !stripped.startsWith(`a ${t}`)) {
        const idx = stripped.indexOf(` ${t} `) >= 0
          ? stripped.indexOf(` ${t} `)
          : (stripped.endsWith(` ${t}`) ? stripped.lastIndexOf(` ${t}`) : -1);
        if (idx > 0) {
          const before = stripped.slice(0, idx).trim().split(/\s+/).pop();
          if (before && !['the', 'a', 'an'].includes(before)) return false;
        } else if (!stripped.startsWith(t)) {
          // token only appears mid/end with junk prefix
          const re = new RegExp(`(?:^|\\s)([a-z0-9]+)[\\s]+${escapeRe(t)}(?:\\s|$)`);
          const m = stripped.match(re);
          if (m && m[1] && !['the', 'a', 'an'].includes(m[1])) return false;
        }
      }
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

  // Year: allow ±1. If filename has no year, keep (don't wipe all sources).
  if (year) {
    const years = [...fname.matchAll(/\b((?:19|20)\d{2})\b/g)].map((m) => Number(m[1]));
    if (years.length && !yearMatches(fname, info, year)) return false;
  }
  return true;
}

function yearMatches(fname, info, year) {
  if (!year) return true;
  const y = Number(String(year).slice(0, 4));
  if (!y) return true;
  const years = [...String(fname).matchAll(/\b((?:19|20)\d{2})\b/g)].map((m) => Number(m[1]));
  if (years.some((fy) => Math.abs(fy - y) <= 1)) return true;
  if (info?.year && Math.abs(Number(info.year) - y) <= 1) return true;
  return years.length === 0;
}

function escapeRe(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
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

// ── Write-through stream cache (Cache API — free, no R2) ─────────────────────
// Bot /cache prewarms head; watch fills rest. Segments TTL = 30 days.

function parseRange(rangeHeader, fileSize) {
  let start = 0;
  let end = fileSize - 1;
  if (!rangeHeader) return { start, end, isRange: false };
  try {
    const unit = rangeHeader.split('=')[0].trim().toLowerCase();
    if (unit !== 'bytes') return { start, end, isRange: false };
    const rng = rangeHeader.split('=')[1].trim();
    const [first, second] = rng.split('-');
    if (first === '' && second) {
      const suffix = parseInt(second, 10);
      start = Math.max(0, fileSize - suffix);
    } else {
      start = first ? parseInt(first, 10) : 0;
      end = second !== undefined && second !== '' ? parseInt(second, 10) : fileSize - 1;
    }
    if (Number.isNaN(start) || Number.isNaN(end)) return { start: 0, end: fileSize - 1, isRange: false };
    end = Math.min(end, fileSize - 1);
    if (start >= fileSize) return { start, end: fileSize - 1, isRange: true, unsatisfiable: true };
    if (end < start) end = fileSize - 1;
    return { start, end, isRange: true };
  } catch (_) {
    return { start: 0, end: fileSize - 1, isRange: false };
  }
}

async function resolveUpstreamUrl(API_BASE, BOT_TOKEN, channelId, fileId, allowedChannels) {
  if (!API_BASE.includes('api.telegram.org')) {
    return `${API_BASE}/stream/${channelId || allowedChannels[0]}/${fileId}`;
  }
  if (!BOT_TOKEN) throw new Error('BOT_TOKEN missing');
  const infoRes = await fetch(`${API_BASE}/bot${BOT_TOKEN}/getFile?file_id=${encodeURIComponent(fileId)}`);
  const info = await infoRes.json();
  if (!info.ok || !info.result?.file_path) throw new Error('File not found');
  return `${API_BASE}/file/bot${BOT_TOKEN}/${info.result.file_path}`;
}

async function getFileMeta(downloadUrl, channelId, messageId, mimeFallback, ctx) {
  const cache = caches.default;
  const key = metaCacheKey(channelId, messageId);
  const hit = await cache.match(key);
  if (hit) {
    const size = Number(hit.headers.get('X-File-Size') || 0);
    const type = hit.headers.get('Content-Type') || mimeFallback;
    if (size > 0) return { fileSize: size, contentType: type, cacheStatus: 'HIT' };
  }

  const upstream = await fetch(downloadUrl, { method: 'HEAD' });
  // Some origins reject HEAD — fall back to tiny Range probe
  let fileSize = 0;
  let contentType = mimeFallback;
  if (upstream.ok || upstream.status === 206) {
    const cr = upstream.headers.get('Content-Range'); // bytes */SIZE or bytes a-b/SIZE
    const cl = upstream.headers.get('Content-Length');
    if (cr && cr.includes('/')) {
      fileSize = parseInt(cr.split('/').pop(), 10) || 0;
    } else if (cl) {
      fileSize = parseInt(cl, 10) || 0;
    }
    const ut = upstream.headers.get('Content-Type');
    if (ut && ut.startsWith('video/')) contentType = ut;
  }
  if (!fileSize) {
    const probe = await fetch(downloadUrl, { headers: { Range: 'bytes=0-0' } });
    const cr = probe.headers.get('Content-Range');
    if (cr && cr.includes('/')) fileSize = parseInt(cr.split('/').pop(), 10) || 0;
    const ut = probe.headers.get('Content-Type');
    if (ut && ut.startsWith('video/')) contentType = ut;
    // drain body
    try { await probe.arrayBuffer(); } catch (_) { /* ignore */ }
  }
  if (!fileSize) throw new Error('Unknown file size');

  const cached = new Response(null, {
    status: 200,
    headers: {
      'X-File-Size': String(fileSize),
      'Content-Type': contentType,
      'Cache-Control': `public, max-age=${SEGMENT_TTL}`,
    },
  });
  ctx.waitUntil(cache.put(key, cached.clone()));
  return { fileSize, contentType, cacheStatus: 'MISS' };
}

async function getSegment(downloadUrl, channelId, messageId, index, fileSize, contentType, ctx) {
  const cache = caches.default;
  const key = segCacheKey(channelId, messageId, index);
  const hit = await cache.match(key);
  if (hit) {
    return { buf: new Uint8Array(await hit.arrayBuffer()), cacheStatus: 'HIT' };
  }

  const start = index * SEGMENT_SIZE;
  if (start >= fileSize) return { buf: new Uint8Array(0), cacheStatus: 'MISS' };
  const end = Math.min(start + SEGMENT_SIZE - 1, fileSize - 1);

  const upstream = await fetch(downloadUrl, {
    headers: { Range: `bytes=${start}-${end}` },
  });
  if (!upstream.ok && upstream.status !== 206) {
    throw new Error(`Upstream segment error: ${upstream.status}`);
  }
  const buf = new Uint8Array(await upstream.arrayBuffer());
  const toStore = new Response(buf, {
    status: 200,
    headers: {
      'Content-Type': contentType,
      'Content-Length': String(buf.byteLength),
      'Cache-Control': `public, max-age=${SEGMENT_TTL}`,
      'X-Segment-Index': String(index),
    },
  });
  // Await put so the segment is durable before we move on; also register waitUntil.
  const put = cache.put(key, toStore.clone());
  if (ctx && typeof ctx.waitUntil === 'function') ctx.waitUntil(put);
  await put;
  return { buf, cacheStatus: 'MISS' };
}

async function mediaProxy(request, env, url, forceDownload, ctx) {
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

  const bypass = url.searchParams.get('nocache') === '1';

  try {
    const downloadUrl = await resolveUpstreamUrl(
      API_BASE, BOT_TOKEN, channelId, fileId, allowedChannels
    );

    if (bypass) {
      return passthroughProxy(request, downloadUrl, fileName, mimeType, forceDownload);
    }

    // Segment Cache API path (bot /cache prewarm + write-through on watch)
    return serveCachedStream(
      request,
      downloadUrl,
      channelId || allowedChannels[0],
      fileId,
      fileName,
      mimeType,
      forceDownload,
      ctx
    );
  } catch (err) {
    return new Response(`Error: ${err.message}`, { status: 500 });
  }
}

async function passthroughProxy(request, downloadUrl, fileName, mimeType, forceDownload) {
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
  const contentType = (upstreamType && upstreamType.startsWith('video/')) ? upstreamType : mimeType;
  const resHeaders = new Headers({
    'Access-Control-Allow-Origin': '*',
    'Accept-Ranges': 'bytes',
    'Content-Type': contentType,
    'Cache-Control': 'no-store',
    'CF-Cache-Status': 'BYPASS',
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
  if (request.method === 'HEAD') return new Response(null, { status, headers: resHeaders });
  return new Response(upstream.body, { status, headers: resHeaders });
}

async function serveCachedStream(request, downloadUrl, channelId, messageId, fileName, mimeType, forceDownload, ctx) {
  const wait = ctx && typeof ctx.waitUntil === 'function'
    ? (p) => ctx.waitUntil(p)
    : (p) => p; // still await puts inline when no ctx
  const fakeCtx = { waitUntil: wait };

  const meta = await getFileMeta(downloadUrl, channelId, messageId, mimeType, fakeCtx);
  const { fileSize, contentType } = meta;
  const rangeHeader = request.headers.get('Range');
  const { start, end, isRange, unsatisfiable } = parseRange(rangeHeader, fileSize);

  if (unsatisfiable) {
    return new Response(null, {
      status: 416,
      headers: {
        'Content-Range': `bytes */${fileSize}`,
        'Access-Control-Allow-Origin': '*',
      },
    });
  }

  const length = end - start + 1;
  const status = isRange ? 206 : 200;

  // Peek first segment to know HIT/MISS for response header (also warms start of stream)
  const firstIdx = Math.floor(start / SEGMENT_SIZE);
  const firstSeg = await getSegment(downloadUrl, channelId, messageId, firstIdx, fileSize, contentType, fakeCtx);
  const cacheStatus = firstSeg.cacheStatus;

  const resHeaders = new Headers({
    'Access-Control-Allow-Origin': '*',
    'Accept-Ranges': 'bytes',
    'Content-Type': contentType,
    'Content-Length': String(length),
    'Cache-Control': 'public, max-age=3600',
    'CF-Cache-Status': cacheStatus,
    'X-Cache': cacheStatus,
    'Content-Disposition': forceDownload
      ? `attachment; filename="${encodeURIComponent(fileName)}"`
      : `inline; filename="${encodeURIComponent(fileName)}"`,
  });
  if (isRange) {
    resHeaders.set('Content-Range', `bytes ${start}-${end}/${fileSize}`);
  }

  if (request.method === 'HEAD') {
    return new Response(null, { status, headers: resHeaders });
  }

  // Stream: emit already-fetched first segment slice, then continue
  let seg = firstIdx;
  const last = Math.floor(end / SEGMENT_SIZE);
  let firstBuf = firstSeg.buf;

  const stream = new ReadableStream({
    async pull(controller) {
      try {
        while (seg <= last) {
          let buf = firstBuf;
          firstBuf = null;
          if (!buf) {
            const got = await getSegment(
              downloadUrl, channelId, messageId, seg, fileSize, contentType, fakeCtx
            );
            buf = got.buf;
          }
          if (!buf.byteLength) {
            controller.close();
            return;
          }
          const segStart = seg * SEGMENT_SIZE;
          const from = Math.max(0, start - segStart);
          const to = Math.min(buf.byteLength, end - segStart + 1);
          if (to > from) controller.enqueue(buf.subarray(from, to));

          // Prefetch next segment while client drains this one
          if (seg + 1 <= last) {
            fakeCtx.waitUntil(
              getSegment(downloadUrl, channelId, messageId, seg + 1, fileSize, contentType, fakeCtx)
                .catch(() => null)
            );
          }
          seg += 1;
          return; // one segment per pull — backpressure friendly
        }
        controller.close();
      } catch (err) {
        controller.error(err);
      }
    },
  });

  return new Response(stream, { status, headers: resHeaders });
}
