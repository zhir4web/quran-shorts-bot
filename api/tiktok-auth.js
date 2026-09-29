const crypto = require('node:crypto');

const AUTHORIZE_URL = 'https://www.tiktok.com/v2/auth/authorize/';
const TOKEN_URL = 'https://open.tiktokapis.com/v2/oauth/token/';
const BLOB_PREFIX = 'quran-shorts-private/tiktok-auth-';
const REQUIRED_SCOPE = 'video.upload';

function safeEqual(left, right) {
  const a = Buffer.from(String(left || ''));
  const b = Buffer.from(String(right || ''));
  return a.length === b.length && a.length > 0 && crypto.timingSafeEqual(a, b);
}

function encryptionKey(blobToken) {
  if (!blobToken) throw new Error('Token storage is not configured');
  return crypto.createHash('sha256').update('quran-shorts/tiktok-token/v1\0').update(blobToken).digest();
}

function encryptRecord(record, blobToken) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', encryptionKey(blobToken), iv);
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(record), 'utf8'), cipher.final()]);
  return JSON.stringify({ version: 1, iv: iv.toString('base64'), tag: cipher.getAuthTag().toString('base64'),
    ciphertext: ciphertext.toString('base64') });
}

function decryptRecord(encoded, blobToken) {
  const row = JSON.parse(encoded);
  if (row.version !== 1 || !row.iv || !row.tag || !row.ciphertext) throw new Error('Invalid encrypted token record');
  const decipher = crypto.createDecipheriv('aes-256-gcm', encryptionKey(blobToken), Buffer.from(row.iv, 'base64'));
  decipher.setAuthTag(Buffer.from(row.tag, 'base64'));
  return JSON.parse(Buffer.concat([decipher.update(Buffer.from(row.ciphertext, 'base64')), decipher.final()]).toString('utf8'));
}

async function createBlobStore(blobToken, fetchImpl) {
  const { list, put, del } = await import('@vercel/blob');
  return {
    async load() {
      const result = await list({ prefix: BLOB_PREFIX, token: blobToken, limit: 20 });
      const latest = (result.blobs || []).sort((a, b) => Date.parse(b.uploadedAt) - Date.parse(a.uploadedAt))[0];
      if (!latest) return {};
      const response = await fetchImpl(latest.url, { cache: 'no-store' });
      if (!response.ok) throw new Error('Token storage read failed');
      return decryptRecord(await response.text(), blobToken);
    },
    async save(record) {
      const result = await list({ prefix: BLOB_PREFIX, token: blobToken, limit: 20 });
      const pathname = BLOB_PREFIX + crypto.randomBytes(16).toString('hex') + '.enc';
      const written = await put(pathname, encryptRecord(record, blobToken), {
        access: 'public', token: blobToken, contentType: 'application/octet-stream',
      });
      const old = (result.blobs || []).filter((blob) => blob.url !== written.url).map((blob) => blob.url);
      if (old.length) await del(old, { token: blobToken }).catch(() => {});
    },
  };
}

function response(res, status, body) {
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  return res.status(status).json(body);
}

function redirect(res, location) {
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  return res.redirect(302, location);
}

function requestRoute(req) {
  if (req.query && typeof req.query.route === 'string') return req.query.route;
  return new URL(req.url || '/', `https://${req.headers.host || 'localhost'}`).searchParams.get('route') || '';
}

function sameSite(req) {
  if (req.headers['sec-fetch-site'] === 'cross-site') return false;
  const origin = req.headers.origin;
  try { return !origin || new URL(origin).host === req.headers.host; }
  catch (_) { return false; }
}

function parseApiBody(payload) {
  if (payload && payload.error && payload.error.code && payload.error.code !== 'ok') {
    throw new Error('TikTok rejected the authorization request');
  }
  const data = payload && typeof payload.data === 'object' ? payload.data : payload;
  if (!data || !data.access_token) throw new Error('TikTok token response was incomplete');
  return data;
}

function scopeIncludes(scope, required) {
  return String(scope || '').split(/[\s,]+/).includes(required);
}

function createHandler(dependencies = {}) {
  const fetchImpl = dependencies.fetch || globalThis.fetch;
  const now = dependencies.now || (() => Date.now());
  const getStore = async (env) => dependencies.store || createBlobStore(env.BLOB_READ_WRITE_TOKEN, fetchImpl);

  return async function handler(req, res) {
    const route = requestRoute(req);
    const env = dependencies.env || process.env;
    const expectedKey = env.DASHBOARD_KEY || '';
    const suppliedKey = req.headers['x-dashboard-key'] || '';
    const isCallback = route === 'callback';

    if (!['connect', 'status', 'access-token', 'callback'].includes(route)) {
      return response(res, 404, { error: 'Not found' });
    }
    if (!isCallback && (!expectedKey || !safeEqual(suppliedKey, expectedKey))) {
      return response(res, 401, { error: 'Dashboard key required' });
    }
    if ((route === 'access-token' && req.method !== 'POST') ||
        (route !== 'access-token' && req.method !== 'GET')) {
      return response(res, 405, { error: 'Method not allowed' });
    }
    if (!['status', 'callback'].includes(route) && !sameSite(req)) return response(res, 403, { error: 'Cross-origin request rejected' });

    const configured = Boolean(env.TIKTOK_CLIENT_KEY && env.TIKTOK_CLIENT_SECRET && env.TIKTOK_REDIRECT_URI && env.BLOB_READ_WRITE_TOKEN);
    if (!configured) {
      if (route === 'status') return response(res, 200, { configured: false, connected: false });
      return isCallback ? redirect(res, '/?tiktok=failed') : response(res, 503, { error: 'TikTok connection is not configured on the server' });
    }

    try {
      const store = await getStore(env);
      if (route === 'status') {
        const record = await store.load();
        const tokens = record.tokens || {};
        const connected = Boolean(tokens.refresh_token && Number(tokens.refresh_expires_at) > now());
        return response(res, 200, { configured: true, connected,
          scope: connected ? String(tokens.scope || '') : '',
          expires_at: connected ? new Date(Number(tokens.expires_at || 0)).toISOString() : null });
      }

      if (route === 'connect') {
        const record = await store.load();
        const state = crypto.randomBytes(32).toString('hex');
        record.oauth_state = { value: state, created_at: now() };
        await store.save(record);
        const authorization = new URL(AUTHORIZE_URL);
        authorization.search = new URLSearchParams({ client_key: env.TIKTOK_CLIENT_KEY,
          response_type: 'code', scope: REQUIRED_SCOPE, redirect_uri: env.TIKTOK_REDIRECT_URI, state }).toString();
        return response(res, 200, { authorization_url: authorization.toString() });
      }

      if (route === 'callback') {
        const query = req.query || Object.fromEntries(new URL(req.url || '/', `https://${req.headers.host || 'localhost'}`).searchParams);
        if (query.error || !query.code || !query.state) return redirect(res, '/?tiktok=cancelled');
        const record = await store.load();
        const pending = record.oauth_state;
        if (!pending || now() - Number(pending.created_at) > 15 * 60 * 1000 || !safeEqual(query.state, pending.value)) {
          return redirect(res, '/?tiktok=failed');
        }
        delete record.oauth_state;
        await store.save(record); // Consume state before exchanging the one-time authorization code.
        const tokenResponse = await fetchImpl(TOKEN_URL, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ client_key: env.TIKTOK_CLIENT_KEY, client_secret: env.TIKTOK_CLIENT_SECRET,
            code: query.code, grant_type: 'authorization_code', redirect_uri: env.TIKTOK_REDIRECT_URI }) });
        if (!tokenResponse.ok) return redirect(res, '/?tiktok=failed');
        const tokenData = parseApiBody(await tokenResponse.json());
        if (!tokenData.refresh_token) return redirect(res, '/?tiktok=failed');
        if (!scopeIncludes(tokenData.scope, REQUIRED_SCOPE)) return redirect(res, '/?tiktok=scope');
        const timestamp = now();
        record.tokens = { access_token: tokenData.access_token, refresh_token: tokenData.refresh_token,
          open_id: tokenData.open_id || '', scope: tokenData.scope,
          expires_at: timestamp + Number(tokenData.expires_in || 0) * 1000,
          refresh_expires_at: timestamp + Number(tokenData.refresh_expires_in || 0) * 1000 };
        await store.save(record);
        return redirect(res, '/?tiktok=connected');
      }

      const record = await store.load();
      const tokens = record.tokens || {};
      if (!tokens.refresh_token || Number(tokens.refresh_expires_at) <= now()) {
        return response(res, 409, { error: 'TikTok authorization expired; reconnect the account' });
      }
      if (Number(tokens.expires_at) <= now() + 5 * 60 * 1000) {
        const refreshResponse = await fetchImpl(TOKEN_URL, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ client_key: env.TIKTOK_CLIENT_KEY, client_secret: env.TIKTOK_CLIENT_SECRET,
            grant_type: 'refresh_token', refresh_token: tokens.refresh_token }) });
        if (!refreshResponse.ok) return response(res, 503, { error: 'Could not refresh TikTok authorization; reconnect and try again' });
        const refreshed = parseApiBody(await refreshResponse.json());
        tokens.access_token = refreshed.access_token;
        tokens.refresh_token = refreshed.refresh_token || tokens.refresh_token;
        tokens.scope = refreshed.scope || tokens.scope;
        tokens.expires_at = now() + Number(refreshed.expires_in || 0) * 1000;
        if (refreshed.refresh_expires_in) tokens.refresh_expires_at = now() + Number(refreshed.refresh_expires_in) * 1000;
        record.tokens = tokens;
        await store.save(record); // TikTok rotates refresh tokens; persist before returning access.
      }
      if (!scopeIncludes(tokens.scope, REQUIRED_SCOPE)) return response(res, 403, { error: 'TikTok video upload permission is missing; reconnect with video.upload' });
      return response(res, 200, { access_token: tokens.access_token, expires_at: tokens.expires_at });
    } catch (_) {
      // Never return or log provider payloads, callback codes, or stored tokens.
      if (isCallback) return redirect(res, '/?tiktok=failed');
      return response(res, 503, { error: 'TikTok connection service is temporarily unavailable' });
    }
  };
}

const defaultHandler = createHandler();
module.exports = defaultHandler;
module.exports.createHandler = createHandler;
module.exports.encryptRecord = encryptRecord;
module.exports.decryptRecord = decryptRecord;
module.exports.scopeIncludes = scopeIncludes;
