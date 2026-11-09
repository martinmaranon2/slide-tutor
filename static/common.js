/* Shared helpers: fetch wrappers, formatting, and renderMarkdown (Markdown + KaTeX + code).
 * Classic script (no modules) so it also works when opened via file:// in tests.
 * Exposes: window.ST (helpers) and window.renderMarkdown.
 */
(function () {
  'use strict';

  /* ---------- tiny utilities ---------- */

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  // el('div', {class:'x', onclick: fn, dataset:{a:1}}, child, 'text', ...)
  function el(tag, attrs) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v == null || v === false) return;
        if (k === 'class') node.className = v;
        else if (k === 'dataset') Object.assign(node.dataset, v);
        else if (k === 'text') node.textContent = v;
        else if (k.slice(0, 2) === 'on' && typeof v === 'function') node.addEventListener(k.slice(2), v);
        else if (v === true) node.setAttribute(k, '');
        else node.setAttribute(k, v);
      });
    }
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }
  function append(node, child) {
    if (child == null || child === false) return;
    if (Array.isArray(child)) child.forEach(function (c) { append(node, c); });
    else node.appendChild(typeof child === 'string' || typeof child === 'number' ? document.createTextNode(String(child)) : child);
  }

  /* ---------- fetch helpers ---------- */

  async function errorFromResponse(r) {
    var msg = r.status + ' ' + r.statusText;
    try {
      var ct = r.headers.get('content-type') || '';
      if (ct.indexOf('json') >= 0) {
        var j = await r.json();
        var d = j && (j.detail || j.message || j.error);
        if (d) msg = typeof d === 'string' ? d : JSON.stringify(d);
      } else {
        var t = await r.text();
        if (t) msg = t.slice(0, 300);
      }
    } catch (e) { /* ignore */ }
    var err = new Error(msg);
    err.status = r.status;
    return err;
  }

  // JSON request; throws Error(message) on non-2xx.
  async function api(path, opts) {
    var r = await fetch(path, opts || {});
    if (!r.ok) throw await errorFromResponse(r);
    var ct = r.headers.get('content-type') || '';
    return ct.indexOf('json') >= 0 ? r.json() : r.text();
  }
  function apiJson(path, method, body) {
    return api(path, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }
  function enc(s) { return encodeURIComponent(s); }

  /* ---------- formatting ---------- */

  function fmtNum(n) {
    n = Number(n) || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M';
    if (n >= 1e4) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1).replace(/\.0$/, '') + 'k';
    if (n >= 1000) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
    return String(Math.round(n));
  }
  // "in 97k · out 15k · ≈ $0.41 API-equiv" (input includes cache creation + reads)
  function usageText(u) {
    if (!u || !u.calls) return '';
    var input = (u.input_tokens || 0) + (u.cache_creation_input_tokens || 0) + (u.cache_read_input_tokens || 0);
    var cost = Number(u.cost_usd) || 0;
    return 'in ' + fmtNum(input) + ' · out ' + fmtNum(u.output_tokens || 0) +
      ' · ≈ $' + cost.toFixed(cost < 1 ? 2 : 2) + ' API-equiv';
  }
  function pct(x) { return Math.round((Number(x) || 0) * 100) + '%'; }
  // {text, warn} from /api/status rate_limit, or null
  function rateLimitInfo(rl) {
    if (!rl) return null;
    var parts = [];
    var warn = false;
    [['five_hour', '5 h'], ['seven_day', '7 d']].forEach(function (p) {
      var w = rl[p[0]];
      if (!w || w.utilization == null) return;
      parts.push(p[1] + ': ' + pct(w.utilization));
      if (w.utilization >= 0.9 || (w.status && w.status !== 'allowed')) warn = true;
    });
    if (!parts.length) return null;
    return { text: 'Plan usage · ' + parts.join(' · '), warn: warn };
  }
  var BUSY = { processing: 1, preparing: 1, paused: 1 };
  function isBusy(status) { return !!BUSY[status]; }
  var STATUS_LABEL = {
    processing: 'Rendering PDF…', ready_to_prepare: 'Ready to prepare', preparing: 'Preparing notes…',
    paused: 'Paused (usage limit)', ready: 'Ready', incomplete: 'Incomplete', error: 'Error',
  };

  /* ---------- Markdown + math ---------- */

  var INLINE_DOLLAR = /(?<![\\$])\$(?![\s$])((?:\\[\s\S]|[^$\\\n]|\n(?![ \t]*\n))+?)(?<![\s\\])\$(?!\d)/g;

  function protectCode(src, store) {
    function ph(m) { store.push(m); return 'CODEPH' + (store.length - 1) + 'END'; }
    // fenced blocks (also unterminated ones at the end of the text)
    src = src.replace(/^[ \t]*(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^[ \t]*\1[ \t]*$|(?![\s\S]))/gm, ph);
    // inline code spans
    src = src.replace(/(?<!`)(`+)(?!`)([\s\S]*?[^`])\1(?!`)/g, function (m) {
      if (/\n[ \t]*\n/.test(m)) return m;
      return ph(m);
    });
    return src;
  }

  function extractMath(src, store) {
    function add(tex, display) {
      store.push({ tex: tex, display: display });
      return 'MATHPH' + (store.length - 1) + 'END';
    }
    // display
    // (?<!\\): a "$$" formed by an escaped dollar plus a closing "$" (e.g. "$\$$", $ = cache) is not display math
    src = src.replace(/(?<!\\)\$\$([\s\S]+?)(?<!\\)\$\$/g, function (m, t) { return add(t.trim(), true); });
    src = src.replace(/\\\[([\s\S]+?)\\\]/g, function (m, t) { return add(t.trim(), true); });
    // bare environments written without delimiters
    src = src.replace(/\\begin\{(equation|align|gather|multline)(\*?)\}([\s\S]*?)\\end\{\1\2\}/g, function (m, env, star, body) {
      var inner = body.trim();
      if (env === 'equation') return add(inner, true);
      var name = env === 'align' ? 'aligned' : 'gathered';
      return add('\\begin{' + name + '}' + inner + '\\end{' + name + '}', true);
    });
    // inline
    src = src.replace(/\\\(([\s\S]+?)\\\)/g, function (m, t) { return add(t.trim(), false); });
    src = src.replace(INLINE_DOLLAR, function (m, t) { return add(t, false); });
    return src;
  }

  function renderTex(tex, display) {
    try {
      return window.katex.renderToString(tex, {
        displayMode: display, throwOnError: false, strict: 'ignore', trust: false,
      });
    } catch (e) {
      return '<span class="katex-error" title="' + escapeHtml(e.message) + '">' + escapeHtml(tex) + '</span>';
    }
  }

  var PH_RE = /MATHPH(\d+)END/g;

  function fillMath(root, maths) {
    // attributes: put raw tex back (never HTML)
    root.querySelectorAll('*').forEach(function (node) {
      for (var i = 0; i < node.attributes.length; i++) {
        var a = node.attributes[i];
        if (a.value.indexOf('MATHPH') >= 0) {
          a.value = a.value.replace(PH_RE, function (m, k) { return maths[k] ? maths[k].tex : m; });
        }
      }
    });
    // text nodes: swap in rendered KaTeX
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var nodes = [];
    while (walker.nextNode()) {
      if (walker.currentNode.nodeValue.indexOf('MATHPH') >= 0) nodes.push(walker.currentNode);
    }
    nodes.forEach(function (tn) {
      var html = escapeHtml(tn.nodeValue).replace(PH_RE, function (m, k) {
        var item = maths[k];
        return item ? renderTex(item.tex, item.display) : m;
      });
      var holder = document.createElement('span');
      holder.innerHTML = html;
      var frag = document.createDocumentFragment();
      while (holder.firstChild) frag.appendChild(holder.firstChild);
      tn.parentNode.replaceChild(frag, tn);
    });
  }

  function decorate(root) {
    // callouts (detected by text)
    root.querySelectorAll('p, li').forEach(function (node) {
      if (node.tagName === 'LI' && node.querySelector(':scope > p')) return;
      var t = (node.textContent || '').trim();
      if (/^⚠️?\s*check\b/i.test(t)) node.classList.add('callout', 'callout-warn');
      else if (/^takeaway\s*:/i.test(t)) node.classList.add('callout', 'callout-takeaway');
    });
    // "(background)" markers
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        if (n.nodeValue.indexOf('(background)') < 0) return NodeFilter.FILTER_REJECT;
        return n.parentElement && n.parentElement.closest('pre, code, .katex, .katex-error')
          ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      },
    });
    var hits = [];
    while (walker.nextNode()) hits.push(walker.currentNode);
    hits.forEach(function (tn) {
      var parts = tn.nodeValue.split('(background)');
      var frag = document.createDocumentFragment();
      parts.forEach(function (p, i) {
        if (i > 0) frag.appendChild(el('span', { class: 'bg-marker', text: '(background)' }));
        if (p) frag.appendChild(document.createTextNode(p));
      });
      tn.parentNode.replaceChild(frag, tn);
    });
    // tables scroll horizontally when wide
    root.querySelectorAll('table').forEach(function (tb) {
      if (tb.parentElement && tb.parentElement.classList.contains('table-wrap')) return;
      var wrap = el('div', { class: 'table-wrap' });
      tb.parentNode.insertBefore(wrap, tb);
      wrap.appendChild(tb);
    });
    // links open outside the app
    root.querySelectorAll('a[href]').forEach(function (a) {
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
    });
    // syntax highlighting for fenced blocks that name a language
    if (window.hljs) {
      root.querySelectorAll('pre code').forEach(function (code) {
        if (!/\blanguage-\w/.test(code.className)) return;
        try { window.hljs.highlightElement(code); } catch (e) { /* ignore */ }
      });
    }
  }

  // Convert Markdown to a sanitized DOM fragment (math placeholders already replaced by KaTeX).
  function markdownToFragment(md, opts) {
    opts = opts || {};
    var codes = [];
    var maths = [];
    var src = String(md == null ? '' : md).replace(/\r\n?/g, '\n');
    src = protectCode(src, codes);
    src = extractMath(src, maths);
    // a callout/takeaway line directly under other text must start its own paragraph
    src = src.replace(/([^\n])\n(?=(?:\*\*|_)?(?:⚠|Takeaway\s*:))/g, '$1\n\n');
    // restore code (also inside math source)
    var restore = function (s) {
      return s.replace(/CODEPH(\d+)END/g, function (m, i) { return codes[i]; });
    };
    maths.forEach(function (m) { m.tex = restore(m.tex); });
    src = restore(src);

    var html = window.marked.parse(src, { gfm: true, breaks: !!opts.breaks, async: false });
    var clean = window.DOMPurify.sanitize(html);
    var tpl = document.createElement('template');
    tpl.innerHTML = clean;
    var root = tpl.content;
    fillMath(root, maths);
    decorate(root);
    return root;
  }

  function renderMarkdown(target, md, opts) {
    var frag;
    try {
      frag = markdownToFragment(md, opts);
    } catch (e) {
      frag = document.createDocumentFragment();
      frag.appendChild(el('pre', { class: 'render-error', text: String(md) }));
    }
    target.replaceChildren(frag);
    target.classList.add('md');
    if (window.requestAnimationFrame) requestAnimationFrame(function () { fitDisplayMath(target); });
    return target;
  }

  // Shrink display equations that are wider than their column (down to 75%); beyond that they scroll.
  // Skips hidden containers (width 0); call again after they become visible.
  function fitDisplayMath(root) {
    var nodes = (root || document).querySelectorAll('.katex-display');
    for (var i = 0; i < nodes.length; i++) {
      var d = nodes[i];
      if (!d.clientWidth) continue;
      d.style.fontSize = '';
      var ratio = d.clientWidth / d.scrollWidth;
      if (ratio < 1) d.style.fontSize = Math.max(0.75, ratio * 0.98).toFixed(3) + 'em';
    }
  }

  /* ---------- misc ---------- */

  function lectureIdFromPath() {
    var m = location.pathname.match(/\/lecture\/([^/]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }
  function lsGet(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function lsSet(key, val) { try { localStorage.setItem(key, val); } catch (e) { /* ignore */ } }

  window.renderMarkdown = renderMarkdown;
  window.ST = {
    el: el, escapeHtml: escapeHtml, api: api, apiJson: apiJson, enc: enc,
    errorFromResponse: errorFromResponse, fmtNum: fmtNum, usageText: usageText, rateLimitInfo: rateLimitInfo,
    isBusy: isBusy, STATUS_LABEL: STATUS_LABEL,
    renderMarkdown: renderMarkdown, fitDisplayMath: fitDisplayMath, lectureIdFromPath: lectureIdFromPath,
    lsGet: lsGet, lsSet: lsSet,
  };
})();
