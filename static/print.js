/* Print page: A4 handout, one page per slide, notes font fitted to the page. */
(function () {
  'use strict';
  var ST = window.ST, el = ST.el;
  var $ = function (id) { return document.getElementById(id); };
  var id = ST.lectureIdFromPath();
  var API = '/api/lectures/' + ST.enc(id || '');
  var MAX_PT = 11, MIN_PT = 8, STEP = 0.25;

  function nextFrame() { return new Promise(function (r) { requestAnimationFrame(function () { r(); }); }); }

  function fits(page, notes) {
    var cs = getComputedStyle(page);
    var padB = parseFloat(cs.paddingBottom) || 0;
    var limit = page.getBoundingClientRect().top + page.clientHeight - padB;
    return notes.getBoundingClientRect().bottom <= limit + 0.5;
  }

  function fitText(page, notes) {
    var size = MAX_PT;
    notes.style.fontSize = size + 'pt';
    while (!fits(page, notes) && size > MIN_PT) {
      size = Math.max(MIN_PT, size - STEP);
      notes.style.fontSize = size + 'pt';
    }
    return size;
  }

  function fitPage(page) {
    var notes = page.querySelector('.notes');
    page.classList.remove('grow', 'tight');
    page.style.removeProperty('--img-w');
    var size = fitText(page, notes);
    if (!fits(page, notes)) {
      // still too long at the floor: shrink the slide image a little (never below 100 mm) before growing
      page.style.setProperty('--img-w', '100mm');
      size = fitText(page, notes);
      page.dataset.imgWidth = '100';
    }
    if (!fits(page, notes)) {
      page.classList.add('tight');
      size = fitText(page, notes);
    }
    if (!fits(page, notes)) {
      page.classList.add('grow');
      page.dataset.overflow = '1';
      page.appendChild(el('span', { class: 'page-flag', text: 'Notes exceed one page at 8 pt' }));
    }
    page.dataset.fontSize = String(size);
  }

  function setStatus(text, ready) {
    var s = $('tb-status');
    s.textContent = text;
    s.classList.toggle('ready', !!ready);
  }

  async function main() {
    $('print-btn').addEventListener('click', function () { window.print(); });
    if (!id) { setStatus('No lecture id in the URL.'); document.body.dataset.ready = '1'; return; }
    $('back-link').setAttribute('href', '/lecture/' + ST.enc(id));
    $('pdf-link').setAttribute('href', API + '/pdf');
    ST.api('/api/status').then(function (s) {
      if (s && s.playwright_pdf) $('pdf-link').hidden = false;
    }).catch(function () { /* ignore */ });

    var lecture, notesList;
    try {
      var res = await Promise.all([ST.api(API), ST.api(API + '/notes')]);
      lecture = res[0]; notesList = res[1];
    } catch (e) {
      setStatus('Could not load: ' + e.message);
      document.body.dataset.ready = '1';
      return;
    }
    var title = lecture.title || lecture.filename || id;
    $('tb-title').textContent = title;
    document.title = title + ' · handout';

    var byN = {};
    (notesList || []).forEach(function (x) { byN[x.n] = x; });
    var slides = (lecture.slides && lecture.slides.length) ? lecture.slides
      : (notesList || []).map(function (x) { return { n: x.n, title: x.title }; });

    var host = $('pages');
    var frag = document.createDocumentFragment();
    var imgs = [];
    slides.forEach(function (s) {
      var note = byN[s.n] || {};
      var img = el('img', { src: API + '/slides/' + s.n + '/image?size=lg', alt: 'Slide ' + s.n, decoding: 'async' });
      imgs.push(img);
      var notes = el('div', { class: 'notes' });
      if (note.markdown) ST.renderMarkdown(notes, note.markdown);
      else notes.appendChild(el('p', { class: 'notes-empty', text: 'No notes generated for this slide yet.' }));
      var page = el('section', { class: 'page', dataset: { slide: s.n } },
        el('div', { class: 'page-head' },
          el('span', { class: 't', text: 'Slide ' + s.n + (s.title || note.title ? ' · ' + (s.title || note.title) : '') }),
          el('span', { class: 'l', text: title })),
        el('div', { class: 'slide-box' }, img),
        notes);
      frag.appendChild(page);
    });
    host.appendChild(frag);

    setStatus('Fitting pages…');
    await Promise.all(imgs.map(function (im) {
      return (im.decode ? im.decode() : Promise.resolve()).catch(function () { /* broken image: keep going */ });
    }));
    try { await document.fonts.ready; } catch (e) { /* ignore */ }
    await nextFrame();
    try { await document.fonts.ready; } catch (e) { /* ignore */ }

    var pages = Array.prototype.slice.call(host.querySelectorAll('.page'));
    for (var i = 0; i < pages.length; i++) {
      fitPage(pages[i]);
      if (i % 4 === 3) await nextFrame();
    }
    // fonts that loaded late can change metrics: one verification pass
    try { await document.fonts.ready; } catch (e) { /* ignore */ }
    pages.forEach(function (p) { if (!p.classList.contains('grow')) fitPage(p); });

    var over = pages.filter(function (p) { return p.dataset.overflow; }).length;
    setStatus('Ready · ' + pages.length + ' page' + (pages.length === 1 ? '' : 's') +
      (over ? ' · ' + over + ' overflow at 8 pt' : ''), true);
    document.body.dataset.ready = '1';
  }

  main().catch(function (e) {
    setStatus('Error: ' + e.message);
    document.body.dataset.ready = '1';
  });
})();
