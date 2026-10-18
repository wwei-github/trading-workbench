/* docs-wiki 浏览页逻辑（按需加载每篇文档的数据文件，避免首屏加载全量正文） */
(function () {
  'use strict';

  window.DOC_REGISTRY = window.DOC_REGISTRY || {};

  var GROUPS = window.DOC_GROUPS || [];
  var DOCS = window.DOC_INDEX || [];

  var byId = {};
  DOCS.forEach(function (d) { byId[d.id] = d; });

  // 文档相对路径 → id，用于解析正文里的跨文档 .md 链接
  var pathIndex = {};
  DOCS.forEach(function (d) { pathIndex[d.path] = d.id; });

  var defaultId = DOCS.length ? DOCS[0].id : '';

  var el = {
    doc: document.getElementById('doc'),
    nav: document.getElementById('nav'),
    sidebar: document.getElementById('sidebar'),
    scrim: document.getElementById('scrim'),
    menuBtn: document.getElementById('menuBtn'),
    search: document.getElementById('search'),
    searchWrap: document.getElementById('searchWrap'),
    searchClear: document.getElementById('searchClear'),
    searchHint: document.getElementById('searchHint'),
    topbarDoc: document.getElementById('topbarDoc')
  };

  var current = null;
  var renderToken = 0;

  /* ---------- helpers ---------- */

  function slugify(text) {
    return String(text)
      .trim()
      .toLowerCase()
      .replace(/[^\p{L}\p{N}\s_-]/gu, '')
      .replace(/\s/g, '-');
  }

  function resolvePath(curPath, href) {
    var parts = String(curPath).split('/');
    parts.pop();
    var segs = String(href).split('/');
    for (var i = 0; i < segs.length; i++) {
      var s = segs[i];
      if (s === '' || s === '.') continue;
      if (s === '..') parts.pop();
      else parts.push(s);
    }
    return parts.join('/');
  }

  // marked 会把 href 里的非 ASCII 百分号编码（中文路径 → %E6%8A%80…），
  // 还原后才能与 pathIndex 的原始路径比对；非法转义序列（字面量 %）原样返回
  function safeDecode(value) {
    try { return decodeURIComponent(value); } catch (e) { return value; }
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function openSidebar(open) {
    el.sidebar.classList.toggle('open', open);
    el.scrim.classList.toggle('show', open);
  }

  function scrollToTop(instant) {
    var root = document.documentElement;
    var prev = root.style.scrollBehavior;
    if (instant) root.style.scrollBehavior = 'auto';
    window.scrollTo(0, 0);
    if (instant) requestAnimationFrame(function () { root.style.scrollBehavior = prev; });
  }

  /* ---------- 按需加载文档数据 ---------- */
  var loads = {};

  function loadDoc(id) {
    if (window.DOC_REGISTRY[id]) return Promise.resolve(window.DOC_REGISTRY[id]);
    if (loads[id]) return loads[id];
    loads[id] = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = 'assets/docs/' + id + '.js';
      s.async = true;
      s.onload = function () {
        if (window.DOC_REGISTRY[id]) resolve(window.DOC_REGISTRY[id]);
        else reject(new Error('文档数据为空: ' + id));
      };
      s.onerror = function () { reject(new Error('加载失败: ' + id)); };
      document.head.appendChild(s);
    });
    return loads[id];
  }

  /* ---------- 侧边导航 ---------- */
  var navItems = [];

  function buildNav() {
    var frag = document.createDocumentFragment();
    GROUPS.forEach(function (g) {
      var group = document.createElement('div');
      group.className = 'group';
      group.dataset.group = g.id;

      var title = document.createElement('div');
      title.className = 'group-title';
      title.textContent = g.name;
      group.appendChild(title);

      DOCS.filter(function (d) { return d.group === g.id; }).forEach(function (d) {
        var a = document.createElement('a');
        a.className = 'nav-item';
        a.dataset.docId = d.id;
        a.href = '#' + d.id;
        var label = document.createElement('span');
        label.className = 'nav-label';
        label.textContent = d.label;
        a.appendChild(label);
        a.title = d.title;
        group.appendChild(a);
        navItems.push(a);
      });
      frag.appendChild(group);
    });
    el.nav.appendChild(frag);
  }

  function markActive(id) {
    navItems.forEach(function (a) {
      var on = a.dataset.docId === id;
      a.classList.toggle('active', on);
      if (on && a.scrollIntoView) a.scrollIntoView({ block: 'nearest' });
    });
  }

  /* ---------- 搜索（导航元数据级，保持按需加载） ---------- */
  function haystack(d) {
    return (d.label + ' ' + d.title + ' ' + d.path).toLowerCase();
  }

  function filterNav(query) {
    var q = query.trim().toLowerCase();
    var groups = el.nav.querySelectorAll('.group');
    if (!q) {
      navItems.forEach(function (a) { a.classList.remove('hidden'); });
      groups.forEach(function (g) { g.classList.remove('hidden'); });
      el.searchWrap.classList.toggle('has-value', false);
      el.searchHint.textContent = '';
      return;
    }
    var terms = q.split(/\s+/).filter(Boolean);
    var hit = 0;
    navItems.forEach(function (a) {
      var d = byId[a.dataset.docId];
      var hay = haystack(d);
      var ok = terms.every(function (t) { return hay.indexOf(t) !== -1; });
      a.classList.toggle('hidden', !ok);
      if (ok) hit++;
    });
    groups.forEach(function (g) {
      g.classList.toggle('hidden', g.querySelectorAll('.nav-item:not(.hidden)').length === 0);
    });
    el.searchWrap.classList.toggle('has-value', true);
    el.searchHint.textContent = hit + ' / ' + DOCS.length + ' 篇匹配';
  }

  function clearSearch() {
    el.search.value = '';
    filterNav('');
  }

  /* ---------- 渲染 ---------- */
  function headingIdFactory() {
    var seen = {};
    return function (text) {
      var base = slugify(text) || 'section';
      var id = base;
      var n = 1;
      while (seen[id]) { id = base + '-' + n; n++; }
      seen[id] = true;
      return id;
    };
  }

  function enhance(container, docPath, nextId) {
    container.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(function (h) {
      if (!h.id) h.id = nextId(h.textContent);
    });

    container.querySelectorAll('table').forEach(function (t) {
      if (t.parentElement && t.parentElement.classList.contains('table-wrap')) return;
      var wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      t.parentNode.insertBefore(wrap, t);
      wrap.appendChild(t);
    });

    container.querySelectorAll('a[href]').forEach(function (a) {
      var href = a.getAttribute('href') || '';
      if (/^https?:/i.test(href)) {
        a.target = '_blank';
        a.rel = 'noopener';
        return;
      }
      if (href.charAt(0) === '#') {
        a.dataset.anchor = safeDecode(href.slice(1));
        return;
      }
      if (/\.md(#|$)/i.test(href)) {
        var parts = href.split('#');
        var target = resolvePath(docPath, safeDecode(parts[0]));
        var id = pathIndex[target];
        if (id) {
          a.dataset.docId = id;
          if (parts[1]) a.dataset.anchor = safeDecode(parts[1]);
          a.setAttribute('href', '#' + id + (parts[1] ? ':' + parts[1] : ''));
        } else {
          a.classList.add('missing');
          a.title = '未纳入本页：' + target;
          a.removeAttribute('href');
        }
      }
    });

    var nodes = [];
    container.querySelectorAll('pre > code.language-mermaid').forEach(function (code) {
      var pre = code.parentElement;
      pre.classList.add('mermaid');
      pre.textContent = code.textContent;
      nodes.push(pre);
    });
    return nodes;
  }

  function mermaidError(node, src) {
    var box = document.createElement('div');
    box.className = 'mermaid-error';
    box.textContent = '图示渲染失败，以下为原始定义：';
    var pre = document.createElement('pre');
    pre.textContent = src;
    box.appendChild(pre);
    if (node.parentNode) node.parentNode.replaceChild(box, node);
  }

  function renderMermaid(nodes, token) {
    if (!nodes.length || typeof window.mermaid === 'undefined') return;
    nodes.forEach(function (node) {
      var src = node.textContent;
      Promise.resolve()
        .then(function () { return window.mermaid.run({ nodes: [node] }); })
        .catch(function () {
          if (token === renderToken) mermaidError(node, src);
        });
    });
  }

  function docNavHTML(index) {
    var prev = index > 0 ? DOCS[index - 1] : null;
    var next = index < DOCS.length - 1 ? DOCS[index + 1] : null;
    var out = '<nav class="doc-nav">';
    out += prev
      ? '<a href="#' + prev.id + '" data-doc-id="' + prev.id + '"><span class="dn-dir">← 上一篇</span><span class="dn-title">' + esc(prev.label) + '</span></a>'
      : '<span></span>';
    out += next
      ? '<a class="next" href="#' + next.id + '" data-doc-id="' + next.id + '"><span class="dn-dir">下一篇 →</span><span class="dn-title">' + esc(next.label) + '</span></a>'
      : '<span></span>';
    out += '</nav>';
    return out;
  }

  function cssEscape(s) {
    if (window.CSS && window.CSS.escape) return window.CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  }

  function findAnchor(anchor) {
    if (!anchor) return null;
    return el.doc.querySelector('#' + cssEscape(anchor)) ||
      el.doc.querySelector('#' + cssEscape(slugify(anchor)));
  }

  function gotoAnchor(anchor) {
    var target = findAnchor(anchor);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (current) {
      try { history.replaceState(null, '', '#' + current.id + ':' + anchor); } catch (e) { /* file:// 下可能受限 */ }
    }
  }

  function render(id, anchor) {
    var meta = byId[id];
    if (!meta) return false;

    var token = ++renderToken;
    current = { id: id, path: meta.path };
    document.title = meta.label + ' · 交易工作台';
    el.topbarDoc.textContent = meta.path;
    markActive(id);
    scrollToTop(!anchor);
    el.doc.innerHTML = '<p class="loading">加载中…</p>';

    loadDoc(id).then(function (doc) {
      if (token !== renderToken) return;

      window.mermaid.initialize({
        startOnLoad: false,
        theme: 'neutral',
        securityLevel: 'loose',
        fontFamily: "'InstrumentSans', 'PingFang SC', 'Microsoft YaHei', sans-serif",
        flowchart: { htmlLabels: true, useMaxWidth: true },
        themeVariables: { fontSize: '13px', lineColor: '#8895a7' }
      });

      var body;
      try {
        body = window.marked.parse(doc.md, { gfm: true, breaks: false });
      } catch (err) {
        body = '<p class="loading">文档解析失败：' + esc(err.message) + '</p>';
      }

      el.doc.innerHTML = body;
      var nodes = enhance(el.doc, doc.path, headingIdFactory());

      var idx = DOCS.findIndex(function (d) { return d.id === id; });
      el.doc.insertAdjacentHTML('beforeend', docNavHTML(idx));
      el.doc.insertAdjacentHTML('beforeend',
        '<footer class="page-foot">来源：docs/' + esc(doc.path) + ' · 本页为 docs 文档的只读浏览视图（以仓库内文档为准）</footer>');

      renderMermaid(nodes, token);

      if (anchor) {
        var target = findAnchor(anchor);
        if (target) {
          requestAnimationFrame(function () {
            if (token === renderToken) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
          });
        }
      }
    }).catch(function (err) {
      if (token !== renderToken) return;
      el.doc.innerHTML = '<p class="loading">' + esc(err.message) + '</p>';
    });

    return true;
  }

  /* ---------- 路由 ---------- */
  function parseHash() {
    var h = location.hash.replace(/^#/, '');
    if (!h) return {};
    var i = h.indexOf(':');
    if (i < 0) return { id: decodeURIComponent(h) };
    return { id: decodeURIComponent(h.slice(0, i)), anchor: decodeURIComponent(h.slice(i + 1)) };
  }

  function navigate(id, anchor) {
    location.hash = id + (anchor ? ':' + anchor : '');
  }

  function route() {
    var r = parseHash();
    var id = (r.id && byId[r.id]) ? r.id : defaultId;
    if (current && current.id === id) {
      if (r.anchor) gotoAnchor(r.anchor);
      return;
    }
    if (!render(id, r.anchor) && defaultId) render(defaultId, '');
  }

  /* ---------- 事件 ---------- */
  el.nav.addEventListener('click', function (e) {
    var a = e.target.closest('a.nav-item');
    if (!a) return;
    e.preventDefault();
    navigate(a.dataset.docId, '');
    openSidebar(false);
  });

  el.doc.addEventListener('click', function (e) {
    var a = e.target.closest('a');
    if (!a) return;
    if (a.dataset.docId) {
      e.preventDefault();
      navigate(a.dataset.docId, a.dataset.anchor || '');
      scrollToTop(true);
      return;
    }
    if (a.dataset.anchor !== undefined) {
      e.preventDefault();
      gotoAnchor(a.dataset.anchor);
    }
  });

  el.menuBtn.addEventListener('click', function () {
    openSidebar(!el.sidebar.classList.contains('open'));
  });
  el.scrim.addEventListener('click', function () { openSidebar(false); });

  var searchTimer = null;
  el.search.addEventListener('input', function () {
    clearTimeout(searchTimer);
    var v = el.search.value;
    searchTimer = setTimeout(function () { filterNav(v); }, 120);
  });
  el.searchClear.addEventListener('click', function () {
    clearSearch();
    el.search.focus();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && document.activeElement !== el.search) {
      e.preventDefault();
      el.search.focus();
    } else if (e.key === 'Escape' && document.activeElement === el.search) {
      clearSearch();
      el.search.blur();
    }
  });

  window.addEventListener('hashchange', route);

  /* ---------- 启动 ---------- */
  buildNav();
  route();
})();
