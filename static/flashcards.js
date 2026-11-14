/* Flashcards page: the cards as one plain text block ("Question >> Answer" per line), copy, regenerate + polling. */
(function () {
  'use strict';
  var ST = window.ST, el = ST.el;
  var $ = function (id) { return document.getElementById(id); };

  var id = ST.lectureIdFromPath();
  var base = '/api/lectures/' + ST.enc(id || '');
  var data = null;        // last GET /flashcards response
  var text = '';          // the cards as they are in flashcards.txt
  var pollTimer = null;

  /* ---------- header ---------- */
  function renderCrumbs(d) {
    var kindName = d.kind === 'recitation' ? 'Recitations' : 'Lectures';
    var title = d.title || id;
    var c = $('crumbs');
    var kids = [];
    function sep() { return el('span', { class: 'sep', 'aria-hidden': 'true', text: ' › ' }); }
    if (d.subject) {
      kids.push(el('a', { class: 'subject', href: '/#subject=' + ST.enc(d.subject), text: d.subject, title: 'All ' + d.subject + ' slides' }),
        sep(), el('span', { class: 'kindname', text: kindName }), sep());
    }
    kids.push(el('a', { class: 'deck', href: '/lecture/' + ST.enc(id), text: title, title: 'Open the slides' }),
      sep(), el('span', { class: 'here', 'aria-current': 'page', text: 'Flashcards' }));
    c.replaceChildren.apply(c, kids);
    document.title = (d.subject ? d.subject + ' › ' + kindName + ' › ' : '') + title + ' › Flashcards · Slide Tutor';
    $('fc-heading').textContent = title;
  }

  /* ---------- provenance ---------- */
  function fmtStamp(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return '';
    return d.toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' }) + ', ' +
      d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  function fmtResume(sec) {
    var d = new Date(sec * 1000);
    var t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return d.toDateString() === new Date().toDateString() ? t : d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + t;
  }
  function renderProvenance(f, n) {
    var p = $('fc-prov');
    if (!n) { p.hidden = true; return; }
    var bits = [n + ' card' + (n === 1 ? '' : 's')];
    if (f.updated) bits.push('written ' + fmtStamp(f.updated));
    bits.push(f.exams && f.exams.length ? 'from past exams: ' + f.exams.join(', ') : 'no past exams used');
    p.textContent = bits.join(' · ');
    p.hidden = false;
  }

  /* ---------- status banner + the one primary action ---------- */
  function actionFor(f, n) {
    switch (f.status) {
      case 'ready': return n > 0 ? { label: 'Regenerate', confirm: true } : { label: 'Make flashcards' };
      case 'idle': return { label: 'Make flashcards' };
      case 'waiting_exams': return { label: 'Make without exams' };
      case 'error': return { label: 'Retry' };
      default: return null;   // waiting_notes, paused, queued, generating
    }
  }

  function renderStatus(f, n) {
    var box = $('fc-status');
    var kids = null, err = false;
    var st = f.status;
    if (st === 'queued' || st === 'generating') {
      kids = [el('span', { class: 'spinner', 'aria-hidden': 'true' }),
        (st === 'queued' ? 'Queued. ' : 'Writing flashcards. ') + 'This takes 1 to 3 minutes and the page updates by itself.' +
        (n ? ' The previous cards stay below until the new ones are ready.' : '')];
    } else if (st === 'waiting_notes') {
      kids = ['The notes for this deck are not finished yet. Flashcards are made automatically once they are.'];
    } else if (st === 'waiting_exams') {
      kids = ['No past exams yet. Put exam PDFs into ', el('code', { text: (data.subject || '<Subject>') + '/Exams/' }),
        ' and the cards are made automatically, or make them now without exams.'];
    } else if (st === 'idle') {
      kids = ['Flashcards are not made automatically for this deck.'];
    } else if (st === 'paused') {
      kids = ['Paused: your plan usage limit was reached. ' +
        (f.resets_at ? 'Generation resumes by itself around ' + fmtResume(f.resets_at) + '.' : 'Generation resumes by itself when the limit resets.')];
    } else if (st === 'error') {
      err = true;
      kids = ['Could not write the flashcards: ' + (f.error || 'unknown error') + (n ? ' The previous cards are kept below.' : '')];
    }
    if (!kids) { box.hidden = true; return; }
    box.replaceChildren.apply(box, kids);
    box.classList.toggle('is-error', err);
    box.classList.toggle('is-paused', st === 'paused');
    box.hidden = false;
  }

  function renderToolbar(f, n) {
    var act = actionFor(f, n);
    var copy = $('fc-copy'), dl = $('fc-download'), btn = $('fc-action');
    copy.hidden = !n;
    dl.hidden = !n;
    dl.setAttribute('href', base + '/flashcards.txt');
    btn.hidden = !act;
    if (act) { btn.textContent = act.label; btn.dataset.confirm = act.confirm ? '1' : ''; }
    $('fc-toolbar').hidden = copy.hidden && dl.hidden && btn.hidden;
  }

  /* ---------- cards: plain text, exactly as in flashcards.txt ---------- */
  function renderCards(cards) {
    var t = cards.map(function (c) { return c.q + ' >> ' + c.a; }).join('\n');
    if (t === text) return;
    text = t;
    var box = $('fc-text');
    box.replaceChildren.apply(box, cards.map(function (c, i) {
      return el('div', { class: 'fc-line' }, c.q + ' >> ' + c.a);
    }));
    box.hidden = !cards.length;
  }

  /* ---------- page state ---------- */
  function render(d) {
    data = d;
    var f = d.flashcards || {};
    var cards = d.cards || [];
    renderCrumbs(d);
    var msg = $('fc-msg');
    msg.classList.remove('err');
    if (!f.enabled) {
      $('fc-status').hidden = true; $('fc-toolbar').hidden = true; $('fc-prov').hidden = true;
      renderCards([]);
      msg.textContent = 'Flashcards are not enabled for this subject. Add a flashcards.md file to the subject folder to turn them on.';
      msg.hidden = false;
      return;
    }
    renderProvenance(f, cards.length);
    renderStatus(f, cards.length);
    renderToolbar(f, cards.length);
    renderCards(cards);
    if (cards.length) {
      msg.hidden = true;
    } else {
      msg.textContent = 'No flashcards yet.';
      msg.hidden = !$('fc-status').hidden;   // the status banner already explains why
    }
    schedulePoll();
  }

  async function load() {
    try {
      render(await ST.api(base + '/flashcards'));
    } catch (e) {
      if (!data) {
        $('crumbs').textContent = 'Flashcards';
        var m = $('fc-msg');
        m.textContent = 'Could not load flashcards: ' + e.message;
        m.classList.add('err');
        m.hidden = false;
      } else {
        schedulePoll();   // transient failure while polling: keep trying
      }
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    var st = data && data.flashcards && data.flashcards.status;
    if (st === 'queued' || st === 'generating') pollTimer = setTimeout(load, 2000);
  }

  /* ---------- events ---------- */
  var copiedTimer = null;
  $('fc-copy').addEventListener('click', async function () {
    var out = $('fc-copied');
    try {
      await navigator.clipboard.writeText(text + '\n');
      out.textContent = 'Copied ' + text.split('\n').length + ' cards';
    } catch (e) {   // no clipboard access: select the block so ⌘C works
      var r = document.createRange(); r.selectNodeContents($('fc-text'));
      var sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
      out.textContent = 'Selected: press ⌘C to copy';
    }
    clearTimeout(copiedTimer);
    copiedTimer = setTimeout(function () { out.textContent = ''; }, 2500);
  });

  $('fc-action').addEventListener('click', async function () {
    var b = $('fc-action');
    if (b.dataset.confirm && !confirm('Regenerate the flashcards? The current cards are replaced when the new ones are ready.')) return;
    b.disabled = true;
    try {
      var upd = await ST.api(base + '/flashcards', { method: 'POST' });
      // the POST returns the lecture summary; fetch the page payload (keeps the old cards visible meanwhile)
      data = Object.assign({}, data, { flashcards: upd.flashcards });
      render(Object.assign({}, data, { cards: data.cards }));
      await load();
    } catch (e) {
      var m = $('fc-msg');
      m.textContent = 'Could not start: ' + e.message;
      m.classList.add('err');
      m.hidden = false;
    } finally { b.disabled = false; }
  });

  if (!id) { $('fc-msg').textContent = 'No lecture in the address.'; return; }
  load();
})();
