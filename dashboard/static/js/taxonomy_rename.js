/* Taxonomy / Rename sub-tab — PR2 (audit, read-only).
 *
 * Surfaces files whose current name diverges from their LLM-extracted
 * title. 3 columns: filtered candidate list / file detail with diff /
 * suggested name preview. Renaming itself comes in PR3+.
 */
(function () {
  'use strict';

  function $(s) { return document.querySelector(s); }
  function el(tag, attrs, children) {
    const n = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class' && v) n.className = v;
      else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
      else if (v != null && v !== false) n.setAttribute(k, v);
    }
    if (children) for (const c of children) {
      if (c == null || c === false) continue;
      n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return n;
  }

  const state = {
    profile: null,
    data: null,                      // last audit response
    selectedRelPath: null,
    activeCategories: new Set(['placeholder', 'divergent']),  // shown by default
    loaded: false,
    // Per-file LLM metadata cache (avoid re-fetching when the user
    // clicks back and forth). Key = rel_path.
    metaCache: new Map(),
    // Currently displayed page per file (defaults to 1). Survives
    // re-renders of the detail panel but not profile changes.
    viewerPageByPath: new Map(),
    // Cap on pages shown in the pager — matches the Mappings cap.
    MAX_PAGES: 5,
  };

  // ── API ──────────────────────────────────────────────────────────────

  async function fetchAudit(force) {
    const url = `/api/rename/audit?profile=${encodeURIComponent(state.profile)}${force ? '&force=true' : ''}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error('audit HTTP ' + r.status);
    return r.json();
  }

  async function fetchFileMeta(relPath) {
    const url = `/api/taxonomy/file/metadata?profile=${encodeURIComponent(state.profile)}`
              + `&path=${encodeURIComponent(relPath)}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error('metadata HTTP ' + r.status);
    return r.json();
  }

  function thumbnailURL(relPath, page) {
    return `/api/taxonomy/file/thumbnail?profile=${encodeURIComponent(state.profile)}`
         + `&path=${encodeURIComponent(relPath)}&page=${page || 1}`;
  }

  // ── Minimal toast + busy helpers (mirror the cat module's approach) ──

  function showToast(msg, type) {
    const t = $('#tax-toast');
    if (!t) { console.log(msg); return; }
    t.textContent = msg;
    t.className = 'tax-toast ' + (type || 'info');
    t.style.display = 'block';
    clearTimeout(showToast._tid);
    showToast._tid = setTimeout(() => { t.style.display = 'none'; }, 4500);
  }

  async function withBusy(label, fn) {
    const overlay = $('#tax-busy');
    const lblEl = $('#tax-busy-label');
    if (overlay) {
      if (lblEl && label) lblEl.textContent = label;
      overlay.classList.add('is-active');
      overlay.setAttribute('aria-hidden', 'false');
    }
    const safety = setTimeout(() => {
      if (overlay) {
        overlay.classList.remove('is-active');
        overlay.setAttribute('aria-hidden', 'true');
      }
      showToast('Action trop longue', 'error');
    }, 30_000);
    try {
      return await fn();
    } finally {
      clearTimeout(safety);
      if (overlay) {
        overlay.classList.remove('is-active');
        overlay.setAttribute('aria-hidden', 'true');
      }
    }
  }

  // ── Header stats ─────────────────────────────────────────────────────

  function renderStats() {
    const out = $('#tax-stats-rename');
    if (!out) return;
    if (!state.data) {
      out.innerHTML = '<span class="muted">Chargement audit rename…</span>';
      return;
    }
    const s = state.data.stats;
    out.innerHTML =
      `<span><strong>${s.n_total}</strong> fichiers</span>` +
      `<span class="dot"></span><span><strong>${s.n_with_title}</strong> avec titre LLM</span>` +
      `<span class="dot"></span><span class="orphan-badge"><strong>${s.n_placeholder}</strong> placeholders</span>` +
      `<span class="dot"></span><span><strong>${s.n_divergent}</strong> divergents</span>` +
      `<span class="dot"></span><span><strong>${s.n_minor_case}</strong> minor case</span>` +
      `<span class="dot"></span><span><strong>${s.n_ok}</strong> ok</span>`;
  }

  // ── Sub-tab toggle ───────────────────────────────────────────────────

  function activateSubtab(view) {
    document.querySelectorAll('.tax-subtab').forEach(b =>
      b.classList.toggle('active', b.dataset.view === view));
    document.querySelectorAll('.tax-view').forEach(v => {
      const match = v.dataset.view === view;
      v.classList.toggle('active', match);
      v.style.display = match ? '' : 'none';
    });
    // Stats banner swap
    const sMap = $('#tax-stats');
    const sCat = $('#tax-stats-categories');
    const sRn = $('#tax-stats-rename');
    if (sMap) sMap.style.display = (view === 'mappings') ? '' : 'none';
    if (sCat) sCat.style.display = (view === 'categories') ? '' : 'none';
    if (sRn) sRn.style.display = (view === 'rename') ? '' : 'none';
    if (view === 'rename' && !state.loaded) {
      loadAndRender();
    }
  }

  // ── Filters bar ──────────────────────────────────────────────────────

  function renderFilters() {
    const wrap = $('#tax-rename-filters');
    wrap.innerHTML = '';
    if (!state.data) return;
    const cats = [
      { id: 'placeholder', label: 'Placeholders', n: state.data.stats.n_placeholder, tone: 'danger' },
      { id: 'divergent', label: 'Divergents', n: state.data.stats.n_divergent, tone: 'warning' },
      { id: 'minor_case', label: 'Minor case', n: state.data.stats.n_minor_case, tone: 'neutral' },
      { id: 'ok', label: 'OK', n: state.data.stats.n_ok, tone: 'success' },
    ];
    for (const c of cats) {
      const active = state.activeCategories.has(c.id);
      wrap.appendChild(el('button', {
        class: 'tax-rename-filter tax-rename-filter-' + c.tone
             + (active ? ' active' : ''),
        onclick: () => {
          if (state.activeCategories.has(c.id)) state.activeCategories.delete(c.id);
          else state.activeCategories.add(c.id);
          // Re-render BOTH the filters (so the pill visually toggles) AND
          // the list (so the visible candidates reflect the new filter).
          renderFilters();
          renderList();
        },
      }, [c.label + ' (' + c.n + ')']));
    }
  }

  // ── Col 1 — candidate list ──────────────────────────────────────────

  function renderList() {
    const wrap = $('#tax-rename-list');
    const sub = $('#tax-rename-list-sub');
    wrap.innerHTML = '';
    if (!state.data) {
      sub.textContent = '';
      return;
    }
    const candidates = state.data.candidates.filter(
      c => state.activeCategories.has(c.category));
    sub.textContent = `${candidates.length} affichés / ${state.data.candidates.length}`;
    if (candidates.length === 0) {
      wrap.appendChild(el('div', { class: 'muted small',
                                    style: 'padding:14px;text-align:center;' },
        ['Aucun candidat avec les filtres actifs.']));
      return;
    }
    const MAX = 500;
    for (const c of candidates.slice(0, MAX)) {
      const selected = state.selectedRelPath === c.rel_path;
      const sim = Math.round(c.similarity * 100);
      wrap.appendChild(el('div', {
        class: 'tax-rename-row' + (selected ? ' selected' : ''),
        title: c.rel_path,
        onclick: () => selectCandidate(c.rel_path),
      }, [
        el('span', {
          class: 'tax-rename-cat tax-rename-cat-' + c.category,
          title: 'Catégorie : ' + c.category,
        }, [_catShortLabel(c.category)]),
        el('span', { class: 'tax-rename-row-current' }, [c.current_name]),
        el('span', { class: 'tax-rename-row-sim',
                     title: 'Similarité courant vs proposition' },
                   [sim + '%']),
      ]));
    }
    if (candidates.length > MAX) {
      wrap.appendChild(el('div', { class: 'muted small',
                                    style: 'padding:8px;text-align:center;' },
        [`+${candidates.length - MAX} autres — affine les filtres`]));
    }
  }

  function _catShortLabel(cat) {
    return { placeholder: 'PH', divergent: 'DIV',
             minor_case: 'CAS', ok: 'OK' }[cat] || cat;
  }

  function selectCandidate(relPath) {
    state.selectedRelPath = relPath;
    renderList();
    renderDetail();
    renderPreview();
    // Lazy fetch the full LLM metadata (gives us themes + language,
    // which the audit response doesn't carry). Re-renders the detail
    // panel once it lands. Cached per file.
    if (!state.metaCache.has(relPath)) {
      fetchFileMeta(relPath)
        .then(meta => {
          state.metaCache.set(relPath, meta);
          if (state.selectedRelPath === relPath) renderDetail();
        })
        .catch(err => {
          state.metaCache.set(relPath, { error: err.message });
          if (state.selectedRelPath === relPath) renderDetail();
        });
    }
  }

  function _candidate() {
    if (!state.data || !state.selectedRelPath) return null;
    return state.data.candidates.find(c => c.rel_path === state.selectedRelPath);
  }

  // ── Col 2 — detail + char diff ──────────────────────────────────────

  function renderDetail() {
    const wrap = $('#tax-rename-detail');
    const sub = $('#tax-rename-detail-sub');
    wrap.innerHTML = '';
    const c = _candidate();
    if (!c) {
      sub.textContent = '';
      wrap.appendChild(el('div', { class: 'muted small',
                                    style: 'padding:30px;text-align:center;' },
        ['Sélectionne un candidat.']));
      return;
    }
    sub.textContent = c.rel_path;

    // ── Cover thumbnail + LLM analysis card ──────────────────────────
    // The audit row already carries the basics (title/author/conf), but
    // a real LLM-meta fetch adds language + the full ranked themes,
    // which help the user confirm the suggestion visually.
    const meta = state.metaCache.get(c.rel_path);

    const currentPage = state.viewerPageByPath.get(c.rel_path) || 1;
    const totalPages = (meta && meta.file && meta.file.page_count_estimate)
                       || state.MAX_PAGES;
    const cappedTotal = Math.min(totalPages, state.MAX_PAGES);

    const coverImg = el('img', {
      class: 'tax-rename-cover',
      src: thumbnailURL(c.rel_path, currentPage),
      alt: 'page ' + currentPage,
      loading: 'lazy',
    });
    coverImg.addEventListener('error', () => {
      coverImg.style.display = 'none';
      coverImg.parentElement.appendChild(
        Object.assign(document.createElement('div'),
                      { className: 'muted small',
                        textContent: '(aperçu indisponible)' }));
    });

    const previewBlock = el('div', { class: 'tax-rename-preview-block' }, [
      el('div', { class: 'tax-rename-cover-wrap' }, [coverImg]),
      _renderPager(c.rel_path, currentPage, cappedTotal),
      el('div', { class: 'tax-rename-llm-card' }, _renderLLMSummary(c, meta)),
    ]);
    wrap.appendChild(previewBlock);

    // Current
    wrap.appendChild(el('div', { class: 'tax-rename-field' }, [
      el('label', null, ['Nom actuel']),
      el('div', { class: 'tax-rename-current-name' }, [c.current_name]),
    ]));
    // Suggested
    wrap.appendChild(el('div', { class: 'tax-rename-field' }, [
      el('label', null, ['Proposition (template)']),
      el('div', { class: 'tax-rename-suggested-name' }, [c.suggested_name]),
    ]));
    // Diff visual: highlight chars NOT shared between the two
    wrap.appendChild(el('div', { class: 'tax-rename-field' }, [
      el('label', null, ['Diff caractère']),
      _renderDiffBlock(c.current_stem, c.suggested_stem),
    ]));
    // LLM metadata
    wrap.appendChild(el('div', { class: 'tax-rename-field' }, [
      el('label', null, ['Métadonnées LLM']),
      el('div', { class: 'tax-rename-meta' }, [
        el('div', null, ['📕 ', el('strong', null, [c.title || '(sans titre)'])]),
        c.author ? el('div', { class: 'muted small' }, ['✍ ' + c.author]) : null,
        el('div', { class: 'muted small' }, [
          `Confidence: ${Math.round(c.confidence * 100)}%`,
        ]),
      ].filter(Boolean)),
    ]));
    // Category + similarity badge
    wrap.appendChild(el('div', { class: 'tax-rename-field',
                                  style: 'border-bottom:none;' }, [
      el('label', null, ['Diagnostic']),
      el('div', null, [
        el('span', {
          class: 'tax-rename-cat tax-rename-cat-' + c.category,
        }, [c.category.toUpperCase()]),
        ' ',
        el('span', { class: 'muted small' }, [
          `similarité ${Math.round(c.similarity * 100)}%`,
        ]),
        c.used_fallback ? el('span', {
          class: 'muted small', style: 'margin-left:6px;',
        }, ['(fallback utilisé)']) : null,
      ].filter(Boolean)),
    ]));
  }

  // Page strip + arrows: same UX as the Mappings viewer. Each thumb is
  // a tiny <img> the browser lazy-loads; clicking a thumb or an arrow
  // updates currentPage and re-renders the detail panel.
  function _renderPager(relPath, currentPage, totalPages) {
    const pager = el('div', { class: 'tax-rename-pager' });
    if (totalPages <= 1) {
      pager.appendChild(el('span', { class: 'muted small' },
                            ['1 page disponible']));
      return pager;
    }
    pager.appendChild(el('button', {
      class: 'btn-icon tax-rename-pager-arrow',
      title: 'Page précédente',
      onclick: () => goToPage(relPath, currentPage - 1, totalPages),
    }, ['◀']));
    for (let i = 1; i <= totalPages; i++) {
      const thumb = el('div', {
        class: 'tax-rename-thumb' + (i === currentPage ? ' active' : ''),
        title: 'Page ' + i,
        onclick: () => goToPage(relPath, i, totalPages),
      }, [el('img', {
        src: thumbnailURL(relPath, i),
        alt: 'pg' + i,
        loading: 'lazy',
      })]);
      pager.appendChild(thumb);
    }
    pager.appendChild(el('button', {
      class: 'btn-icon tax-rename-pager-arrow',
      title: 'Page suivante',
      onclick: () => goToPage(relPath, currentPage + 1, totalPages),
    }, ['▶']));
    pager.appendChild(el('span', {
      class: 'muted small tax-rename-pager-indicator',
    }, [`pg ${currentPage}/${totalPages}`]));
    return pager;
  }

  function goToPage(relPath, page, totalPages) {
    if (page < 1 || page > totalPages) return;
    state.viewerPageByPath.set(relPath, page);
    renderDetail();
  }

  // Compact LLM summary for the rename detail panel. Mirrors the
  // "Analyse LLM" card of the Mappings view but trimmed to what matters
  // for naming decisions: title / author / language / confidence / top
  // themes.
  function _renderLLMSummary(c, meta) {
    const v = (meta && meta.vision) || null;
    const children = [];
    // Title — use the audit's value as a stable source even before the
    // fetch lands, then swap to the full meta version if richer.
    children.push(el('div', { class: 'tax-rename-llm-title' },
                     [v ? (v.title || c.title || '(sans titre)') : (c.title || '…')]));
    const author = (v && v.author) || c.author;
    if (author) {
      children.push(el('div', { class: 'tax-rename-llm-author' }, [author]));
    }
    // Badges line
    const badges = el('div', { class: 'tax-rename-llm-badges' });
    if (v && v.language) {
      badges.appendChild(
        el('span', { class: 'badge' }, [String(v.language).toUpperCase()]));
    }
    const conf = (v && v.confidence != null)
      ? v.confidence : (c.confidence || 0);
    badges.appendChild(
      el('span', { class: 'badge' }, [`conf ${Math.round(conf * 100)}%`]));
    children.push(badges);
    // Top themes (compact, max 3)
    if (v && Array.isArray(v.themes) && v.themes.length) {
      children.push(el('div', { class: 'tax-rename-llm-themes-h' },
                        ['Thèmes détectés']));
      const list = el('ul', { class: 'tax-rename-llm-themes' });
      v.themes.slice(0, 3).forEach((t, idx) => {
        list.appendChild(el('li', null, [
          el('span', { class: idx === 0 ? 'dot-primary' : 'dot-secondary' },
                     [idx === 0 ? '●' : '○']),
          ' ',
          el('strong', null, [t.theme]),
          ' ',
          el('span', { class: 'muted small' },
                     [`(${Math.round((t.confidence || 0) * 100)}%)`]),
        ]));
      });
      children.push(list);
    } else if (meta && meta.error) {
      children.push(el('div', { class: 'muted small' },
                        ['✗ ' + meta.error]));
    } else if (!meta) {
      children.push(el('div', { class: 'muted small' }, ['Chargement…']));
    }
    return children;
  }

  // Char-level diff: each character in the longer string is colored
  // by whether it appears in the other (cheap proxy of insert/delete).
  function _renderDiffBlock(a, b) {
    const wrap = el('div', { class: 'tax-rename-diff' });
    const aSet = new Set(a.toLowerCase());
    const bSet = new Set(b.toLowerCase());
    const rowA = el('div', { class: 'tax-rename-diff-row' });
    for (const ch of a) {
      const cls = bSet.has(ch.toLowerCase()) ? 'kept' : 'removed';
      rowA.appendChild(el('span', { class: 'tax-rename-diff-ch ' + cls }, [ch]));
    }
    const rowB = el('div', { class: 'tax-rename-diff-row' });
    for (const ch of b) {
      const cls = aSet.has(ch.toLowerCase()) ? 'kept' : 'added';
      rowB.appendChild(el('span', { class: 'tax-rename-diff-ch ' + cls }, [ch]));
    }
    wrap.appendChild(el('div', { class: 'muted small' }, ['– actuel']));
    wrap.appendChild(rowA);
    wrap.appendChild(el('div', { class: 'muted small', style: 'margin-top:4px;' },
                       ['+ proposé']));
    wrap.appendChild(rowB);
    return wrap;
  }

  // ── Col 3 — preview + apply (PR2: apply is disabled) ────────────────

  function renderPreview() {
    const wrap = $('#tax-rename-preview');
    const sub = $('#tax-rename-preview-sub');
    wrap.innerHTML = '';
    const c = _candidate();
    if (!c) {
      sub.textContent = '';
      wrap.appendChild(el('div', { class: 'muted small',
                                    style: 'padding:30px;text-align:center;' },
        ['Sélectionne un candidat.']));
      return;
    }
    sub.textContent = '';
    // Editable proposed name
    wrap.appendChild(el('div', { class: 'tax-rename-field' }, [
      el('label', null, ['Nouveau nom']),
      el('input', {
        type: 'text', value: c.suggested_name, disabled: true,
        title: 'Édition disponible en PR3',
      }),
    ]));
    if (c.issues && c.issues.length) {
      wrap.appendChild(el('div', { class: 'tax-rename-field' }, [
        el('label', null, ['Notes du template']),
        el('div', { class: 'muted small' }, [c.issues.join('; ')]),
      ]));
    }
    wrap.appendChild(el('div', { class: 'tax-rename-field',
                                  style: 'border-bottom:none;' }, [
      el('label', null, ['Actions']),
      el('div', { class: 'muted small', style: 'margin-bottom:6px;' },
                 ['L\'application du renommage arrive en PR3.']),
      el('button', {
        class: 'btn-primary', disabled: true,
        title: 'Disponible en PR3',
      }, ['Renommer']),
      ' ',
      el('button', {
        class: 'btn-secondary', disabled: true,
        title: 'Disponible en PR4',
      }, ['Ajouter au batch']),
    ]));
    // Template config preview (read-only)
    if (state.data && state.data.config) {
      wrap.appendChild(el('div', { class: 'tax-rename-field',
                                    style: 'border-bottom:none;' }, [
        el('label', null, ['Template (profile.yaml)']),
        el('div', { class: 'tax-rename-template-display' },
                   [state.data.config.template]),
        el('div', { class: 'muted small', style: 'margin-top:2px;' },
                   ['fallback : ' + state.data.config.fallback]),
      ]));
    }
  }

  // ── Init ─────────────────────────────────────────────────────────────

  function renderAll() {
    renderStats();
    renderFilters();
    renderList();
    renderDetail();
    renderPreview();
  }

  async function loadAndRender() {
    await withBusy('Audit rename…', async () => {
      try {
        state.data = await fetchAudit();
        state.loaded = true;
        // Pre-select the first candidate so the detail panel is not empty
        if (state.data.candidates.length) {
          state.selectedRelPath = state.data.candidates[0].rel_path;
        }
        renderAll();
      } catch (e) {
        const wrap = $('#tax-rename-list');
        if (wrap) {
          wrap.innerHTML = '';
          wrap.appendChild(el('div', { class: 'error', style: 'padding:20px;' },
            ['✗ ' + e.message]));
        }
      }
    });
  }

  function init() {
    const sel = document.querySelector('#tax-profile-select');
    if (!sel) return;
    state.profile = sel.value;
    document.querySelectorAll('.tax-subtab').forEach(b => {
      b.addEventListener('click', () => activateSubtab(b.dataset.view));
    });
    sel.addEventListener('change', () => {
      state.profile = sel.value;
      state.data = null;
      state.selectedRelPath = null;
      state.loaded = false;
      const active = document.querySelector('.tax-subtab.active');
      if (active && active.dataset.view === 'rename') loadAndRender();
    });
    // Wire the info banner toggle (same pattern as the cat module)
    const infoBtn = document.querySelector('.tax-rename-info-toggle');
    const infoBody = document.querySelector('.tax-rename-info-body');
    if (infoBtn && infoBody) {
      infoBtn.addEventListener('click', () => {
        const open = infoBody.hidden;
        infoBody.hidden = !open;
        infoBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
        infoBtn.classList.toggle('open', open);
      });
    }
    // Cross-view jump: the Mappings LLM card can fire a `tax-rename-select`
    // event with {rel_path}. We make sure the audit is loaded, then pick
    // the candidate matching the path. If the file isn't in the current
    // filter set, broaden filters to ensure it shows up.
    document.addEventListener('tax-rename-select', async (e) => {
      const relPath = e.detail && e.detail.rel_path;
      if (!relPath) return;
      if (!state.loaded) {
        await loadAndRender();
      }
      const found = (state.data && state.data.candidates || [])
        .find(c => c.rel_path === relPath);
      if (found) {
        // Make sure its category is part of active filters
        state.activeCategories.add(found.category);
      }
      selectCandidate(relPath);
      // Auto-expand the list if the row was hidden by filters
      renderFilters();
      renderList();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
