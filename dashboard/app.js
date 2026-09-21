(() => {
  'use strict';

  const state = { overview: null, manualCount: 1 };
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

  function authKey() { return sessionStorage.getItem('quran_dashboard_key') || ''; }

  let keyRequest = null;
  function requestKey() {
    if (keyRequest) return keyRequest;
    keyRequest = new Promise((resolve) => {
      const dialog = $('#key-dialog');
      const form = $('#key-form');
      const input = $('#key-input');
      const finish = (value) => { keyRequest = null; form.removeEventListener('submit', submit); resolve(value); };
      const submit = (event) => {
        event.preventDefault();
        const value = input.value.trim();
        if (value) sessionStorage.setItem('quran_dashboard_key', value);
        dialog.close();
        finish(value);
      };
      form.addEventListener('submit', submit);
      dialog.addEventListener('cancel', () => finish(''), { once: true });
      input.value = '';
      dialog.showModal();
      input.focus();
    });
    return keyRequest;
  }

  async function api(path, options = {}, retry = true) {
    const headers = { ...(options.headers || {}), 'Accept': 'application/json' };
    const key = authKey();
    if (key) headers['X-Dashboard-Key'] = key;
    const response = await fetch(path, { ...options, headers });
    if (response.status === 401 && retry) {
      const entered = await requestKey();
      if (entered) return api(path, options, false);
    }
    let data = {};
    try { data = await response.json(); } catch (_) { /* server error without JSON */ }
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }

  function renderStats(data) {
    const schedule = data.schedule || {};
    const health = data.health || {};
    $('#today-count').textContent = schedule.today_count ?? '0';
    $('#today-target').textContent = `لە ${schedule.target ?? 3} ئامانج`;
    $('#today-progress').style.width = `${Math.min(100, ((schedule.today_count || 0) / Math.max(1, schedule.target || 3)) * 100)}%`;
    $('#tracked-count').textContent = health.tracked_videos ?? '0';
    $('#avg-views').textContent = Number(health.average_latest_views || 0).toLocaleString('en-US');
    $('#health-state').textContent = health.state === 'attention' ? 'ئاگاداری' : 'باشە';
    $('#health-detail').textContent = health.flagged_videos ? `${health.flagged_videos} ڤیدیۆ پێویستی بە سەرنجە` : 'هیچ ئاگادارییەک نییە';
    $('#health-badge').textContent = health.flagged_videos ? '!' : '●';
    const channel = data.channel || {};
    $('#youtube-channel').textContent = channel.id ? `${channel.id} · ${channel.privacy || 'public'}` : 'کەناڵ ڕێک نەخراوە';
    $('#youtube-connection').textContent = channel.id ? '● پەیوەستە' : '○ ڕێک نەخراوە';
    $('#settings-youtube').textContent = channel.id ? `پەیوەستە · ${channel.privacy || 'public'}` : 'پەیوەست نییە';
  }

  function renderSchedule(data) {
    const slots = data.schedule?.slots || ['11:00', '16:00', '20:00'];
    const todayCount = data.schedule?.today_count || 0;
    const rows = slots.map((slot, index) => {
      const done = index < todayCount;
      const next = !done && index === todayCount;
      return `<div class="schedule-row ${done ? 'done' : ''} ${next ? 'next' : ''}"><span class="slot-time">${escapeHtml(slot)}</span><span class="slot-line"></span><span class="slot-copy"><strong>${done ? 'بڵاوکراوەتەوە' : next ? 'داهاتوو' : 'چاوەڕوان'}</strong><small>${done ? 'ئەمڕۆ بە سەرکەوتوویی' : next ? 'کۆتا هەنگاوەکە ئامادەیە' : 'لە schedule ـدا'}</small></span><span class="slot-state">${done ? '✓' : next ? '●' : '○'}</span></div>`;
    }).join('');
    $('#schedule-list').innerHTML = rows || '<div class="empty">هیچ slot ـێک نییە.</div>';
    $('#schedule-editor-list').innerHTML = slots.map((slot, index) => `<label class="editor-slot"><input type="checkbox" checked disabled><strong>${escapeHtml(slot)}</strong><small>${index < todayCount ? 'تەواو کراوە' : 'خۆکارانە'}</small></label>`).join('');
  }

  function rowHtml(row) {
    const date = row.uploaded_at ? new Date(row.uploaded_at).toLocaleString('ku-IQ', { dateStyle: 'short', timeStyle: 'short' }) : '—';
    return `<tr><td><div class="video-cell"><span class="video-thumb">▶</span><span>${escapeHtml(row.video_id)}</span></div></td><td>${escapeHtml(row.reciter)}</td><td>${escapeHtml(row.theme)}</td><td>${escapeHtml(date)}</td><td><span class="status-pill">uploaded</span></td><td><a class="table-link" href="${escapeHtml(row.url)}" target="_blank" rel="noreferrer">بینین ↗</a></td></tr>`;
  }

  function renderTables(data) {
    const rows = data.recent || [];
    const html = rows.length ? rows.map(rowHtml).join('') : '<tr><td colspan="6" class="empty">هێشتا هیچ پۆستێک نییە.</td></tr>';
    $('#recent-table').innerHTML = html;
    $('#library-table').innerHTML = html;
  }

  async function refresh() {
    try {
      const data = await api('/api/overview');
      state.overview = data;
      renderStats(data); renderSchedule(data); renderTables(data);
      $('#metrics-check').textContent = '✓';
      $('#metrics-copy').textContent = 'کۆتا داتا بەردەستە';
      $('#health-heading').textContent = data.health?.state === 'attention' ? 'پێویستی بە سەرنج هەیە' : 'سیستەم تەندروستە';
      $('#health-copy').textContent = `${data.health?.tracked_videos || 0} ڤیدیۆ چاودێری دەکرێن و duplicate protection چالاکە.`;
      $('#health-ring').textContent = data.health?.state === 'attention' ? '!' : '✓';
    } catch (error) {
      toast(`نەتوانرا داتا بار بکرێت: ${error.message}`, true);
      $('#health-heading').textContent = 'پەیوەندی پشکنینەوەی دەوێت';
      $('#health-copy').textContent = error.message;
    }
  }

  async function runAction(mode, count = 1) {
    const names = { publish: 'پۆستکردن', preview: 'دروستکردنی preview', scheduled: 'گرتنەوەی schedule' };
    toast(`${names[mode] || 'کردار'} دەستی پێکرد...`);
    try {
      const result = await api('/api/workflow', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode, count }) });
      toast(`${result.dispatched} workflow dispatch کرا. دۆخەکە لە GitHub Actions ـدا ببینە.`);
      window.setTimeout(refresh, 4500);
    } catch (error) { toast(`کردارەکە سەرکەوتوو نەبوو: ${error.message}`, true); }
  }

  function bind() {
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
  }

  bind();
  refresh();
  window.setInterval(refresh, 30000);
})();

