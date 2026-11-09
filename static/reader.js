/* Reader page: slide viewer, notes, per-slide chat with streaming, region questions. */
(function () {
  'use strict';
  var ST = window.ST, el = ST.el;
  var $ = function (id) { return document.getElementById(id); };

  var id = ST.lectureIdFromPath();
  var API = '/api/lectures/' + ST.enc(id || '');

  var QUICK = {
    simpler: "Explain this slide more simply, as if I'm seeing it for the first time. Use one everyday analogy, then map it back to the slide's notation.",
    exam: "Give me one exam-style question on this slide, like a real exam would ask. Don't give the solution yet. I'll answer and you check it.",
  };

  var state = {
    lecture: null,   // LectureSummary
    slides: [],      // [{n,title,has_notes}]
    total: 0,
    n: 0,
    tab: 'notes',
    region: null,    // {rect:[x0,y0,x1,y1], crop:dataURL}
    aspect: 16 / 9,
    lsKey: 'slide-tutor:last:' + id,
  };

  /* ---------- elements ---------- */
  var stage = $('stage'), wrap = $('slide-wrap'), img = $('slide-img'), layer = $('sel-layer'), stageMsg = $('stage-msg');
  var thumbs = $('thumbs'), jump = $('jump');
  var qInput = $('q'), sendBtn = $('send'), messagesEl = $('messages');

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function slideTitle(n) {
    var s = state.slides[n - 1];
    return s && s.title ? s.title : '';
  }

  /* =====================================================================
   * Header: lecture info + prep progress
   * ===================================================================== */
  var progTimer = null;
  var lastDone = -1;

  function renderHeader() {
    var l = state.lecture;
    if (!l) return;
    var title = l.title || l.filename || id;
    var t = $('title');
    t.textContent = '';
    if (l.subject) {
      t.appendChild(el('span', { class: 'subject', text: l.subject }));
      t.appendChild(el('span', { class: 'sep', text: ' › ' }));
    }
    t.appendChild(document.createTextNode(title));
    t.title = (l.subject ? l.subject + ' › ' : '') + title;
    document.title = (l.subject ? l.subject + ' › ' : '') + title + ' · Slide Tutor';
    $('print-link').setAttribute('href', '/lecture/' + ST.enc(id) + '/print');

    var p = l.prep || {};
    var prep = $('prep');
    var show = p.total > 0 || l.status === 'processing' || l.status === 'error';
    prep.hidden = !show;
    if (show) {
      var pct = p.total ? Math.round(100 * (p.done || 0) / p.total) : 0;
      var bar = $('prep-bar');
      bar.firstChild.style.width = pct + '%';
      bar.classList.toggle('is-paused', l.status === 'paused');
      bar.classList.toggle('is-done', l.status === 'ready');
      $('prep-count').textContent = p.total ? (p.done || 0) + '/' + p.total : '';
      var m = p.message || '';
      if (!m) {
        if (l.status === 'ready') m = 'Notes ready';
        else if (l.status === 'incomplete') m = Object.keys(p.errors || {}).length + ' failed';
        else m = ST.STATUS_LABEL[l.status] || l.status;
      }
      var mEl = $('prep-msg');
      mEl.textContent = m;
      mEl.classList.toggle('err', l.status === 'error');
    }
    $('prepare-btn').hidden = !(l.status === 'ready_to_prepare' || l.status === 'incomplete');
    $('prepare-btn').textContent = l.status === 'incomplete' ? 'Retry failed slides' : 'Prepare lecture';

    var ut = ST.usageText(l.usage);
    $('usage').textContent = ut;
    $('usage').title = ut ? 'Token usage for this lecture (input includes cache reads)' : '';
  }

  async function loadLecture() {
    var d = await ST.api(API);
    state.lecture = d;
    state.slides = d.slides || [];
    state.total = state.slides.length || d.n_slides || 0;
    $('total').textContent = state.total;
    jump.max = state.total || '';
    lastDone = (d.prep || {}).done;
    renderHeader();
    return d;
  }

  function reflectSlideNotes() {
    // refresh the little "no notes yet" dots on thumbnails
    state.slides.forEach(function (s, i) {
      var t = thumbs.children[i];
      if (!t) return;
      var err = state.lecture && state.lecture.prep && state.lecture.prep.errors && state.lecture.prep.errors[String(s.n)];
      t.classList.toggle('nonotes', !s.has_notes && !err);
      t.classList.toggle('errnotes', !!err && !s.has_notes);
    });
  }

  async function pollProgress() {
    clearTimeout(progTimer);
    var prevStatus = state.lecture && state.lecture.status;
    try {
      var p = await ST.api(API + '/progress');
      var l = state.lecture;
      l.status = p.status; l.prep = p.prep; l.usage = p.usage;
      renderHeader();
      var changedDone = (p.prep || {}).done !== lastDone;
      lastDone = (p.prep || {}).done;
      if (prevStatus === 'processing' && p.status !== 'processing') {
        await loadLecture();
        await initSlides();
      } else if (changedDone || (prevStatus !== p.status)) {
        var d = await ST.api(API);
        state.slides = d.slides || state.slides;
        l.n_slides = d.n_slides;
        reflectSlideNotes();
        renderHeader();
      }
    } catch (e) { /* transient: try again */ }
    if (state.lecture && ST.isBusy(state.lecture.status)) {
      progTimer = setTimeout(pollProgress, 2000);
    }
  }

  $('prepare-btn').addEventListener('click', async function () {
    var b = $('prepare-btn');
    b.disabled = true;
    try {
      var upd = await ST.api(API + '/prepare', { method: 'POST' });
      Object.assign(state.lecture, upd);
      renderHeader();
      pollProgress();
      loadNotes(state.n);
    } catch (e) {
      $('prep').hidden = false;
      $('prep-msg').textContent = 'Could not start: ' + e.message;
      $('prep-msg').classList.add('err');
    } finally { b.disabled = false; }
  });

  /* =====================================================================
   * Slide stage + navigation
   * ===================================================================== */
  function fitStage() {
    var r = stage.getBoundingClientRect();
    if (!r.width || !r.height) return;
    var w = r.width, h = w / state.aspect;
    if (h > r.height) { h = r.height; w = h * state.aspect; }
    wrap.style.width = Math.floor(w) + 'px';
    wrap.style.height = Math.floor(h) + 'px';
  }
  if (window.ResizeObserver) new ResizeObserver(fitStage).observe(stage);
  window.addEventListener('resize', fitStage);

  img.addEventListener('dragstart', function (e) { e.preventDefault(); });
  wrap.addEventListener('dragstart', function (e) { e.preventDefault(); });

  function imageUrl(n, size) { return API + '/slides/' + n + '/image?size=' + size; }

  function showSlideImage(n) {
    var url = imageUrl(n, 'lg');
    var pre = new Image();
    pre.onload = function () {
      if (state.n !== n) return;
      if (pre.naturalWidth && pre.naturalHeight) {
        var a = pre.naturalWidth / pre.naturalHeight;
        if (Math.abs(a - state.aspect) > 0.001) {
          state.aspect = a;
          thumbs.style.setProperty('--aspect', a);
        }
      }
      img.src = url;
      img.alt = 'Slide ' + n + (slideTitle(n) ? ': ' + slideTitle(n) : '');
      stageMsg.hidden = true;
      wrap.hidden = false;
      fitStage();
    };
    pre.onerror = function () {
      if (state.n !== n) return;
      wrap.hidden = true;
      stageMsg.hidden = false;
      stageMsg.textContent = 'Could not load slide ' + n + '.';
    };
    pre.src = url;
    // warm the cache for neighbours
    [n - 1, n + 1].forEach(function (k) {
      if (k >= 1 && k <= state.total) new Image().src = imageUrl(k, 'lg');
    });
  }

  function buildThumbs() {
    thumbs.replaceChildren();
    var frag = document.createDocumentFragment();
    for (var i = 1; i <= state.total; i++) {
      (function (n) {
        var b = el('button', { class: 'thumb', type: 'button', title: n + (slideTitle(n) ? ' · ' + slideTitle(n) : ''), 'aria-label': 'Slide ' + n },
          el('img', { src: imageUrl(n, 'sm'), loading: 'lazy', alt: '', draggable: 'false' }),
          el('span', { class: 'num', text: String(n) }),
          el('span', { class: 'dot' }));
        b.addEventListener('click', function () { go(n); });
        frag.appendChild(b);
      })(i);
    }
    thumbs.appendChild(frag);
    reflectSlideNotes();
  }

  function updateNav() {
    $('prev').disabled = state.n <= 1;
    $('next').disabled = state.n >= state.total;
    if (document.activeElement !== jump) jump.value = state.n;
    Array.prototype.forEach.call(thumbs.children, function (t, i) {
      var cur = i + 1 === state.n;
      t.classList.toggle('current', cur);
      if (cur) {
        t.setAttribute('aria-current', 'true');
        var left = t.offsetLeft - (thumbs.clientWidth - t.offsetWidth) / 2;
        thumbs.scrollTo({ left: Math.max(0, left), behavior: 'smooth' });
      } else t.removeAttribute('aria-current');
    });
  }

  function go(n, opts) {
    n = clamp(parseInt(n, 10) || 1, 1, Math.max(1, state.total));
    if (n === state.n && !(opts && opts.force)) { updateNav(); return; }
    state.n = n;
    clearRegion();
    showSlideImage(n);
    updateNav();
    ST.lsSet(state.lsKey, String(n));
    try { history.replaceState(null, '', '#' + n); } catch (e) { /* ignore */ }
    loadNotes(n, { reset: true });
    renderThread({ switched: true });
    loadThread(n);
    updateAskHead();
  }

  $('prev').addEventListener('click', function () { go(state.n - 1); });
  $('next').addEventListener('click', function () { go(state.n + 1); });
  jump.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { go(jump.value); jump.blur(); }
  });
  jump.addEventListener('change', function () { go(jump.value); });
  jump.addEventListener('blur', function () { jump.value = state.n; });
  window.addEventListener('hashchange', function () {
    var h = parseInt(location.hash.slice(1), 10);
    if (h && h !== state.n && state.total) go(h);
  });

  function isTyping(t) {
    return !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
  }
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (isTyping(e.target)) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); go(state.n - 1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); go(state.n + 1); }
    else if (e.key === '/') { e.preventDefault(); setTab('ask'); qInput.focus(); }
  });

  /* =====================================================================
   * Region selection on the slide
   * ===================================================================== */
  var drag = null;
  var keptRect = null;

  function rectEl() { return el('div', { class: 'sel-rect' }); }
  function placeRect(node, r) {
    node.style.left = (r[0] * 100) + '%';
    node.style.top = (r[1] * 100) + '%';
    node.style.width = ((r[2] - r[0]) * 100) + '%';
    node.style.height = ((r[3] - r[1]) * 100) + '%';
  }
  function pt(e) {
    var b = layer.getBoundingClientRect();
    return [clamp((e.clientX - b.left) / b.width, 0, 1), clamp((e.clientY - b.top) / b.height, 0, 1)];
  }
  function normRect(a, b) {
    return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
  }

  layer.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    e.preventDefault();
    var p = pt(e);
    drag = { start: p, node: rectEl(), pointer: e.pointerId };
    if (keptRect) keptRect.hidden = true;
    layer.appendChild(drag.node);
    placeRect(drag.node, [p[0], p[1], p[0], p[1]]);
    try { layer.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
  });
  layer.addEventListener('pointermove', function (e) {
    if (!drag) return;
    placeRect(drag.node, normRect(drag.start, pt(e)));
  });
  function endDrag(e, cancelled) {
    if (!drag) return;
    var r = normRect(drag.start, pt(e));
    var node = drag.node;
    drag = null;
    var area = (r[2] - r[0]) * (r[3] - r[1]);
    if (!cancelled && area > 0.01) {
      node.remove();
      setRegion(r);
    } else {
      node.remove();
      if (keptRect) keptRect.hidden = false;
    }
  }
  layer.addEventListener('pointerup', function (e) { endDrag(e, false); });
  layer.addEventListener('pointercancel', function (e) { endDrag(e, true); });

  function setRegion(r) {
    clearRegion();
    var canvas = $('region-canvas');
    var iw = img.naturalWidth, ih = img.naturalHeight;
    if (iw && ih) {
      var sx = r[0] * iw, sy = r[1] * ih, sw = (r[2] - r[0]) * iw, sh = (r[3] - r[1]) * ih;
      var scale = Math.min(1, 360 / sw, 240 / sh);
      canvas.width = Math.max(1, Math.round(sw * scale));
      canvas.height = Math.max(1, Math.round(sh * scale));
      canvas.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
    }
    var crop = null;
    try { crop = canvas.toDataURL('image/png'); } catch (e) { /* ignore */ }
    state.region = { rect: r, crop: crop };
    keptRect = rectEl();
    keptRect.classList.add('kept');
    placeRect(keptRect, r);
    layer.appendChild(keptRect);
    $('region-chip').hidden = false;
    setTab('ask');
    qInput.focus();
  }
  function clearRegion() {
    state.region = null;
    if (keptRect) { keptRect.remove(); keptRect = null; }
    $('region-chip').hidden = true;
  }
  $('region-remove').addEventListener('click', function () { clearRegion(); qInput.focus(); });

  /* =====================================================================
   * Tabs
   * ===================================================================== */
  function setTab(tab) {
    state.tab = tab;
    ['notes', 'ask'].forEach(function (t) {
      $('tab-' + t).setAttribute('aria-selected', String(t === tab));
      $('pane-' + t).hidden = t !== tab;
    });
    if (tab === 'ask') { renderThread({ switched: true }); }
    requestAnimationFrame(function () { ST.fitDisplayMath($('pane-' + tab)); });
  }
  document.querySelectorAll('.tab').forEach(function (b) {
    b.addEventListener('click', function () { setTab(b.dataset.tab); });
  });

  /* =====================================================================
   * Notes
   * ===================================================================== */
  var notesTimer = null;
  var notesData = null;   // for state.n
  var notesSig = '';

  function renderNotes(reset) {
    var d = notesData;
    var body = $('notes-body');
    var sc = $('notes-scroll');
    var regen = $('regen');
    var n = state.n;
    $('notes-title').textContent = n ? n + '. ' + ((d && d.title) || slideTitle(n) || 'Slide ' + n) : '';
    var sig = n + '|' + (d ? d.status + '|' + (d.error || '') + '|' + (d.markdown || '').length + '|' + (d.markdown || '').slice(-40) : 'loading');
    if (sig === notesSig && !reset) return;
    notesSig = sig;
    var keep = sc.scrollTop;

    regen.hidden = !d;
    if (!d) {
      body.replaceChildren(el('div', { class: 'notes-status' }, el('span', { class: 'spinner' }), 'Loading notes…'));
    } else if (d.status === 'done' && d.markdown != null) {
      var md = el('div');
      ST.renderMarkdown(md, d.markdown);
      body.replaceChildren(md);
      regen.textContent = 'Regenerate';
      regen.disabled = false;
    } else if (d.status === 'generating') {
      body.replaceChildren(el('div', { class: 'notes-status' }, el('span', { class: 'spinner' }), 'Generating notes for this slide…'));
      regen.textContent = 'Regenerate';
      regen.disabled = true;
    } else if (d.status === 'error') {
      body.replaceChildren(el('div', { class: 'notes-status is-error' }, 'Notes failed: ' + (d.error || 'unknown error')));
      regen.textContent = 'Retry';
      regen.disabled = false;
    } else {
      var st = state.lecture && state.lecture.status;
      var txt = st === 'preparing'
        ? 'Waiting in the queue. Notes for this slide will appear here when ready.'
        : st === 'paused'
          ? 'Preparation is paused (usage limit). Notes will continue automatically.'
          : 'No notes yet. Use “Prepare lecture” in the header to generate all slides, or generate just this one.';
      body.replaceChildren(el('div', { class: 'notes-status' }, st === 'preparing' ? el('span', { class: 'spinner' }) : null, txt));
      regen.textContent = 'Generate this slide';
      regen.disabled = false;
    }
    sc.scrollTop = reset ? 0 : keep;
  }

  async function loadNotes(n, opts) {
    clearTimeout(notesTimer);
    opts = opts || {};
    if (opts.reset) { notesData = null; notesSig = ''; renderNotes(true); }
    try {
      var d = await ST.api(API + '/slides/' + n + '/notes');
      if (state.n !== n) return;
      notesData = d;
      renderNotes(false);
      if (d.status === 'pending' || d.status === 'generating') {
        notesTimer = setTimeout(function () { loadNotes(n); }, 2000);
      } else if (opts.wasWaiting || (state.slides[n - 1] && !state.slides[n - 1].has_notes && d.status === 'done')) {
        if (state.slides[n - 1]) state.slides[n - 1].has_notes = d.status === 'done';
        reflectSlideNotes();
      }
    } catch (e) {
      if (state.n !== n) return;
      $('notes-body').replaceChildren(el('div', { class: 'notes-status is-error' }, 'Could not load notes: ' + e.message));
      notesSig = '';
      notesTimer = setTimeout(function () { loadNotes(n); }, 4000);
    }
  }

  $('regen').addEventListener('click', async function () {
    var n = state.n;
    $('regen').disabled = true;
    try {
      await ST.api(API + '/slides/' + n + '/regenerate', { method: 'POST' });
      if (state.n !== n) return;
      notesData = { n: n, title: (notesData && notesData.title) || '', markdown: null, status: 'generating', error: null };
      renderNotes(true);
      loadNotes(n, { wasWaiting: true });
    } catch (e) {
      $('notes-body').replaceChildren(el('div', { class: 'notes-status is-error' }, 'Could not regenerate: ' + e.message));
      notesSig = '';
      $('regen').disabled = false;
    }
  });

  /* =====================================================================
   * Chat threads (per slide, kept in memory)
   * ===================================================================== */
  var threads = new Map(); // n -> {messages, loaded, loading, streaming, loadError}
  function thread(n) {
    var t = threads.get(n);
    if (!t) { t = { messages: [], loaded: false, loading: null, streaming: false, loadError: null }; threads.set(n, t); }
    return t;
  }

  function loadThread(n, force) {
    var t = thread(n);
    if (t.streaming) { t.loaded = true; return Promise.resolve(); }
    if (t.loaded && !force) return Promise.resolve();
    if (t.loading) return t.loading;
    t.loading = (async function () {
      try {
        var r = await ST.api(API + '/slides/' + n + '/chat');
        if (!t.streaming) t.messages = r.messages || [];
        t.loaded = true;
        t.loadError = null;
      } catch (e) {
        t.loadError = e.message;
      } finally {
        t.loading = null;
        if (state.n === n) renderThread({});
      }
    })();
    return t.loading;
  }

  function nearBottom() {
    return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 90;
  }

  function fillContent(node, msg) {
    var md = node.querySelector('.md-slot');
    if (msg.role === 'assistant' && msg.streaming && !msg.content) {
      md.replaceChildren(el('span', { class: 'thinking' }, 'thinking', el('i', { text: '.' }), el('i', { text: '.' }), el('i', { text: '.' })));
      md.classList.remove('md');
    } else if (msg.content) {
      ST.renderMarkdown(md, msg.content, { breaks: msg.role === 'user' });
    } else {
      md.replaceChildren();
    }
    var errEl = node.querySelector('.msg-err');
    if (msg.error) {
      if (!errEl) { errEl = el('div', { class: 'msg-err' }); node.querySelector('.bubble').appendChild(errEl); }
      errEl.textContent = 'Error: ' + msg.error;
    } else if (errEl) errEl.remove();
  }

  // streaming updates: only touch messages that are currently on screen
  function fillMsg(msg) {
    var node = msg._el;
    if (!node || !node.isConnected) return;
    var stick = nearBottom();
    fillContent(node, msg);
    if (stick) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function buildMsg(msg) {
    var bubble = el('div', { class: 'bubble' });
    var crop = msg.localCrop || (msg.region && msg.region.image);
    if (crop) bubble.appendChild(el('img', { class: 'msg-crop', src: crop, alt: 'Selected region of the slide' }));
    bubble.appendChild(el('div', { class: 'md-slot' }));
    var node = el('div', { class: 'msg ' + msg.role }, bubble);
    msg._el = node;
    fillContent(node, msg);
    return node;
  }

  var shownThreadN = null;
  function renderThread(opts) {
    if (state.tab !== 'ask' && !(opts && opts.force)) { shownThreadN = null; return; }
    var n = state.n;
    if (!n) return;
    var t = thread(n);
    var switched = (opts && opts.switched) || shownThreadN !== n;
    var stick = switched || nearBottom();
    shownThreadN = n;
    var kids = [];
    if (t.loadError && !t.messages.length) {
      kids.push(el('div', { class: 'ask-empty err' }, 'Could not load the thread: ' + t.loadError));
    } else if (!t.loaded && !t.messages.length) {
      kids.push(el('div', { class: 'ask-empty' }, 'Loading…'));
    } else if (!t.messages.length) {
      kids.push(el('div', { class: 'ask-empty' },
        'Ask anything about this slide. The thread is saved per slide.',
        el('br'), 'Drag a rectangle on the slide to ask about one part.'));
    } else {
      t.messages.forEach(function (m) { kids.push(buildMsg(m)); });
    }
    messagesEl.replaceChildren.apply(messagesEl, kids);
    if (stick) messagesEl.scrollTop = messagesEl.scrollHeight;
    updateComposer();
  }

  function updateAskHead() {
    $('ask-title').textContent = 'Slide ' + state.n + ' thread' + (slideTitle(state.n) ? ' · ' + slideTitle(state.n) : '');
  }

  function updateComposer() {
    var t = thread(state.n);
    var busy = t.streaming;
    sendBtn.disabled = busy;
    sendBtn.textContent = busy ? '…' : 'Send';
    ['q-simpler', 'q-exam', 'q-check'].forEach(function (k) { $(k).disabled = busy; });
    $('clear-thread').disabled = busy || !t.messages.length;
    qInput.placeholder = busy ? 'Waiting for the answer…' : 'Ask about this slide…';
  }

  /* ---------- SSE ---------- */
  function parseFrame(frame) {
    var ev = 'message', data = [];
    frame.split('\n').forEach(function (line) {
      if (!line || line[0] === ':') return;
      var i = line.indexOf(':');
      var field = i < 0 ? line : line.slice(0, i);
      var val = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '');
      if (field === 'event') ev = val;
      else if (field === 'data') data.push(val);
    });
    if (!data.length) return null;
    var parsed;
    try { parsed = JSON.parse(data.join('\n')); } catch (e) { parsed = { text: data.join('\n') }; }
    return { event: ev, data: parsed };
  }

  async function readSSE(resp, onEvent) {
    var reader = resp.body.getReader();
    var dec = new TextDecoder();
    var buf = '';
    for (;;) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buf += dec.decode(chunk.value, { stream: true }).replace(/\r\n/g, '\n');
      var idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        var f = parseFrame(buf.slice(0, idx));
        buf = buf.slice(idx + 2);
        if (f) onEvent(f.event, f.data);
      }
    }
    buf += dec.decode();
    var last = parseFrame(buf.replace(/\r\n/g, '\n'));
    if (last) onEvent(last.event, last.data);
  }

  var fillTimers = new WeakMap();
  function scheduleFill(msg) {
    if (fillTimers.get(msg)) return;
    fillTimers.set(msg, setTimeout(function () {
      fillTimers.delete(msg);
      fillMsg(msg);
    }, 70));
  }

  async function ask(n, question, rect, crop) {
    var t = thread(n);
    if (t.streaming) return;
    await loadThread(n);
    if (t.streaming) return;
    var userMsg = {
      role: 'user', content: question, ts: new Date().toISOString(),
      region: rect ? { rect: rect, image: null } : null, localCrop: crop || null,
    };
    var aMsg = { role: 'assistant', content: '', streaming: true };
    t.messages.push(userMsg, aMsg);
    t.streaming = true;
    if (state.n === n) renderThread({ switched: true, force: true });
    updateComposer();

    var gotDone = false;
    try {
      var resp = await fetch(API + '/slides/' + n + '/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
        body: JSON.stringify({ question: question, region: rect || null }),
      });
      if (!resp.ok) throw await ST.errorFromResponse(resp);
      await readSSE(resp, function (ev, d) {
        if (ev === 'start') {
          var um = d.user_message || {};
          if (um.ts) userMsg.ts = um.ts;
          if (um.region) userMsg.region = um.region;
        } else if (ev === 'delta') {
          aMsg.content += d.text || '';
          scheduleFill(aMsg);
        } else if (ev === 'done') {
          gotDone = true;
          if (d.message) {
            aMsg.content = d.message.content != null ? d.message.content : aMsg.content;
            aMsg.ts = d.message.ts;
          }
          refreshUsage();
        } else if (ev === 'error') {
          aMsg.error = d.message || 'The request failed.';
        }
      });
      if (!gotDone && !aMsg.error && !aMsg.content) aMsg.error = 'The answer stream ended unexpectedly.';
    } catch (e) {
      aMsg.error = e.message || String(e);
    } finally {
      aMsg.streaming = false;
      t.streaming = false;
      clearTimeout(fillTimers.get(aMsg));
      fillTimers.delete(aMsg);
      fillMsg(aMsg);
      updateComposer();
    }
  }

  async function refreshUsage() {
    try {
      var p = await ST.api(API + '/progress');
      state.lecture.usage = p.usage;
      renderHeader();
    } catch (e) { /* ignore */ }
  }

  /* ---------- composer ---------- */
  function autosize() {
    qInput.style.height = 'auto';
    qInput.style.height = Math.min(qInput.scrollHeight + 2, 180) + 'px';
  }

  function submit(text, opts) {
    text = (text || '').trim();
    if (!text || !state.n) return;
    var n = state.n;
    if (thread(n).streaming) return;
    var rect = state.region ? state.region.rect.slice() : null;
    var crop = state.region ? state.region.crop : null;
    clearRegion();
    if (opts && opts.clearInput) { qInput.value = ''; autosize(); }
    setTab('ask');
    ask(n, text, rect, crop);
  }

  sendBtn.addEventListener('click', function () { submit(qInput.value, { clearInput: true }); });
  qInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit(qInput.value, { clearInput: true });
    }
  });
  qInput.addEventListener('input', autosize);

  $('q-simpler').addEventListener('click', function () { submit(QUICK.simpler); });
  $('q-exam').addEventListener('click', function () { submit(QUICK.exam); });
  $('q-check').addEventListener('click', function () {
    var v = qInput.value.trim();
    setTab('ask');
    if (v) {
      submit('Check my answer:\n' + v, { clearInput: true });
    } else {
      qInput.value = 'Check my answer: ';
      autosize();
      qInput.focus();
      qInput.setSelectionRange(qInput.value.length, qInput.value.length);
    }
  });

  $('clear-thread').addEventListener('click', async function () {
    var n = state.n, t = thread(n);
    if (t.streaming) return;
    try {
      await ST.api(API + '/slides/' + n + '/chat', { method: 'DELETE' });
      t.messages = [];
      t.loaded = true;
      if (state.n === n) renderThread({ switched: true, force: true });
    } catch (e) {
      t.loadError = null;
      messagesEl.appendChild(el('div', { class: 'msg-err' }, 'Could not clear thread: ' + e.message));
    }
  });

  /* =====================================================================
   * Init
   * ===================================================================== */
  async function initSlides() {
    if (!state.total) {
      wrap.hidden = true;
      stageMsg.hidden = false;
      stageMsg.textContent = state.lecture && state.lecture.status === 'processing'
        ? 'Rendering the PDF… slides will appear here.'
        : (state.lecture && state.lecture.status === 'error' ? 'Processing failed: ' + ((state.lecture.prep || {}).message || 'unknown error') : 'This lecture has no slides.');
      return;
    }
    buildThumbs();
    var start = parseInt(location.hash.slice(1), 10);
    if (!start) start = parseInt(ST.lsGet(state.lsKey), 10);
    if (!start) start = 1;
    state.n = 0;
    go(start, { force: true });
  }

  async function init() {
    if (!id) { stageMsg.textContent = 'No lecture id in the URL.'; return; }
    updateAskHead();
    try {
      await loadLecture();
    } catch (e) {
      $('title').textContent = 'Lecture not found';
      stageMsg.textContent = 'Could not load this lecture: ' + e.message;
      return;
    }
    await initSlides();
    if (ST.isBusy(state.lecture.status)) pollProgress();
  }
  init();
})();
