const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../dashboard/index.html'), 'utf8');
const script = fs.readFileSync(path.join(__dirname, '../dashboard/app.js'), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));
const response = (status, data) => ({ status, ok: status < 400, json: async () => data });
const overview = () => ({ channel: { enabled: true, id: 'configured' }, integrations: { youtube: false },
  health: { state: 'attention', issues: ['uncertain_upload'], uncertain_uploads: [{ id: 'pending' }] },
  schedule: { slots: ['06:00', '12:00', '18:00'], today_count: 1, target: 3,
    slot_states: [{ completed: false }, { completed: false }, { completed: true }] }, recent: [] });

function element() {
  const listeners = new Map();
  const classes = new Set();
  return { textContent: '', innerHTML: '', value: '', dataset: {}, disabled: false, style: {},
    classList: { add: (x) => classes.add(x), remove: (x) => classes.delete(x),
      toggle: (x, on) => on ? classes.add(x) : classes.delete(x) },
    addEventListener(type, callback) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(callback); },
    removeEventListener(type, callback) { listeners.get(type)?.delete(callback); },
    async emit(type, extra = {}) { await Promise.all([...listeners.get(type) || []].map((fn) => fn({ preventDefault() {}, ...extra }))); },
    showModal() { this.open = true; }, close() { this.open = false; }, focus() {},
  };
}

function boot(fetch) {
  const ids = new Map([...html.matchAll(/id="([^"]+)"/g)].map((m) => ['#' + m[1], element()]));
  const action = element(); action.dataset.action = 'publish'; action.dataset.count = '1';
  const preview = element(); preview.dataset.action = 'preview';
  const report = element(); report.dataset.action = 'report';
  const selectors = new Map([['[data-action]', [action, preview, report]]]);
  const document = { querySelector(selector) {
    if (selector.startsWith('#')) { assert.ok(ids.has(selector), `Missing UI element ${selector}`); return ids.get(selector); }
    return element();
  }, querySelectorAll: (selector) => selectors.get(selector) || [] };
  const storage = new Map();
  let interval;
  let registered = false;
  vm.runInNewContext(script, { document, fetch, AbortController,
    sessionStorage: { getItem: (k) => storage.get(k), setItem: (k, v) => storage.set(k, v) },
    navigator: { serviceWorker: { register: async () => { registered = true; } } },
    window: { setTimeout: () => 1, clearTimeout() {}, setInterval: (fn) => { interval = fn; }, scrollTo() {} },
  });
  return { ids, action, preview, report, storage, tick: () => interval(), registered: () => registered };
}

test('uncertain upload disables publishing and an ID does not claim a verified connection', async () => {
  const ui = boot(async () => response(200, overview()));
  await flush();
  assert.equal(ui.action.disabled, true);
  assert.equal(ui.preview.disabled, false);
  assert.match(ui.ids.get('#youtube-connection').textContent, /نەپشکنراوە/);
  assert.equal(ui.ids.get('#uncertain-list').textContent, 'pending');
  const rows = ui.ids.get('#schedule-list').innerHTML.match(/class="schedule-row [^"]*"/g);
  assert.equal(rows.length, 3);
  assert.doesNotMatch(rows[0], /done/);
  assert.match(rows[2], /done/);
  assert.equal(ui.registered(), true);
});

test('refresh failure clears healthy indicators and prevents stale publishing', async () => {
  let fail = false;
  const data = overview(); data.health = { state: 'healthy', issues: [], uncertain_uploads: [] };
  const ui = boot(async () => { if (fail) throw new Error('offline'); return response(200, data); });
  await flush();
  assert.equal(ui.action.disabled, false);
  fail = true;
  await ui.tick();
  await flush();
  assert.equal(ui.action.disabled, true);
  assert.equal(ui.ids.get('#health-state').textContent, 'نادیارە');
  assert.equal(ui.ids.get('#metrics-check').textContent, '!');
});

test('canceling authentication closes the dialog without saving or repeatedly prompting', async () => {
  let calls = 0;
  const ui = boot(async () => { calls++; return response(401, { error: 'key required' }); });
  await flush();
  assert.equal(ui.ids.get('#key-dialog').open, true);
  ui.ids.get('#key-input').value = 'must-not-save';
  await ui.ids.get('#key-form').emit('submit', { submitter: { value: 'cancel' } });
  await flush();
  assert.equal(ui.ids.get('#key-dialog').open, false);
  assert.equal(ui.storage.get('quran_dashboard_key'), '');
  await ui.tick();
  assert.equal(calls, 1);
});

test('rapid repeated clicks create only one publication request', async () => {
  const data = overview(); data.health = { state: 'healthy', issues: [], uncertain_uploads: [] };
  let calls = 0, complete;
  const ui = boot(async (_path, options) => {
    if (options.method === 'POST') { calls++; return new Promise((resolve) => { complete = resolve; }); }
    return response(200, data);
  });
  await flush();
  const first = ui.action.emit('click');
  await ui.action.emit('click');
  assert.equal(calls, 1);
  assert.equal(ui.action.disabled, true);
  complete(response(202, { requested_count: 1, dispatched: 1 }));
  await first;
  assert.equal(ui.action.disabled, false);
});

test('YouTube status report is available without enabling publication and does not request a post', async () => {
  const data = overview();
  data.channel.enabled = false;
  data.health.uncertain_uploads = [{ id: 'needs-review' }];
  const requests = [];
  const ui = boot(async (_path, options) => {
    requests.push(options);
    return options.method === 'POST' ? response(202, { requested_count: 1 }) : response(200, data);
  });
  await flush();
  assert.equal(ui.action.disabled, true);
  assert.equal(ui.report.disabled, false);
  await ui.report.emit('click');
  const body = JSON.parse(requests.find((item) => item.method === 'POST').body);
  assert.equal(body.mode, 'report');
  assert.equal(body.count, 1);
  assert.match(ui.ids.get('#toast').textContent, /GitHub Actions/);
});

test('analytics section renders retention bars, CTA variants and playlists safely', async () => {
  const data = overview();
  data.analytics = {
    retention: [
      { id: 'a', url: 'https://www.youtube.com/shorts/a', reciter: 'Reader <b>', percentage: 72, views: 500 },
      { id: 'b', url: null, reciter: 'No link', percentage: 140, views: 5 },
    ],
    cta_variants: [{ variant: 0, videos: 5, avg_view_percentage: 61, avg_views: 150.5 }],
    playlists: [{ surah: 'Al-Ikhlas', url: 'https://www.youtube.com/playlist?list=PL1' },
                { surah: 'Broken <img>', url: null }],
  };
  const ui = boot(async () => response(200, data));
  await flush();
  const retention = ui.ids.get('#retention-list').innerHTML;
  assert.match(retention, /Reader &lt;b&gt;/);
  assert.match(retention, /width:100%/);
  assert.match(retention, /width:72%/);
  assert.doesNotMatch(retention, /<b>/);
  const cta = ui.ids.get('#cta-list').innerHTML;
  assert.match(cta, /جۆر 0/);
  assert.match(cta, /150\.5/);
  const playlists = ui.ids.get('#playlist-list').innerHTML;
  assert.match(playlists, /playlist\?list=PL1/);
  assert.match(playlists, /Broken &lt;img&gt;/);
  assert.doesNotMatch(playlists, /<img>/);
});

test('analytics section shows empty-state copy when the ledger has no analytics yet', async () => {
  const ui = boot(async () => response(200, overview()));
  await flush();
  assert.match(ui.ids.get('#retention-list').innerHTML, /بەردەست نییە/);
  assert.match(ui.ids.get('#cta-list').innerHTML, /کۆ نەبووەتەوە/);
  assert.match(ui.ids.get('#playlist-list').innerHTML, /دروست نەبووە/);
});
