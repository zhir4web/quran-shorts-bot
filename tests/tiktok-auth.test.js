const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createHandler, encryptRecord, decryptRecord } = require('../api/tiktok-auth');

function harness({ record = {}, fetch: fetchImpl = async () => ({ ok: true, json: async () => ({}) }), now = 1000000 } = {}) {
  const store = { record: structuredClone(record), saves: 0,
    async load() { return structuredClone(this.record); },
    async save(value) { this.record = structuredClone(value); this.saves += 1; },
  };
  const env = { DASHBOARD_KEY: 'dashboard-test-key', TIKTOK_CLIENT_KEY: 'client-key',
    TIKTOK_CLIENT_SECRET: 'never-return-this-secret', TIKTOK_REDIRECT_URI: 'https://example.test/api/tiktok/callback',
    BLOB_READ_WRITE_TOKEN: 'blob-server-token' };
  const handler = createHandler({ env, store, fetch: fetchImpl, now: () => now });
  const calls = [];
  async function call(route, { method = 'GET', key = 'dashboard-test-key', query = {}, headers = {} } = {}) {
    const req = { method, url: `/api/tiktok-auth?route=${route}`, query: { route, ...query },
      headers: { host: 'example.test', ...(key ? { 'x-dashboard-key': key } : {}), ...headers } };
    const res = { statusCode: 0, headers: {}, setHeader(k, v) { this.headers[k] = v; },
      status(code) { this.statusCode = code; return this; }, json(body) { this.body = body; return this; },
      redirect(code, location) { this.statusCode = code; this.location = location; return this; } };
    await handler(req, res);
    calls.push({ req, res });
    return res;
  }
  return { store, env, calls, call };
}

test('encrypted token record cannot be decoded with a different server secret', () => {
  const value = { tokens: { access_token: 'private-access-value' } };
  const encoded = encryptRecord(value, 'server-only-blob-token');
  assert.deepEqual(decryptRecord(encoded, 'server-only-blob-token'), value);
  assert.throws(() => decryptRecord(encoded, 'wrong-token'));
  assert.doesNotMatch(encoded, /private-access-value/);
});

test('TikTok status requires the dashboard key and never returns token material', async () => {
  const app = harness({ record: { tokens: { access_token: 'private-access-value', refresh_token: 'private-refresh',
    scope: 'video.upload', expires_at: 2000000, refresh_expires_at: 3000000 } } });
  const denied = await app.call('status', { key: '' });
  assert.equal(denied.statusCode, 401);
  const result = await app.call('status');
  assert.equal(result.body.connected, true);
  assert.equal(result.body.scope, 'video.upload');
  assert.equal(JSON.stringify(result.body).includes('private-access-value'), false);
  assert.equal(JSON.stringify(result.body).includes('private-refresh'), false);
});

test('connect creates expiring state and callback exchanges a code once', async () => {
  const requests = [];
  const app = harness({ fetch: async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ data: { access_token: 'access-token-value', refresh_token: 'refresh-token-value',
      open_id: 'user-id', scope: 'video.upload', expires_in: 86400, refresh_expires_in: 31536000 } }) };
  } });
  const connect = await app.call('connect');
  assert.equal(connect.statusCode, 200);
  const authorization = new URL(connect.body.authorization_url);
  const state = authorization.searchParams.get('state');
  assert.ok(state);
  assert.equal(authorization.searchParams.get('scope'), 'video.upload');
  assert.equal(authorization.searchParams.get('redirect_uri'), app.env.TIKTOK_REDIRECT_URI);
  const callback = await app.call('callback', { key: '', query: { code: 'one-time-code', state },
    headers: { 'sec-fetch-site': 'cross-site' } });
  assert.equal(callback.statusCode, 302);
  assert.equal(callback.location, '/?tiktok=connected');
  assert.equal(requests.length, 1);
  assert.equal(app.store.record.tokens.access_token, 'access-token-value');
  assert.equal(app.store.record.oauth_state, undefined);
  const replay = await app.call('callback', { key: '', query: { code: 'one-time-code', state } });
  assert.equal(replay.location, '/?tiktok=failed');
  assert.equal(requests.length, 1);
});

test('refresh persists TikTok rotated refresh token before returning access token', async () => {
  const calls = [];
  const app = harness({ record: { tokens: { access_token: 'expired-access', refresh_token: 'old-refresh',
    scope: 'video.upload', expires_at: 999000, refresh_expires_at: 3000000 } },
    fetch: async (_url, options) => {
      calls.push(options);
      return { ok: true, json: async () => ({ data: { access_token: 'fresh-access', refresh_token: 'rotated-refresh',
        scope: 'video.upload', expires_in: 86400, refresh_expires_in: 30000000 } }) };
    } });
  const result = await app.call('access-token', { method: 'POST' });
  assert.equal(result.body.access_token, 'fresh-access');
  assert.equal(app.store.record.tokens.refresh_token, 'rotated-refresh');
  assert.equal(calls.length, 1);
  assert.equal(app.store.saves, 1);
});

test('wrong OAuth state and missing upload scope cannot connect an account', async () => {
  const app = harness({ record: { oauth_state: { value: 'expected-state', created_at: 1000000 } },
    fetch: async () => ({ ok: true, json: async () => ({ data: { access_token: 'access-token-value',
      refresh_token: 'refresh-token-value', scope: 'user.info.basic', expires_in: 10, refresh_expires_in: 20 } }) }) });
  const wrong = await app.call('callback', { key: '', query: { code: 'code', state: 'wrong-state' } });
  assert.equal(wrong.location, '/?tiktok=failed');
  const deniedScope = await app.call('callback', { key: '', query: { code: 'code', state: 'expected-state' } });
  assert.equal(deniedScope.location, '/?tiktok=scope');
  assert.equal(app.store.record.tokens, undefined);
});
