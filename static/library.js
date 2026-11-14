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
      showLoose(s.loose_pdfs || []);
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

  function showLoose(list) {
    var box = document.getElementById('loose-warning');
    if (!list.length) { box.hidden = true; return; }
    box.replaceChildren(
      el('div', { text: 'These PDFs are ignored because they are not inside a subject\'s Lectures/ or Recitations/ folder:' }),
      el('ul', {}, list.map(function (p) { return el('li', {}, el('code', { text: p })); })),
      el('div', { text: 'Put slides into ' }, el('code', { text: '<Subject>/Lectures/' }), ' or ', el('code', { text: '<Subject>/Recitations/' }), '.')
    );
    box.hidden = false;
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

    refs.fc = el('div', { class: 'fc', hidden: true });

    var root = el('article', { class: 'card lecture', dataset: { id: l.id } },
      el('div', { class: 'head' }, refs.title, refs.badge),
      refs.meta,
      el('div', { class: 'progress-row' }, refs.bar, refs.count),
      refs.message,
      el('div', { class: 'actions' }, refs.open, refs.print, refs.prepare, el('span', { class: 'grow' }), refs.del),
      refs.fc,
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

  /* ---------- flashcards line ---------- */
  function fcBusy(l) {
    var f = l && l.flashcards;
    return !!(f && f.enabled && (f.status === 'queued' || f.status === 'generating'));
  }
  function fmtWhen(sec) {
    var d = new Date(sec * 1000);
    var t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return d.toDateString() === new Date().toDateString() ? t : d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + t;
  }
  function fmtDay(iso) {
    var d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  // Rebuilt only when the flashcards state changes, so polling never steals focus from its button.
  function renderFlashcards(l, r) {
    var f = l.flashcards;
    if (!f || !f.enabled) { r.fc.hidden = true; r.fcSig = ''; return; }
    var sig = JSON.stringify([l.id, l.subject, f.status, f.count, f.target, f.updated, f.error, f.resets_at]);
    r.fc.hidden = false;
    if (sig === r.fcSig) return;
    r.fcSig = sig;

    var st = f.status, count = f.count || 0;
    var kids = [count > 0
      ? el('a', { class: 'fc-title fc-link', href: '/lecture/' + ST.enc(l.id) + '/flashcards', text: 'Flashcards (' + count + ')' })
      : el('span', { class: 'fc-title', text: 'Flashcards' })];
    var state = el('span', { class: 'fc-state' });
    var action = null;   // {label, regenerate}

    if (st === 'waiting_notes') {
      state.textContent = 'Made automatically once the notes are done.';
    } else if (st === 'waiting_exams') {
      state.append('Waiting for past exams. Put PDFs into ', el('code', { text: (l.subject || '<Subject>') + '/Exams/' }), ' and they are picked up.');
      action = { label: 'Make without exams' };
    } else if (st === 'idle') {
      state.textContent = 'Not made automatically for this deck.';
      action = { label: 'Make flashcards' };
    } else if (st === 'queued' || st === 'generating') {
      state.append(el('span', { class: 'spinner', 'aria-hidden': 'true' }),
        st === 'queued' ? 'Queued…' : 'Writing flashcards… (1 to 3 minutes)');
      if (count > 0) state.append(' The current cards stay available.');
    } else if (st === 'ready') {
      if (f.updated) state.textContent = 'Updated ' + fmtDay(f.updated);
      action = { label: 'Regenerate flashcards', regenerate: true };
    } else if (st === 'paused') {
      state.textContent = 'Paused by the plan usage limit' + (f.resets_at ? '; resumes about ' + fmtWhen(f.resets_at) : '') + '.';
    } else if (st === 'error') {
      state.classList.add('is-error');
      state.textContent = 'Failed: ' + (f.error || 'unknown error') + (count > 0 ? ' (previous cards kept)' : '');
      action = { label: 'Retry' };
    }
    if (state.childNodes.length) kids.push(state);
    if (action) {
      var b = el('button', { class: 'btn btn-sm fc-btn', type: 'button', text: action.label });
      b.addEventListener('click', async function () {
        if (action.regenerate && !confirm('Regenerate the flashcards for "' + (l.title || l.id) + '"? The current cards are replaced.')) return;
        b.disabled = true;
        try {
          var upd = await ST.api('/api/lectures/' + ST.enc(l.id) + '/flashcards', { method: 'POST' });
          update(upd);
          schedulePoll(true);
        } catch (e) {
          r.message.textContent = 'Flashcards: ' + e.message;
          r.message.hidden = false;
          r.message.classList.add('is-error');
          b.disabled = false;
        }
      });
      kids.push(b);
    }
    r.fc.replaceChildren.apply(r.fc, kids);
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
    var srcBit = l.filename ? { t: '📄 ' + l.filename, title: l.source || '' } : null;
    var ut = ST.usageText(l.usage);
    var bits = [{ t: metaBits[0] }];
    if (srcBit) bits.push(srcBit);
    if (ut) bits.push({ t: ut });
    r.meta.replaceChildren.apply(r.meta, bits.map(function (b) { return el('span', { text: b.t, title: b.title || '' }); }));

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

    renderFlashcards(l, r);

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
    lastLayout = null;
    load();
  }
  function showEmpty() {
    if (!listEl.querySelector('.empty')) { lastLayout = null; listEl.replaceChildren(el('div', { class: 'empty', text: 'No lectures yet. Upload a PDF above to get started.' })); }
  }

  var subjects = [];      // [{name, lectures}] from /api/subjects
  var lastLayout = null;  // signature of the grouped DOM structure

  function natCmp(a, b) { return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' }); }

  /* ---------- subject filter: '' = all subjects; kept in the URL (#subject=…) and localStorage ---------- */
  var SUBJ_KEY = 'st.libSubject';
  var tabsEl = document.getElementById('subject-tabs');
  var current = readSelected();

  function readSelected() {
    var m = /^#subject=(.*)$/.exec(location.hash);
    if (m) { try { return decodeURIComponent(m[1]); } catch (e) { /* bad hash */ } }
    try { return localStorage.getItem(SUBJ_KEY) || ''; } catch (e) { return ''; }
  }
  function select(name) {
    current = name;
    try { localStorage.setItem(SUBJ_KEY, name); } catch (e) {}
    history.replaceState(null, '', name ? '#subject=' + encodeURIComponent(name) : location.pathname + location.search);
    if (name) document.getElementById('subject').value = name;
    lastLayout = null;
    render(lastList);
  }
  window.addEventListener('hashchange', function () { current = readSelected(); lastLayout = null; render(lastList); });

  function renderTabs(names, groups) {
    if (names.length < 2) { tabsEl.hidden = true; tabsEl.replaceChildren(); return; }
    var total = 0;
    function tab(value, label, count) {
      return el('button', { type: 'button', class: 'subject-tab', 'aria-pressed': String(value === current),
                            onclick: function () { select(value); } },
        el('span', { text: label }), el('span', { class: 'n', text: String(count) }));
    }
    var tabs = names.map(function (n) {
      var g = groups.get(n), c = g.lecture.length + g.recitation.length;
      total += c;
      return tab(n, n, c);
    });
    tabs.unshift(tab('', 'All subjects', total));
    tabsEl.replaceChildren.apply(tabsEl, tabs);
    tabsEl.hidden = false;
  }

  function render(lectures) {
    var seen = new Set();
    lectures.forEach(function (l) {
      seen.add(l.id);
      if (!cards.has(l.id)) buildCard(l);
      update(l);
    });
    Array.from(cards.keys()).forEach(function (id) { if (!seen.has(id)) { cards.get(id).root.remove(); cards.delete(id); } });

    var groups = new Map(); // subject -> {lecture: [], recitation: []}
    function grp(n) { if (!groups.has(n)) groups.set(n, { lecture: [], recitation: [] }); return groups.get(n); }
    subjects.forEach(function (s) { grp(s.name); });
    lectures.forEach(function (l) {
      grp(l.subject || '(no subject)')[l.kind === 'recitation' ? 'recitation' : 'lecture'].push(l);
    });
    function sortL(ls) {
      return ls.sort(function (a, b) {
        return natCmp(a.title || a.filename || a.id, b.title || b.filename || b.id) || natCmp(a.id, b.id);
      });
    }
    var names = Array.from(groups.keys()).sort(natCmp);
    if (current && names.indexOf(current) < 0) current = '';   // subject folder gone or renamed
    var shown = current ? [current] : names;
    var layout = current + '#' + names.map(function (n) {
      var g = groups.get(n);
      return n + ':' + sortL(g.lecture).map(function (l) { return l.id; }).join(',') +
        '/' + sortL(g.recitation).map(function (l) { return l.id; }).join(',');
    }).join('|');
    if (layout === lastLayout) return;
    lastLayout = layout;

    renderTabs(names, groups);
    if (!lectures.length && !subjects.length) { showEmpty(); return; }
    listEl.replaceChildren.apply(listEl, shown.map(function (n) {
      var g = groups.get(n);
      var kids = [el('h2', { class: 'subject-title', text: n })];
      [['lecture', 'Lectures'], ['recitation', 'Recitations']].forEach(function (k) {
        if (!g[k[0]].length) return;
        kids.push(el('h3', { class: 'kind-title', text: k[1] }));
        kids.push(el('div', { class: 'lectures' }, g[k[0]].map(function (l) { return cards.get(l.id).root; })));
      });
      if (kids.length === 1) kids.push(el('div', { class: 'subject-empty', text: 'No slides yet' }));
      return el('section', { class: 'subject-section', dataset: { subject: n } }, kids);
    }));
  }

  async function loadSubjects() {
    try {
      subjects = await ST.api('/api/subjects');
      var dl = document.getElementById('subject-list');
      dl.replaceChildren.apply(dl, subjects.map(function (s) { return el('option', { value: s.name }); }));
    } catch (e) { /* older backend: fall back to subjects derived from lectures */ }
  }

  /* ---------- polling ---------- */
  var lastList = [];
  var subjectsLoaded = false;
  async function load() {
    if (inFlight) return;
    inFlight = true;
    try {
      lastList = await ST.api('/api/lectures');
      if (!subjectsLoaded || lastList.length !== cards.size) { subjectsLoaded = true; await loadSubjects(); }
      render(lastList);
    } catch (e) {
      if (!cards.size) listEl.replaceChildren(el('div', { class: 'empty err', text: 'Could not load lectures: ' + e.message }));
    } finally { inFlight = false; }
    if (Date.now() - lastStatusFetch > 20000) loadStatus();
    schedulePoll();
  }
  function schedulePoll(force) {
    clearTimeout(pollTimer);
    var busy = lastList.some(function (l) { return ST.isBusy(l.status) || fcBusy(l); });
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
    var subject = document.getElementById('subject').value.trim();
    if (!subject) { msg.classList.add('err'); msg.textContent = 'Enter a subject.'; return; }
    var fd = new FormData();
    var kind = (form.querySelector('input[name=kind]:checked') || {}).value || 'lecture';
    fd.append('subject', subject);
    fd.append('kind', kind);
    fd.append('file', file);
    Array.prototype.forEach.call(document.getElementById('context').files, function (f) { fd.append('context', f); });
    fd.append('autostart', document.getElementById('autostart').checked ? 'true' : 'false');
    btn.disabled = true;
    msg.classList.remove('err');
    msg.textContent = 'Uploading…';
    try {
      await ST.api('/api/lectures', { method: 'POST', body: fd });
      try { localStorage.setItem('st.lastSubject', subject); localStorage.setItem('st.lastKind', kind); } catch (e2) {}
      form.reset();
      if (current && current !== subject) select(subject);   // show the subject the deck went into
      document.getElementById('subject').value = subject;
      var kr = form.querySelector('input[name=kind][value=' + kind + ']'); if (kr) kr.checked = true;
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

  try {
    var ls = current || localStorage.getItem('st.lastSubject');
    if (ls) document.getElementById('subject').value = ls;
  } catch (e) {}
  try {
    var lk = localStorage.getItem('st.lastKind');
    var kr0 = lk && form.querySelector('input[name=kind][value=' + lk + ']');
    if (kr0) kr0.checked = true;
  } catch (e) {}
  loadStatus();
  load();
})();
