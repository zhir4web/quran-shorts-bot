(() => {
  'use strict';

  const state = { overview: null, tiktok: null, manualCount: 1, busy: false, refreshing: false, authCancelled: false };
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#039;', '"': '&quot;'
  }[char]));

  function toast(message, error = false) {
    const node = $('#toast');
    node.textContent = message;
    node.classList.toggle('error', error);
    node.classList.add('show');
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => node.classList.remove('show'), 4200);
  }

  function setSection(name) {
    $$('.section-panel').forEach((panel) => panel.classList.toggle('active-section', panel.dataset.panel === name));
    $$('.nav-item[data-section]').forEach((item) => item.classList.toggle('active', item.dataset.section === name));
    const title = $(`.nav-item[data-section="${name}"]`);
    if (title) $('#page-title').textContent = title.textContent.replace('بەم زووانە', '').trim();
    $('#sidebar').classList.remove('open');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  let memoryKey = '';
  function authKey() { try { return sessionStorage.getItem('quran_dashboard_key') || memoryKey; } catch (_) { return memoryKey; } }
  function saveKey(value) { memoryKey = value; try { sessionStorage.setItem('quran_dashboard_key', value); } catch (_) { /* keep only in memory */ } }

  let keyRequest = null;
  function requestKey() {
    if (keyRequest) return keyRequest;
    keyRequest = new Promise((resolve) => {
      const dialog = $('#key-dialog');
      const form = $('#key-form');
      const input = $('#key-input');
      const finish = (value) => {
        keyRequest = null;
        form.removeEventListener('submit', submit);
        dialog.removeEventListener('cancel', cancel);
        state.authCancelled = !value;
        resolve(value);
      };
      const cancel = () => finish('');
      const submit = (event) => {
        event.preventDefault();
        if (event.submitter?.value === 'cancel') { dialog.close(); finish(''); return; }
        const value = input.value.trim();
        if (!value) { input.focus(); return; }
        saveKey(value);
        dialog.close();
        finish(value);
      };
      form.addEventListener('submit', submit);
      dialog.addEventListener('cancel', cancel);
      input.value = '';
      dialog.showModal();
      input.focus();
    });
    return keyRequest;
  }

  const RETRYABLE_GET_STATUSES = new Set([408, 429, 500, 502, 503, 504]);
  const GET_RETRY_DELAYS = [700, 1400];
  const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
  async function api(path, options = {}, retry = true) {
    const method = String(options.method || 'GET').toUpperCase();
    const readOnly = method === 'GET' || method === 'HEAD';
    const headers = {
      ...(options.headers || {}),
      'Accept': 'application/json',
      'Cache-Control': 'no-cache',
    };
    const key = authKey();
    if (key) headers['X-Dashboard-Key'] = key;
    const attempts = readOnly ? GET_RETRY_DELAYS.length + 1 : 1;
    let response;
    let lastError;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const controller = new AbortController();
      const timer = window.setTimeout(() => controller.abort(), readOnly ? 30000 : 90000);
      try {
        response = await fetch(path, { ...options, cache: 'no-store', headers, signal: controller.signal });
      } catch (error) {
        // A network failure should release the UI immediately; the next
        // scheduled refresh will retry without leaving stale healthy state.
        lastError = error;
        if (method === 'POST') throw new Error('ئەنجامی داواکاری نادیارە؛ پێش دووبارەکردنەوە GitHub Actions بپشکنە.');
        throw error;
      } finally {
        window.clearTimeout(timer);
      }
      if (response.status === 401 && retry) {
        saveKey('');
        if (state.authCancelled) throw new Error('کلیلی داشبۆرد پێویستە.');
        const entered = await requestKey();
        if (entered) return api(path, options, false);
      }
      if (readOnly && RETRYABLE_GET_STATUSES.has(response.status) && attempt < attempts - 1) {
        await wait(GET_RETRY_DELAYS[attempt]);
        continue;
      }
      let data = {};
      try { data = await response.json(); } catch (_) { /* server error without JSON */ }
      if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
      return data;
    }
    throw lastError || new Error('داتا بەردەست نییە؛ تکایە دووبارە هەوڵ بدەرەوە.');
  }

  let blobUploadModule = null;
  async function uploadCustomVideo(file) {
    if (!file) return null;
    if (!['video/mp4', 'video/quicktime', 'video/webm'].includes(file.type)) {
      throw new Error('تەنها MP4، MOV یان WebM ڕێگەپێدراوە.');
    }
    if (file.size > 60 * 1024 * 1024) throw new Error('قەبارەی فایل نابێت لە 60MB زیاتر بێت.');
    blobUploadModule ||= await import('https://esm.sh/@vercel/blob@1.1.1/client?bundle');
    const safeName = file.name.replace(/[^a-zA-Z0-9._-]+/g, '-').slice(-120) || 'custom-video.mp4';
    return blobUploadModule.upload(
      'custom-videos/' + Date.now() + '-' + safeName,
      file,
      { access: 'public', handleUploadUrl: '/api/blob-upload', multipart: true,
        clientPayload: JSON.stringify({ purpose: 'quran-shorts-custom-video' }) }
    );
  }

  async function deleteUploadedBlob(url) {
    if (!url) return;
    try { await api('/api/blob-delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) }); }
    catch (_) { /* cleanup is retried by the runner when publishing succeeds */ }
  }

  function renderStats(data) {
    const schedule = data.schedule || {};
    const health = data.health || {};
    $('#today-count').textContent = schedule.today_count ?? '0';
    $('#today-target').textContent = `لە ${schedule.target ?? 3} ئامانج`;
    $('#today-progress').style.width = `${Math.min(100, ((schedule.today_count || 0) / Math.max(1, schedule.target || 3)) * 100)}%`;
    $('#tracked-count').textContent = health.tracked_videos ?? '0';
    $('#avg-views').textContent = Number(health.average_latest_views || 0).toLocaleString('en-US');
    const issues = health.issues || [];
    const labels = { uncertain_upload: 'upload ـێک نادیارە و پشکنینی دەوێت', flagged_videos: 'ڤیدیۆی ئاگادارکراو هەیە',
      youtube_not_verified: 'پەیوەندیی YouTube تازە پشتڕاست نەکراوەتەوە', workflow_failed: 'دوایین کار سەرکەوتوو نەبووە',
      workflow_unknown: 'دۆخی کارەکان نادیارە', automation_disabled: 'بڵاوکردنەوە ناچالاکە', invalid_schedule_data: 'تۆماری کات کێشەی هەیە' };
    $('#health-state').textContent = ({ attention: 'ئاگاداری', healthy: 'باشە', unknown: 'نادیارە' })[health.state] || 'نادیارە';
    const metricsDate = health.metrics_checked_at ? new Date(health.metrics_checked_at).toLocaleString('ku-IQ', { dateStyle: 'short', timeStyle: 'short' }) : '';
    $('#metrics-check').textContent = health.metrics_status === 'current' ? '✓' : health.metrics_status === 'unavailable' || health.metrics_status === 'stale' ? '!' : '○';
    $('#metrics-copy').textContent = health.metrics_status === 'current' ? `نوێکراوەتەوە: ${metricsDate}` : health.metrics_status === 'unavailable' ? `پشکنینی نوێ سەرکەوتوو نەبوو${metricsDate ? ` · دوا داتای سەرکەوتوو: ${metricsDate}` : ''}` : health.metrics_status === 'stale' ? `داتاکە کۆنە · دوا پشکنین: ${metricsDate}` : 'هێشتا پشکنینی YouTube نەکراوە';
    $('#health-detail').textContent = issues.map((issue) => labels[issue] || issue).join(' · ') || 'هیچ ئاگادارییەک نییە';
    $('#health-badge').textContent = health.state === 'healthy' ? '✓' : '!';
    const channel = data.channel || {};
    const connected = data.integrations?.youtube === true;
    const connectionText = connected ? '✓ تازە پشتڕاست کراوەتەوە' : channel.id ? '○ ڕێکخراوە؛ پەیوەندی نەپشکنراوە' : '○ ڕێک نەخراوە';
    $('#youtube-channel').textContent = channel.id ? `${channel.id} · ${channel.privacy || 'public'}` : 'کەناڵ ڕێک نەخراوە';
    $('#youtube-connection').textContent = connectionText;
    $('#settings-youtube').textContent = connectionText;
    $('#youtube-label').textContent = connected ? 'VERIFIED RECENTLY' : 'NOT VERIFIED';
    $('#live-status-text').textContent = channel.enabled ? 'بڵاوکردنەوە ڕێکخراوە' : 'بڵاوکردنەوە ناچالاکە';
    $('#ledger-check').textContent = health.uncertain_uploads?.length ? '!' : '✓';
    $('#ledger-copy').textContent = health.uncertain_uploads?.length ? `${health.uncertain_uploads.length} upload پشکنینی دەوێت` : 'تۆمار خوێندرایەوە';
    $('#workflow-check').textContent = data.workflow?.conclusion === 'success' ? '✓' : '○';
    $('#workflow-copy').textContent = data.workflow?.conclusion || data.workflow?.status || 'unknown';
    $('#uncertain-list').textContent = (health.uncertain_uploads || []).map((row) => row.id).join('، ');
    updateButtons();
  }

  function updateTikTokButtons() {
    const configured = state.tiktok?.configured === true;
    const connected = state.tiktok?.connected === true;
    const consent = $('#tiktok-consent')?.checked === true;
    $('#tiktok-connect').disabled = state.busy || !configured;
    $('#tiktok-draft').disabled = state.busy || !connected || !consent;
    if (!configured) {
      $('#tiktok-label').textContent = 'APP SETUP REQUIRED';
      $('#tiktok-connection').textContent = 'پێویستە TikTok app دروست بکرێت و کلیلی API لە Vercel زیاد بکرێت.';
      $('#tiktok-badge').textContent = '○ ئامادە نییە';
      $('#tiktok-status').textContent = 'TIKTOK_CLIENT_KEY، TIKTOK_CLIENT_SECRET و TIKTOK_REDIRECT_URI لە Vercel پێویستن.';
      $('#tiktok-settings').textContent = 'ڕێکخستنی ئەپی TikTok پێویستە';
    } else if (connected) {
      $('#tiktok-label').textContent = 'CONNECTED';
      $('#tiktok-connection').textContent = 'ئەکاونتی TikTok پەیوەستە. draft ـەکان بۆ Inbox دەنێردرێن.';
      $('#tiktok-badge').textContent = '✓ پەیوەستە';
      $('#tiktok-status').textContent = 'بۆ ناردن، ڕەزامەندییەکە هەڵبژێرە و دوگمەی draft دابگرە؛ publish لە TikTok خۆت دەکەیت.';
      $('#tiktok-settings').textContent = 'پەیوەستە؛ draft/inbox تەنها';
    } else {
      $('#tiktok-label').textContent = 'NOT CONNECTED';
      $('#tiktok-connection').textContent = 'ئەپەکە ڕێکخراوە؛ ئەکاونتی TikTok ـەکەت پەیوەست بکە.';
      $('#tiktok-badge').textContent = '○ پەیوەست نییە';
      $('#tiktok-status').textContent = 'دوگمەی پەیوەستکردن دابگرە و لە TikTok ڕێگەی video.upload پەسەند بکە.';
      $('#tiktok-settings').textContent = 'پەیوەندیی ئەکاونت پێویستە';
    }
  }

  async function refreshTikTokStatus() {
    if (state.authCancelled) {
      state.tiktok = { configured: false, connected: false };
      updateTikTokButtons();
      return;
    }
    try { state.tiktok = await api('/api/tiktok/status'); }
    catch (_) { state.tiktok = { configured: false, connected: false }; }
    updateTikTokButtons();
  }

  async function connectTikTok() {
    if (state.busy) return;
    try {
      const result = await api('/api/tiktok/connect');
      const target = new URL(result.authorization_url);
      if (target.protocol !== 'https:' || !['tiktok.com', 'www.tiktok.com'].includes(target.hostname)) {
        throw new Error('بەستەری OAuth ـی TikTok پشتڕاست نییە.');
      }
      window.location.assign(target.toString());
    } catch (error) { toast(`پەیوەستکردنی TikTok سەرکەوتوو نەبوو: ${error.message}`, true); }
  }

  function renderSchedule(data) {
    const slots = data.schedule?.slots || ['06:00', '12:00', '18:00'];
    const states = data.schedule?.slot_states || [];
    const rows = slots.map((slot, index) => {
      const done = states[index]?.completed === true;
      const next = states[index]?.due === true;
      return `<div class="schedule-row ${done ? 'done' : ''} ${next ? 'next' : ''}"><span class="slot-time">${escapeHtml(slot)}</span><span class="slot-line"></span><span class="slot-copy"><strong>${done ? 'بڵاوکراوەتەوە' : next ? 'داهاتوو' : 'چاوەڕوان'}</strong><small>${done ? 'ئەمڕۆ بە سەرکەوتوویی' : next ? 'کۆتا هەنگاوەکە ئامادەیە' : 'لە schedule ـدا'}</small></span><span class="slot-state">${done ? '✓' : next ? '●' : '○'}</span></div>`;
    }).join('');
    $('#schedule-list').innerHTML = rows || '<div class="empty">هیچ slot ـێک نییە.</div>';
    $('#schedule-editor-list').innerHTML = slots.map((slot, index) => `<label class="editor-slot"><input type="checkbox" checked disabled><strong>${escapeHtml(slot)}</strong><small>${states[index]?.completed ? 'تەواو کراوە' : 'خۆکارانە'}</small></label>`).join('');
  }

  function rowHtml(row) {
    const date = row.uploaded_at ? new Date(row.uploaded_at).toLocaleString('ku-IQ', { dateStyle: 'short', timeStyle: 'short' }) : '—';
    const privacy = { public: 'گشتی', unlisted: 'بە بەستەر', private: 'تایبەت', unknown: 'نەپشکنراوە' }[row.privacy_status] || escapeHtml(row.privacy_status || 'نەپشکنراوە');
    const flagLabels = { 'visibility problem': 'کێشەی بینراوی', 'no traction': 'بینەر نییە', 'video unavailable': 'ڤیدیۆ لە YouTube نەدۆزرایەوە', 'safety incident': 'کێشەی پاراستن' };
    const flags = Array.isArray(row.flags) ? row.flags.map((flag) => escapeHtml(flagLabels[flag] || flag)).join('، ') : '';
    const status = `<span class="status-pill">uploaded · ${privacy}</span>${flags ? `<small class="status-warning">${flags}</small>` : ''}`;
    return `<tr><td><div class="video-cell"><span class="video-thumb">▶</span><span>${escapeHtml(row.video_id)}</span></div></td><td>${escapeHtml(row.reciter)}</td><td>${escapeHtml(row.theme)}</td><td>${escapeHtml(date)}</td><td>${status}</td><td><a class="table-link" href="${escapeHtml(row.url)}" target="_blank" rel="noreferrer">بینین ↗</a></td></tr>`;
  }

  function renderAnalytics(data) {
    const analytics = data.analytics || {};
    const retention = analytics.retention || [];
    $('#retention-list').innerHTML = retention.length ? retention.map((row) =>
      `<div class="retention-row"><a class="retention-link" href="${escapeHtml(row.url || '#')}" target="_blank" rel="noreferrer">${escapeHtml(row.reciter)}</a><div class="retention-bar"><span style="width:${Math.min(100, row.percentage)}%"></span></div><strong>${row.percentage}%</strong><small>${row.views.toLocaleString('en-US')} بینین</small></div>`
    ).join('') : '<div class="empty">هێشتا داتای retention بەردەست نییە؛ scope ـی yt-analytics.readonly پێویستە.</div>';
    const cta = analytics.cta_variants || [];
    $('#cta-list').innerHTML = cta.length ? cta.map((row) =>
      `<div class="cta-row"><strong>جۆر ${row.variant}</strong><small>${row.videos} ڤیدیۆ · مامناوەندی ماوە ${row.avg_view_percentage}% · ${row.avg_views.toLocaleString('en-US')} بینین</small></div>`
    ).join('') : '<div class="empty">هێشتا داتای جۆرەکانی CTA کۆ نەبووەتەوە.</div>';
    const playlists = analytics.playlists || [];
    $('#playlist-list').innerHTML = playlists.length ? playlists.map((row) =>
      row.url ? `<a class="playlist-link" href="${escapeHtml(row.url)}" target="_blank" rel="noreferrer">${escapeHtml(row.surah)} ↗</a>` : `<span>${escapeHtml(row.surah)}</span>`
    ).join('') : '<div class="empty">هێشتا playlist ـێک دروست نەبووە.</div>';
  }

  function renderTables(data) {
    const rows = data.recent || [];
    const html = rows.length ? rows.map(rowHtml).join('') : '<tr><td colspan="6" class="empty">هێشتا هیچ پۆستێک نییە.</td></tr>';
    $('#recent-table').innerHTML = html;
    $('#library-table').innerHTML = html;
  }

  async function refresh() {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      const data = await api('/api/overview');
      state.overview = data;
      renderStats(data); renderSchedule(data); renderTables(data); renderAnalytics(data);
      $('#health-heading').textContent = ({ attention: 'پێویستی بە سەرنج هەیە', healthy: 'پشکنینەکان باشن', unknown: 'دۆخی سیستەم تەواو نەپشکنراوە' })[data.health?.state] || 'نادیارە';
      $('#health-copy').textContent = $('#health-detail').textContent;
      $('#health-ring').textContent = data.health?.state === 'healthy' ? '✓' : '?';
    } catch (error) {
      toast(`نەتوانرا داتا بار بکرێت: ${error.message}`, true);
      $('#health-heading').textContent = 'پەیوەندی پشکنینەوەی دەوێت';
      $('#health-copy').textContent = error.message;
      $('#health-state').textContent = 'نادیارە';
      $('#health-ring').textContent = '?';
      $('#metrics-check').textContent = '!';
      $('#metrics-copy').textContent = 'داتا نوێ نەکراوەتەوە';
      $('#youtube-connection').textContent = 'دۆخی پەیوەندی نادیارە';
      $('#live-status-text').textContent = 'داتا بەردەست نییە';
      state.overview = null;
      updateButtons();
    } finally { state.refreshing = false; }
    await refreshTikTokStatus();
  }

  function updateButtons() {
    const blocked = !state.overview?.channel?.enabled || state.overview?.health?.uncertain_uploads?.length > 0;
    $$('[data-action]').forEach((button) => { const mode = button.dataset.action; button.disabled = state.busy || !state.overview || (blocked && !['preview', 'report', 'tiktok_draft'].includes(mode)); });
    $('#manual-publish').disabled = state.busy || !state.overview || blocked;
    updateTikTokButtons();
  }

  async function runAction(mode, count = 1) {
    if (state.busy) return;
    state.busy = true;
    updateButtons();
    const names = { publish: 'پۆستکردن', preview: 'دروستکردنی preview', scheduled: 'گرتنەوەی schedule', report: 'پشکنینی ڕاستەقینەی YouTube', tiktok_draft: 'ناردنی draft بۆ TikTok' };
    toast(`${names[mode] || 'کردار'} دەستی پێکرد...`);
    try {
      const result = await api('/api/workflow', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode, count }) });
      toast(mode === 'report' ? 'داواکاریی پشکنین نێردرا؛ دوای تەواوبوونی GitHub Actions دۆخی ڤیدیۆکان نوێ دەبێتەوە.' : mode === 'tiktok_draft' ? 'داواکاری نێردرا؛ دوای سەرکەوتنی GitHub Actions ئاگاداری TikTok inbox بپشکنە. پۆستەکە خۆکارانە بڵاو نابێتەوە.' : `داواکاریی ${result.requested_count} پۆست نێردرا؛ ئەمە پشتڕاستکردنەوەی بڵاوکردنەوە نییە.`);
      window.setTimeout(refresh, 4500);
    } catch (error) { toast(`کردارەکە سەرکەوتوو نەبوو: ${error.message}`, true); }
    finally { state.busy = false; updateButtons(); }
  }

  async function submitClip() {
    if (state.busy) return;
    const file = $('#clip-file')?.files?.[0] || null;
    const url = $('#clip-url')?.value.trim() || '';
    const sourcePage = $('#clip-source')?.value.trim() || '';
    const theme = $('#clip-theme').value || '';
    const title = $('#clip-title').value.trim();
    const licenseConfirmed = $('#clip-license')?.checked === true;
    if ((!file && !url) || !theme || !title || !licenseConfirmed) {
      toast('فایل یان لینک، theme، ناونیشان و پشتڕاستکردنەوەی مۆڵەت پێویستن.', true);
      return;
    }
    state.busy = true;
    let uploadedBlob = null;
    try {
      if (file) { toast('فایلەکە بە شێوەی پارێزراو upload دەکرێت...'); uploadedBlob = await uploadCustomVideo(file); }
      const result = await api('/api/submit-clip', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: uploadedBlob?.url || url, blob_url: uploadedBlob?.url || '', blob_pathname: uploadedBlob?.pathname || '', source_page: sourcePage, theme, title, license_confirmed: licenseConfirmed }) });
      toast(result.note || 'ڤیدیۆکە نێردرا بۆ edit و publish.');
      if ($('#clip-url')) $('#clip-url').value = '';
      if ($('#clip-file')) $('#clip-file').value = '';
      if ($('#clip-source')) $('#clip-source').value = '';
      $('#clip-title').value = '';
      if ($('#clip-license')) $('#clip-license').checked = false;
    } catch (error) {
      if (uploadedBlob?.url) await deleteUploadedBlob(uploadedBlob.url);
      toast('نەتوانرا بینێردرێت: ' + error.message, true);
    } finally { state.busy = false; updateButtons(); }
  }

  function bind() {
    const themeSelect = $('#clip-theme');
    if (themeSelect) {
      themeSelect.innerHTML = ['forest_rain', 'mist_mountains', 'starry_night', 'ocean_moon', 'dawn_mosque']
        .map((theme) => `<option value="${theme}">${theme}</option>`).join('');
      $('#submit-clip').addEventListener('click', submitClip);
    }
    $$('.nav-item[data-section]').forEach((item) => item.addEventListener('click', () => setSection(item.dataset.section)));
    $$('[data-section-jump]').forEach((item) => item.addEventListener('click', () => setSection(item.dataset.sectionJump)));
    $('[data-open-menu]').addEventListener('click', () => $('#sidebar').classList.add('open'));
    $$('[data-close-menu]').forEach((item) => item.addEventListener('click', () => $('#sidebar').classList.remove('open')));
    $$('[data-action]').forEach((item) => item.addEventListener('click', () => runAction(item.dataset.action, Number(item.dataset.count || 1))));
    $$('[data-count-adjust]').forEach((button) => button.addEventListener('click', () => {
      state.manualCount = Math.max(1, Math.min(5, state.manualCount + Number(button.dataset.countAdjust)));
      $('#manual-count').textContent = state.manualCount;
    }));
    $('#manual-publish').addEventListener('click', () => runAction('publish', state.manualCount));
    $('#tiktok-connect').addEventListener('click', connectTikTok);
    $('#tiktok-consent').addEventListener('change', updateTikTokButtons);
    $('#tiktok-draft').addEventListener('click', () => {
      if ($('#tiktok-consent').checked && state.tiktok?.connected === true) runAction('tiktok_draft', 1);
    });
    $('#refresh-data').addEventListener('click', () => { state.authCancelled = false; refresh(); });
  }

  bind();
  const callbackResult = window.location?.search || '';
  if (callbackResult.includes('tiktok=')) {
    const result = new URLSearchParams(callbackResult).get('tiktok');
    toast(result === 'connected' ? 'TikTok پەیوەست کرا؛ دەتوانیت draft بنێریت.' : 'پەیوەستکردنی TikTok تەواو نەبوو؛ ڕێکخستن و ڕەزامەندی بپشکنە.', result !== 'connected');
    window.history?.replaceState({}, '', '/');
  }
  updateButtons();
  refresh();
  window.setInterval(() => { if (!state.authCancelled) refresh(); }, 30000);
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
})();
