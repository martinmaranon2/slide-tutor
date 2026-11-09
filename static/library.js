/* Library page: upload form + lecture cards with polling. */
(function () {
  'use strict';
  var ST = window.ST, el = ST.el;

  var listEl = document.getElementById('lectures');
  var planEl = document.getElementById('plan-usage');
  var cards = new Map(); // id -> {root, refs}
  var pollTimer = null;
  var lastStatusFetch = 0;
  var inFlight = false;

  /* ---------- plan usage ---------- */
  async function loadStatus() {
    lastStatusFetch = Date.now();
    try {
      var s = await ST.api('/api/status');
      if (s.lectures_dir) document.getElementById('lectures-dir').textContent = s.lectures_dir;
      var info = ST.rateLimitInfo(s.rate_limit);
      if (info) {
        planEl.textContent = info.text;
        planEl.classList.toggle('warn', info.warn);
        planEl.hidden = false;
      } else {
        planEl.hidden = true;
      }
    } catch (e) { planEl.hidden = true; }
  }

  /* ---------- cards ---------- */
  function progressPct(l) {
    var p = l.prep || {};
    return p.total ? Math.min(100, Math.round(100 * (p.done || 0) / p.total)) : 0;
  }

  function buildCard(l) {
    var refs = {};
    refs.title = el('h3');
    refs.badge = el('span', { class: 'badge' });
    refs.meta = el('div', { class: 'meta' });
    refs.bar = el('div', { class: 'progress' }, el('i'));
    refs.count = el('span', { class: 'count' });
    refs.message = el('div', { class: 'message' });
    refs.open = el('a', { class: 'btn btn-primary', text: 'Open' });
    refs.print = el('a', { class: 'btn', text: 'Print', target: '_blank' });
    refs.prepare = el('button', { class: 'btn', type: 'button', text: 'Prepare lecture' });
    refs.del = el('button', { class: 'btn btn-danger', type: 'button', text: 'Delete' });
    refs.ctxList = el('ul');
    refs.ctxInput = el('input', { type: 'file', accept: 'application/pdf,.pdf', multiple: true, 'aria-label': 'Add context files' });
    refs.ctxMsg = el('span', { class: 'small err' });

    var root = el('article', { class: 'card lecture', dataset: { id: l.id } },
      el('div', { class: 'head' }, refs.title, refs.badge),
      refs.meta,
      el('div', { class: 'progress-row' }, refs.bar, refs.count),
      refs.message,
      el('div', { class: 'actions' }, refs.open, refs.print, refs.prepare, el('span', { class: 'grow' }), refs.del),
      el('div', { class: 'ctx' },
        el('div', { class: 'ctx-title', text: 'Context files' }),
        refs.ctxList,
        el('div', {}, refs.ctxInput, ' ', refs.ctxMsg)
      )
    );

    refs.prepare.addEventListener('click', async function () {
      refs.prepare.disabled = true;
      try {
        var upd = await ST.api('/api/lectures/' + ST.enc(l.id) + '/prepare', { method: 'POST' });
        update(upd);
        schedulePoll(true);
      } catch (e) {
        refs.message.textContent = 'Could not start: ' + e.message;
        refs.message.classList.add('is-error');
        refs.prepare.disabled = false;
      }
    });
    refs.del.addEventListener('click', async function () {
      if (!confirm('Delete "' + (refs.title.textContent || l.id) + '" and all its notes and chats?')) return;
      refs.del.disabled = true;
      try {
        await ST.api('/api/lectures/' + ST.enc(l.id), { method: 'DELETE' });
        removeCard(l.id);
      } catch (e) {
        refs.message.textContent = 'Delete failed: ' + e.message;
        refs.message.classList.add('is-error');
        refs.del.disabled = false;
      }
    });
    refs.ctxInput.addEventListener('change', async function () {
      var files = Array.prototype.slice.call(refs.ctxInput.files);
      if (!files.length) return;
      var fd = new FormData();
      files.forEach(function (f) { fd.append('context', f); });
      refs.ctxMsg.textContent = 'Uploading…';
      try {
        var upd = await ST.api('/api/lectures/' + ST.enc(l.id) + '/context', { method: 'POST', body: fd });
        refs.ctxMsg.textContent = '';
        update(upd);
      } catch (e) {
        refs.ctxMsg.textContent = e.message;
      }
      refs.ctxInput.value = '';
    });

    cards.set(l.id, { root: root, refs: refs });
    return root;
  }

  function update(l) {
    var c = cards.get(l.id);
    if (!c) return;
    var r = c.refs, p = l.prep || {};
    var status = l.status;
    r.title.textContent = l.title || l.filename || l.id;
    r.badge.textContent = ST.STATUS_LABEL[status] || status;
    r.badge.className = 'badge ' + status;

    var metaBits = [];
    metaBits.push((l.n_slides || 0) + ' slide' + (l.n_slides === 1 ? '' : 's'));
    if (l.folder) metaBits.push('📁 ' + l.folder.split('/').slice(-1)[0] + '/' + (l.filename || ''));
    else if (l.filename) metaBits.push(l.filename);
    var ut = ST.usageText(l.usage);
    if (ut) metaBits.push(ut);
    r.meta.replaceChildren.apply(r.meta, metaBits.map(function (t) { return el('span', { text: t }); }));

    var showBar = p.total > 0 || status === 'processing';
    r.bar.parentNode.hidden = !showBar;
    r.bar.firstChild.style.width = (status === 'processing' && !p.total ? 0 : progressPct(l)) + '%';
    r.bar.classList.toggle('is-paused', status === 'paused');
    r.bar.classList.toggle('is-done', status === 'ready');
    r.count.textContent = p.total ? (p.done || 0) + '/' + p.total : '';

    var msg = p.message || '';
    if (!msg && status === 'incomplete') {
      var nErr = Object.keys(p.errors || {}).length;
      msg = nErr + ' slide' + (nErr === 1 ? '' : 's') + ' failed. Run "Prepare lecture" to retry.';
    }
    r.message.textContent = msg;
    r.message.hidden = !msg;
    r.message.classList.toggle('is-error', status === 'error' || status === 'incomplete');

    var href = '/lecture/' + ST.enc(l.id);
    r.open.setAttribute('href', href);
    r.print.setAttribute('href', href + '/print');
    r.open.setAttribute('aria-disabled', status === 'processing' ? 'true' : 'false');
    var canPrepare = status === 'ready_to_prepare' || status === 'incomplete';
    r.prepare.hidden = !canPrepare;
    r.prepare.disabled = false;
    r.prepare.textContent = status === 'incomplete' ? 'Retry failed slides' : 'Prepare lecture';

    // context files
    var ctx = l.context_files || [];
    var items = ctx.map(function (f) {
      var x = el('button', { class: 'x', type: 'button', title: 'Remove', 'aria-label': 'Remove ' + f.name, text: '✕' });
      x.addEventListener('click', async function () {
        x.disabled = true;
        try {
          var upd = await ST.api('/api/lectures/' + ST.enc(l.id) + '/context/' + ST.enc(f.name), { method: 'DELETE' });
          update(upd);
        } catch (e) { r.ctxMsg.textContent = e.message; x.disabled = false; }
      });
      return el('li', {}, el('span', { text: f.name }),
        el('span', { class: 'muted', text: f.chars ? '(' + ST.fmtNum(f.chars) + ' chars)' : '' }), x);
    });
    if (!items.length) items = [el('li', { class: 'muted', text: 'None' })];
    r.ctxList.replaceChildren.apply(r.ctxList, items);
  }

  function removeCard(id) {
    var c = cards.get(id);
    if (c) { c.root.remove(); cards.delete(id); }
    if (!cards.size) showEmpty();
  }
  function showEmpty() {
    if (!listEl.querySelector('.empty')) listEl.replaceChildren(el('div', { class: 'empty', text: 'No lectures yet. Upload a PDF above to get started.' }));
  }

  function render(lectures) {
    var seen = new Set();
    var prev = null;
    var empty = listEl.querySelector('.empty');
    if (lectures.length && empty) empty.remove();
    lectures.forEach(function (l) {
      seen.add(l.id);
      if (!cards.has(l.id)) buildCard(l);
      var c = cards.get(l.id);
      update(l);
      // keep DOM order = API order (newest first) without recreating nodes
      var expectedNext = prev ? prev.nextSibling : listEl.firstChild;
      if (c.root !== expectedNext) listEl.insertBefore(c.root, expectedNext);
      prev = c.root;
    });
    Array.from(cards.keys()).forEach(function (id) { if (!seen.has(id)) removeCard(id); });
    if (!lectures.length) showEmpty();
  }

  /* ---------- polling ---------- */
  var lastList = [];
  async function load() {
    if (inFlight) return;
    inFlight = true;
    try {
      lastList = await ST.api('/api/lectures');
      render(lastList);
    } catch (e) {
      if (!cards.size) listEl.replaceChildren(el('div', { class: 'empty err', text: 'Could not load lectures: ' + e.message }));
    } finally { inFlight = false; }
    if (Date.now() - lastStatusFetch > 20000) loadStatus();
    schedulePoll();
  }
  function schedulePoll(force) {
    clearTimeout(pollTimer);
    var busy = lastList.some(function (l) { return ST.isBusy(l.status); });
    if (busy || force) {
      pollTimer = setTimeout(function () { loadStatus(); load(); }, 2000);
    }
  }

  /* ---------- upload ---------- */
  var form = document.getElementById('upload-form');
  var msg = document.getElementById('form-msg');
  var btn = document.getElementById('upload-btn');
  form.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    var file = document.getElementById('file').files[0];
    if (!file) { msg.textContent = 'Choose a PDF first.'; return; }
    var fd = new FormData();
    fd.append('file', file);
    Array.prototype.forEach.call(document.getElementById('context').files, function (f) { fd.append('context', f); });
    fd.append('autostart', document.getElementById('autostart').checked ? 'true' : 'false');
    btn.disabled = true;
    msg.classList.remove('err');
    msg.textContent = 'Uploading…';
    try {
      await ST.api('/api/lectures', { method: 'POST', body: fd });
      form.reset();
      document.getElementById('autostart').checked = true;
      msg.textContent = 'Uploaded.';
      lastList = [{ status: 'processing' }];
      await load();
      schedulePoll(true);
      setTimeout(function () { if (msg.textContent === 'Uploaded.') msg.textContent = ''; }, 3000);
    } catch (e) {
      msg.classList.add('err');
      msg.textContent = 'Upload failed: ' + e.message;
    } finally { btn.disabled = false; }
  });

  loadStatus();
  load();
})();
