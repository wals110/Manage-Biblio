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
    // Edited basename per file (only set if the user typed something
    // different from the audit suggestion). Cleared on rename success.
    editingNameByPath: new Map(),
    // True while a rename HTTP call is in flight — gates the button.
    renaming: false,
    // Free-text filter on the candidate list (case-insensitive substring
    // match on current_name). Combined with category filters in AND.
    searchQuery: '',
    // Renames done during this tab session — visual feedback distinct
    // from the journal (which is durable). Newest first. Persisted in
    // sessionStorage so an F5 keeps the list. Max 50 entries.
    sessionRenames: [],
    sessionExpanded: false,
    SESSION_MAX: 50,
    // Bulk selection: set of rel_paths currently checked in the list.
    // Cleared on profile change + on successful bulk apply.
    bulkSelected: new Set(),
    // Active tab of the 📜 modal: "records" or "batches"
    historyTab: 'records',
  };

  function _sessionKey() {
    return 'tax-rename-session::' + (state.profile || '');
  }

  function _loadSessionFromStorage() {
    try {
      const raw = sessionStorage.getItem(_sessionKey());
      if (!raw) { state.sessionRenames = []; return; }
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        state.sessionRenames = parsed.slice(0, state.SESSION_MAX);
      }
    } catch (_) { state.sessionRenames = []; }
  }

  function _saveSessionToStorage() {
    try {
      sessionStorage.setItem(
        _sessionKey(), JSON.stringify(state.sessionRenames));
    } catch (_) { /* quota / disabled — ignore */ }
  }

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

  async function postRenameFile(relPath, newName) {
    const r = await fetch('/api/rename/file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        profile: state.profile,
        rel_path: relPath,
        new_name: newName,
      }),
    });
    let body = null;
    try { body = await r.json(); } catch (_) { /* ignore */ }
    if (!r.ok) {
      const msg = (body && body.error) || `HTTP ${r.status}`;
      const err = new Error(msg);
      err.status = r.status;
      throw err;
    }
    return body;
  }

  async function fetchJournal(limit) {
    const url = `/api/rename/journal?profile=${encodeURIComponent(state.profile)}`
              + (limit ? `&limit=${limit}` : '');
    const r = await fetch(url);
    if (!r.ok) throw new Error('journal HTTP ' + r.status);
    return r.json();
  }

  async function postUndoRecord(record) {
    const r = await fetch('/api/rename/undo/record', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        profile: state.profile,
        ts: record.ts,
        old: record.old,
        new: record.new,
      }),
    });
    let body = null;
    try { body = await r.json(); } catch (_) { /* ignore */ }
    if (!r.ok) {
      const msg = (body && body.error) || `HTTP ${r.status}`;
      const err = new Error(msg);
      err.status = r.status;
      throw err;
    }
    return body;
  }

  async function postBulk(items) {
    const r = await fetch('/api/rename/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, items }),
    });
    let body = null;
    try { body = await r.json(); } catch (_) { /* ignore */ }
    if (!r.ok) {
      const msg = (body && body.error) || `HTTP ${r.status}`;
      const err = new Error(msg);
      err.status = r.status;
      throw err;
    }
    return body;
  }

  async function postUndoBatch(batchId) {
    const r = await fetch('/api/rename/undo/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, batch_id: batchId }),
    });
    let body = null;
    try { body = await r.json(); } catch (_) { /* ignore */ }
    if (!r.ok) {
      const msg = (body && body.error) || `HTTP ${r.status}`;
      const err = new Error(msg);
      err.status = r.status;
      throw err;
    }
    return body;
  }

  // Reuses the shared modal HTML (#tax-confirm-modal) declared in
  // taxonomy.html — same pattern as taxonomy_categories.js. We can't
  // share the JS helper because each sub-tab module lives in its own
  // IIFE, so we duplicate the 30-line wiring rather than expose a
  // global. Cheaper than a coupling abstraction for one button.
  function showConfirm({title, body, confirmLabel, cancelLabel, variant}) {
    return new Promise((resolve) => {
      const modal = $('#tax-confirm-modal');
      const okBtn = $('#tax-confirm-ok');
      const cancelBtn = $('#tax-confirm-cancel');
      if (!modal || !okBtn || !cancelBtn) {
        // Fall back to native confirm if the shared modal isn't on this page
        resolve(window.confirm(`${title || ''}\n\n${body || ''}`));
        return;
      }
      $('#tax-confirm-title').textContent = title || 'Confirmer ?';
      $('#tax-confirm-body').textContent = body || '';
      okBtn.textContent = confirmLabel || 'Confirmer';
      cancelBtn.textContent = cancelLabel || 'Annuler';
      okBtn.className = (variant === 'danger') ? 'btn-danger' : 'btn-primary';
      modal.style.display = 'flex';

      function cleanup(result) {
        modal.style.display = 'none';
        okBtn.onclick = null;
        cancelBtn.onclick = null;
        document.removeEventListener('keydown', onKey);
        resolve(result);
      }
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); cleanup(false); }
        if (e.key === 'Enter')  { e.preventDefault(); cleanup(true); }
      }
      okBtn.onclick = () => cleanup(true);
      cancelBtn.onclick = () => cleanup(false);
      document.addEventListener('keydown', onKey);
      setTimeout(() => okBtn.focus(), 50);
    });
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
    const lowConf = s.n_low_confidence || 0;
    out.innerHTML =
      `<span><strong>${s.n_total}</strong> fichiers</span>` +
      `<span class="dot"></span><span><strong>${s.n_with_title}</strong> avec titre LLM</span>` +
      `<span class="dot"></span><span class="orphan-badge"><strong>${s.n_placeholder}</strong> placeholders</span>` +
      `<span class="dot"></span><span><strong>${s.n_divergent}</strong> divergents</span>` +
      `<span class="dot"></span><span><strong>${s.n_minor_case}</strong> minor case</span>` +
      `<span class="dot"></span><span><strong>${s.n_ok}</strong> ok</span>` +
      (lowConf > 0
        ? `<span class="dot"></span><span class="muted small" title="Fichiers avec une entrée vision_cache mais une confidence < min_title_confidence — non audités, à re-scanner si besoin"><strong>${lowConf}</strong> low conf.</span>`
        : '');
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
    const q = (state.searchQuery || '').trim().toLowerCase();
    const candidates = state.data.candidates.filter(c => {
      if (!state.activeCategories.has(c.category)) return false;
      if (q && !c.current_name.toLowerCase().includes(q)) return false;
      return true;
    });
    const total = state.data.candidates.length;
    const suffix = q ? ` · recherche "${state.searchQuery}"` : '';
    sub.textContent = `${candidates.length} affichés / ${total}${suffix}`;
    if (candidates.length === 0) {
      const emptyMsg = q
        ? `Aucun candidat ne matche "${state.searchQuery}".`
        : 'Aucun candidat avec les filtres actifs.';
      wrap.appendChild(el('div', { class: 'muted small',
                                    style: 'padding:14px;text-align:center;' },
        [emptyMsg]));
      return;
    }
    const MAX = 500;
    for (const c of candidates.slice(0, MAX)) {
      const selected = state.selectedRelPath === c.rel_path;
      const checked = state.bulkSelected.has(c.rel_path);
      const sim = Math.round(c.similarity * 100);
      const checkbox = el('input', {
        type: 'checkbox',
        class: 'tax-rename-row-check',
        title: 'Inclure dans le batch de renommage',
        checked: checked || undefined,
      });
      // Stop propagation so toggling the checkbox doesn't also select
      // the row for detail-view. The click handler ON the row stays
      // for everywhere except the checkbox itself.
      checkbox.addEventListener('click', (e) => e.stopPropagation());
      checkbox.addEventListener('change', () => {
        _toggleBulk(c.rel_path, checkbox.checked);
      });
      wrap.appendChild(el('div', {
        class: 'tax-rename-row'
             + (selected ? ' selected' : '')
             + (checked ? ' bulk-selected' : ''),
        title: c.rel_path,
        onclick: () => selectCandidate(c.rel_path),
      }, [
        checkbox,
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

    // Editable proposed name. Default = suggested_name from the audit,
    // overridable by the user via the input. We persist their edits per
    // file so switching candidates and coming back keeps the value.
    const initialName = state.editingNameByPath.get(c.rel_path)
                        || c.suggested_name;
    const nameInput = el('input', {
      type: 'text', value: initialName,
      title: 'Renommer le fichier (basename uniquement, pas de chemin)',
    });
    nameInput.addEventListener('input', () => {
      state.editingNameByPath.set(c.rel_path, nameInput.value);
      _refreshRenameButtonState(c, nameInput);
    });
    nameInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        applyRename(c.rel_path);
      }
    });
    wrap.appendChild(el('div', { class: 'tax-rename-field' }, [
      el('label', null, ['Nouveau nom']),
      nameInput,
    ]));
    if (c.issues && c.issues.length) {
      wrap.appendChild(el('div', { class: 'tax-rename-field' }, [
        el('label', null, ['Notes du template']),
        el('div', { class: 'muted small' }, [c.issues.join('; ')]),
      ]));
    }

    const renameBtn = el('button', {
      class: 'btn-primary',
      'data-tax-rename-apply': '1',
      onclick: () => applyRename(c.rel_path),
    }, ['Renommer']);
    wrap.appendChild(el('div', { class: 'tax-rename-field',
                                  style: 'border-bottom:none;' }, [
      el('label', null, ['Actions']),
      el('div', { class: 'muted small', style: 'margin-bottom:6px;' },
                 ['Le rename est appliqué sur disque et journalisé pour undo.']),
      renameBtn,
      ' ',
      el('button', {
        class: 'btn-secondary', disabled: true,
        title: 'Disponible en PR4',
      }, ['Ajouter au batch']),
    ]));
    _refreshRenameButtonState(c, nameInput);
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

  // ── Rename application ───────────────────────────────────────────────

  // Reflect whether the current input value is a usable, distinct
  // basename. Same checks the backend will run (basename only, no
  // path separators, non-empty), so the user sees the button gray
  // out instantly on invalid input.
  function _refreshRenameButtonState(c, nameInput) {
    const btn = document.querySelector('[data-tax-rename-apply="1"]');
    if (!btn) return;
    const raw = (nameInput.value || '').trim();
    const invalidChars = /[\\/:*?"<>|\x00]/.test(raw);
    const isPlaceholder = !raw || raw === '.' || raw === '..';
    const sameAsCurrent = raw === c.current_name;
    const disabled = state.renaming || isPlaceholder || invalidChars || sameAsCurrent;
    btn.disabled = disabled;
    if (state.renaming) {
      btn.title = 'Renommage en cours…';
    } else if (isPlaceholder) {
      btn.title = 'Saisis un nouveau nom';
    } else if (invalidChars) {
      btn.title = 'Caractères interdits dans un nom de fichier : / \\ : * ? " < > |';
    } else if (sameAsCurrent) {
      btn.title = 'Identique au nom actuel — rien à renommer';
    } else {
      btn.title = `Renommer vers : ${raw}`;
    }
  }

  async function applyRename(relPath) {
    if (state.renaming) return;
    const c = state.data && state.data.candidates.find(
      x => x.rel_path === relPath);
    if (!c) return;
    const newName = (state.editingNameByPath.get(relPath)
                     || c.suggested_name || '').trim();
    if (!newName || newName === c.current_name) return;

    const ok = await showConfirm({
      title: 'Renommer ce fichier ?',
      body: `${c.current_name}  →  ${newName}\n\nLe rename est journalisé sur disque. Tu peux l'annuler depuis 📜 Renommages ou directement depuis la liste de session en bas à gauche.`,
      confirmLabel: 'Renommer',
      cancelLabel: 'Annuler',
      variant: 'primary',
    });
    if (!ok) return;

    state.renaming = true;
    _refreshButtonsDuringRename(true);
    await withBusy('Renommage…', async () => {
      try {
        const result = await postRenameFile(relPath, newName);
        // Clear the per-file edit since we just committed it
        state.editingNameByPath.delete(relPath);
        // Record in the session journal BEFORE refreshing the audit so
        // the UI shows the entry immediately even if fetchAudit is slow.
        const newRel = result.new_rel_path || relPath;
        const journalEntry = result.journal_entry || null;
        _pushSessionRename({
          old_name: c.current_name,
          new_name: newName,
          old_rel_path: relPath,
          new_rel_path: newRel,
          ts: Date.now(),
          // Keep the durable journal entry so the inline ↶ button can
          // POST it back to /api/rename/undo/record. Without these
          // fields the undo can't work (server matches on ts+old+new).
          journal: journalEntry ? {
            ts: journalEntry.ts,
            old: journalEntry.old,
            new: journalEntry.new,
            batch: journalEntry.batch || '',
          } : null,
          undone: false,
        });
        // Refresh the audit so the renamed file shows up under its new name
        state.data = await fetchAudit(true);
        // Try to re-select the renamed file under its new path
        const found = (state.data.candidates || []).find(
          x => x.rel_path === newRel);
        if (found) {
          state.selectedRelPath = newRel;
          state.activeCategories.add(found.category);
        } else {
          // The renamed file might now fall into "ok" (correctly named)
          // and be hidden behind a filter — surface it anyway.
          state.selectedRelPath = newRel;
        }
        renderAll();
        _refreshHistoryCount();
        showToast(`✓ Renommé : ${newName}`, 'success');
      } catch (e) {
        const detail = e.status ? `(${e.status}) ${e.message}` : e.message;
        showToast(`✗ Échec du renommage : ${detail}`, 'error');
      } finally {
        state.renaming = false;
        _refreshButtonsDuringRename(false);
      }
    });
  }

  function _refreshButtonsDuringRename(active) {
    const btn = document.querySelector('[data-tax-rename-apply="1"]');
    if (btn) {
      btn.disabled = active;
      if (active) btn.title = 'Renommage en cours…';
    }
  }

  // ── Session log ──────────────────────────────────────────────────────

  function _pushSessionRename(entry) {
    // Newest first; cap at SESSION_MAX. If the same file is renamed
    // twice, keep both entries (audit trail of the session).
    state.sessionRenames.unshift(entry);
    if (state.sessionRenames.length > state.SESSION_MAX) {
      state.sessionRenames.length = state.SESSION_MAX;
    }
    _saveSessionToStorage();
    renderSession();
  }

  function _formatTime(ts) {
    const d = new Date(ts);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function renderSession() {
    const body = $('#tax-rename-session-body');
    const count = $('#tax-rename-session-count');
    const clearBtn = $('#tax-rename-session-clear');
    if (!body || !count) return;
    const n = state.sessionRenames.length;
    count.textContent = String(n);
    if (clearBtn) clearBtn.hidden = n === 0;
    body.innerHTML = '';
    if (n === 0) {
      body.appendChild(el('div', { class: 'tax-rename-session-empty' },
                            ['Aucun renommage dans cette session.']));
      return;
    }
    state.sessionRenames.forEach((entry, idx) => {
      const row = el('div', {
        class: 'tax-rename-session-row',
        title: 'Cliquer pour aller au fichier (nouveau nom)',
        onclick: () => _jumpToSessionEntry(entry),
      }, [
        el('div', { class: 'tax-rename-session-row-names' }, [
          el('span', { class: 'tax-rename-session-row-old' }, [entry.old_name]),
          el('span', { class: 'tax-rename-session-row-arrow' }, ['→']),
          el('span', { class: 'tax-rename-session-row-new' }, [entry.new_name]),
        ]),
        el('div', { class: 'tax-rename-session-row-ts' },
                   [_formatTime(entry.ts)]),
      ]);
      // Inline undo button (visible on row hover). Only enabled when
      // the durable journal record is available — older session entries
      // (saved before PR4A) won't have it, so we just hide the button.
      if (entry.journal) {
        const undoBtn = el('button', {
          type: 'button',
          class: 'tax-rename-session-row-undo'
                 + (entry.undone ? ' is-undone' : ''),
          title: entry.undone
            ? 'Déjà annulé'
            : 'Annuler ce renommage (remet l\'ancien nom)',
          disabled: entry.undone,
          onclick: (e) => {
            e.stopPropagation();
            if (!entry.undone) _undoSessionEntry(idx);
          },
        }, [entry.undone ? '✓ annulé' : '↶ annuler']);
        row.appendChild(undoBtn);
      }
      body.appendChild(row);
    });
  }

  async function _undoSessionEntry(idx) {
    const entry = state.sessionRenames[idx];
    if (!entry || !entry.journal || entry.undone) return;
    const ok = await showConfirm({
      title: 'Annuler ce renommage ?',
      body: `Le fichier va revenir à : ${entry.old_name}\n\nCette opération est elle-même journalisée (audit trail complet).`,
      confirmLabel: 'Annuler le renommage',
      cancelLabel: 'Garder',
      variant: 'danger',
    });
    if (!ok) return;
    await withBusy('Annulation…', async () => {
      try {
        await postUndoRecord(entry.journal);
        // Mark the session entry as undone (keep it in the list for
        // visibility, but disable the button).
        entry.undone = true;
        _saveSessionToStorage();
        // Refresh the audit so the restored file shows up under its
        // original name. Try to select it.
        state.data = await fetchAudit(true);
        const found = (state.data.candidates || []).find(
          c => c.rel_path === entry.old_rel_path);
        if (found) {
          state.activeCategories.add(found.category);
          state.selectedRelPath = entry.old_rel_path;
        }
        renderAll();
        _refreshHistoryCount();
        showToast(`↶ Renommage annulé : ${entry.old_name}`, 'success');
      } catch (e) {
        const detail = e.status ? `(${e.status}) ${e.message}` : e.message;
        showToast(`✗ Échec de l'annulation : ${detail}`, 'error');
      }
    });
  }

  async function _jumpToSessionEntry(entry) {
    if (!state.loaded) await loadAndRender();
    const target = entry.new_rel_path;
    const found = (state.data && state.data.candidates || []).find(
      c => c.rel_path === target);
    if (found) {
      state.activeCategories.add(found.category);
      selectCandidate(target);
      renderFilters();
      renderList();
    } else {
      showToast(
        `Fichier hors audit (probablement OK et filtré) : ${entry.new_name}`,
        'info');
    }
  }

  function _toggleSessionPanel(expand) {
    const toggle = $('#tax-rename-session-toggle');
    const body = $('#tax-rename-session-body');
    if (!toggle || !body) return;
    const willExpand = expand != null ? !!expand : body.hidden;
    body.hidden = !willExpand;
    toggle.setAttribute('aria-expanded', willExpand ? 'true' : 'false');
    state.sessionExpanded = willExpand;
  }

  // ── Durable journal modal (📜 Renommages) ────────────────────────────

  async function _refreshHistoryCount() {
    try {
      const data = await fetchJournal(200);
      const btnCount = $('#tax-rename-history-count');
      if (btnCount) btnCount.textContent = String(data.n_active || 0);
    } catch (_) { /* silent — the count just won't update */ }
  }

  async function openHistoryModal() {
    const modal = $('#tax-rename-history-modal');
    const list = $('#tax-rename-history-list');
    const sub = $('#tax-rename-history-modal-sub');
    if (!modal || !list) return;
    modal.style.display = 'flex';
    list.innerHTML = '';
    if (sub) sub.textContent = 'Chargement…';
    list.appendChild(el('div', { class: 'tax-rename-history-empty' },
                          ['Chargement du journal…']));
    try {
      const data = await fetchJournal(200);
      state._historyData = data;
      _renderHistoryActiveTab();
    } catch (e) {
      list.innerHTML = '';
      list.appendChild(el('div', { class: 'tax-rename-history-empty error' },
                            ['✗ ' + e.message]));
    }
  }

  function _renderHistoryActiveTab() {
    const list = $('#tax-rename-history-list');
    const batches = $('#tax-rename-history-batches');
    if (!list || !batches) return;
    const data = state._historyData;
    if (!data) return;
    if (state.historyTab === 'batches') {
      list.hidden = true;
      batches.hidden = false;
      _renderBatches(data);
    } else {
      list.hidden = false;
      batches.hidden = true;
      _renderHistoryList(data);
    }
  }

  function _renderBatches(data) {
    const wrap = $('#tax-rename-history-batches');
    if (!wrap) return;
    wrap.innerHTML = '';
    const batches = (data && data.batches) || [];
    if (batches.length === 0) {
      wrap.appendChild(el('div', { class: 'tax-rename-history-empty' },
                            ['Aucun batch de renommage pour l\'instant — utilise la barre "sélectionnés" en col 1 pour en créer un.']));
      return;
    }
    for (const b of batches) {
      // Compute if all records of this batch are undone (each record's
      // is_undone flag is in data.records).
      const recordsOfBatch = (data.records || []).filter(
        r => r.batch === b.batch && !r.is_undo);
      const nUndone = recordsOfBatch.filter(r => r.is_undone).length;
      const nTotal = recordsOfBatch.length || b.n_renames || 0;
      const allUndone = nTotal > 0 && nUndone === nTotal;
      wrap.appendChild(el('div', { class: 'tax-rename-history-batch-row' }, [
        el('div', { class: 'tax-rename-history-batch-meta' }, [
          el('div', null, [
            el('strong', null, [b.ts.replace('T', ' ')]),
            ' · ',
            el('span', { class: 'tax-rename-history-batch-count' },
                       [`${nTotal} renommage(s)`]),
            nUndone > 0
              ? el('span', { class: 'muted small',
                              style: 'margin-left:6px;color:#ff7b72;' },
                            [`(${nUndone} déjà annulé(s))`])
              : null,
          ].filter(Boolean)),
          el('div', { class: 'tax-rename-history-batch-id' },
                     ['batch ' + b.batch]),
        ]),
        el('span'),
        allUndone
          ? el('span', { class: 'tax-rename-history-meta' },
                       ['déjà annulé'])
          : el('button', {
              type: 'button',
              class: 'tax-rename-history-undo-btn',
              title: 'Annuler tout le batch en une opération',
              onclick: () => _undoBatchFromHistory(b, nTotal - nUndone),
            }, [`↶ Annuler le lot (${nTotal - nUndone})`]),
      ]));
    }
  }

  async function _undoBatchFromHistory(batch, nActive) {
    const ok = await showConfirm({
      title: 'Annuler tout le batch ?',
      body: `${nActive} renommage(s) vont être inversés en une opération (ordre inverse).\n\nbatch_id : ${batch.batch}\nDate : ${batch.ts.replace('T', ' ')}`,
      confirmLabel: 'Annuler le batch',
      cancelLabel: 'Garder',
      variant: 'danger',
    });
    if (!ok) return;
    await withBusy(`Annulation du batch…`, async () => {
      try {
        const summary = await postUndoBatch(batch.batch);
        // Mark every session entry of this batch as undone
        for (const entry of state.sessionRenames) {
          if (entry.batch_id === batch.batch
              || (entry.journal && entry.journal.batch === batch.batch)) {
            entry.undone = true;
          }
        }
        _saveSessionToStorage();
        state.data = await fetchAudit(true);
        renderAll();
        const data = await fetchJournal(200);
        state._historyData = data;
        _renderHistoryActiveTab();
        _refreshHistoryCount();
        if (summary.n_errors > 0) {
          showToast(
            `${summary.n_undone} annulé(s), ${summary.n_errors} en erreur (voir console)`,
            'info');
          console.warn('Undo batch errors:', summary.errors);
        } else {
          showToast(`↶ Batch annulé : ${summary.n_undone} fichier(s)`, 'success');
        }
      } catch (e) {
        const detail = e.status ? `(${e.status}) ${e.message}` : e.message;
        showToast(`✗ Échec : ${detail}`, 'error');
      }
    });
  }

  function _renderHistoryList(data) {
    const list = $('#tax-rename-history-list');
    const sub = $('#tax-rename-history-modal-sub');
    const btnCount = $('#tax-rename-history-count');
    if (!list) return;
    list.innerHTML = '';
    const records = (data && data.records) || [];
    if (sub) {
      sub.textContent = `${data.n_active} actif(s) / ${data.n_total} entrée(s) — ` +
                        `les annulations sont elles-mêmes journalisées`;
    }
    if (btnCount) btnCount.textContent = String(data.n_active || 0);
    if (records.length === 0) {
      list.appendChild(el('div', { class: 'tax-rename-history-empty' },
                            ['Aucun renommage dans ce profil pour l\'instant.']));
      return;
    }
    for (const r of records) {
      const klass = 'tax-rename-history-row'
                  + (r.is_undo ? ' is-undo' : '')
                  + (r.is_undone ? ' is-undone' : '');
      const row = el('div', { class: klass }, [
        el('div', { class: 'tax-rename-history-names', title: r.new_rel }, [
          el('span', { class: 'tax-rename-history-old' }, [r.old_rel]),
          el('span', { class: 'tax-rename-history-new' }, ['→ ' + r.new_rel]),
        ]),
        el('div', { class: 'tax-rename-history-meta' }, [
          r.is_undo ? '↶ undo · ' : '',
          r.ts.replace('T', ' '),
        ]),
        _buildHistoryUndoButton(r),
      ]);
      list.appendChild(row);
    }
  }

  function _buildHistoryUndoButton(r) {
    // No undo button on "undo" records (they're inverse ops, undoing
    // them would re-apply the original rename — too confusing for an
    // MVP; the user can re-apply manually if needed).
    if (r.is_undo) {
      return el('span', { class: 'tax-rename-history-meta' }, ['—']);
    }
    if (r.is_undone) {
      return el('span', { class: 'tax-rename-history-meta' }, ['déjà annulé']);
    }
    return el('button', {
      type: 'button',
      class: 'tax-rename-history-undo-btn',
      title: 'Annuler ce renommage (remet l\'ancien nom)',
      onclick: () => _undoFromHistory(r),
    }, ['↶ Annuler']);
  }

  async function _undoFromHistory(record) {
    const ok = await showConfirm({
      title: 'Annuler ce renommage ?',
      body: `${record.new_rel} → ${record.old_rel}\n\n` +
            `L'opération inverse est elle-même journalisée.`,
      confirmLabel: 'Annuler le renommage',
      cancelLabel: 'Garder',
      variant: 'danger',
    });
    if (!ok) return;
    const list = $('#tax-rename-history-list');
    await withBusy('Annulation…', async () => {
      try {
        await postUndoRecord({
          ts: record.ts, old: record.old, new: record.new,
        });
        // Mark matching session entry undone (if present)
        for (const entry of state.sessionRenames) {
          if (entry.journal && entry.journal.ts === record.ts
              && entry.journal.new === record.new) {
            entry.undone = true;
          }
        }
        _saveSessionToStorage();
        // Refresh both the audit and the modal list
        state.data = await fetchAudit(true);
        renderAll();
        const data = await fetchJournal(200);
        state._historyData = data;
        _renderHistoryActiveTab();
        _refreshHistoryCount();
        showToast(`↶ Renommage annulé`, 'success');
      } catch (e) {
        const detail = e.status ? `(${e.status}) ${e.message}` : e.message;
        showToast(`✗ Échec : ${detail}`, 'error');
        if (list) {
          // Reload the list so the user sees the current state of the
          // entry (might have been undone by another route, etc.).
          try {
            const data = await fetchJournal(200);
            state._historyData = data;
            _renderHistoryActiveTab();
          } catch (_) { /* ignore */ }
        }
      }
    });
  }

  function closeHistoryModal() {
    const modal = $('#tax-rename-history-modal');
    if (modal) modal.style.display = 'none';
  }

  // ── Init ─────────────────────────────────────────────────────────────

  function renderAll() {
    renderStats();
    renderFilters();
    renderList();
    renderDetail();
    renderPreview();
    renderSession();
    renderBulkbar();
  }

  // ── Bulk selection (multi-rename) ────────────────────────────────────

  function _toggleBulk(relPath, checked) {
    if (checked) state.bulkSelected.add(relPath);
    else state.bulkSelected.delete(relPath);
    renderList();
    renderBulkbar();
  }

  function _clearBulk() {
    if (state.bulkSelected.size === 0) return;
    state.bulkSelected.clear();
    renderList();
    renderBulkbar();
  }

  function renderBulkbar() {
    const bar = $('#tax-rename-bulkbar');
    const n = $('#tax-rename-bulkbar-n');
    if (!bar || !n) return;
    const count = state.bulkSelected.size;
    n.textContent = String(count);
    bar.hidden = count === 0;
  }

  async function _applyBulkSuggestion() {
    if (state.bulkSelected.size === 0 || !state.data) return;
    // Build items list from current candidates (their suggested name).
    // A file might have been edited in col 3 — for bulk we use the
    // template suggestion to stay predictable. If the user edited one
    // file's name, they should rename it solo before bulking the rest.
    const byPath = new Map(state.data.candidates.map(c => [c.rel_path, c]));
    const items = [];
    const skipped = [];
    for (const relPath of state.bulkSelected) {
      const c = byPath.get(relPath);
      if (!c) { skipped.push(relPath); continue; }
      // Skip files where current_name == suggested_name (no-op)
      if (c.current_name === c.suggested_name) { skipped.push(relPath); continue; }
      items.push({ rel_path: relPath, new_name: c.suggested_name });
    }
    if (items.length === 0) {
      showToast('Rien à renommer (les fichiers sélectionnés sont déjà à leur nom suggéré).',
                'info');
      return;
    }

    // Preview: list the first 5 for the confirm modal
    const preview = items.slice(0, 5).map(i => `• ${i.new_name}`).join('\n');
    const extra = items.length > 5 ? `\n…et ${items.length - 5} autres` : '';
    const ok = await showConfirm({
      title: `Renommer ${items.length} fichier(s) ?`,
      body: `Application de la suggestion du template pour chaque fichier sélectionné. Tout est tagué avec un même batch_id — annulable d'un clic depuis 📜 Renommages.\n\n${preview}${extra}`,
      confirmLabel: `Renommer ${items.length} fichier(s)`,
      cancelLabel: 'Annuler',
      variant: 'primary',
    });
    if (!ok) return;

    await withBusy(`Renommage de ${items.length} fichier(s)…`, async () => {
      try {
        const result = await postBulk(items);
        // Push session entries for each success so the user sees them
        // immediately and can undo individually too.
        for (const s of (result.successes || [])) {
          const c = byPath.get(s.rel_path);
          _pushSessionRename({
            old_name: c ? c.current_name : s.rel_path,
            new_name: s.new_name,
            old_rel_path: s.rel_path,
            new_rel_path: s.new_rel_path,
            ts: Date.now(),
            journal: s.journal_entry ? {
              ts: s.journal_entry.ts,
              old: s.journal_entry.old,
              new: s.journal_entry.new,
              batch: s.journal_entry.batch || result.batch_id,
            } : null,
            batch_id: result.batch_id,
            undone: false,
          });
        }
        state.bulkSelected.clear();
        state.data = await fetchAudit(true);
        renderAll();
        _refreshHistoryCount();
        if (result.n_errors > 0) {
          showToast(
            `✓ ${result.n_renamed} renommés, ${result.n_errors} en erreur. Voir la console pour le détail.`,
            'info');
          console.warn('Bulk rename errors:', result.errors);
        } else {
          showToast(
            `✓ ${result.n_renamed} fichier(s) renommé(s) (batch ${result.batch_id.slice(0, 8)}…)`,
            'success');
        }
      } catch (e) {
        const detail = e.status ? `(${e.status}) ${e.message}` : e.message;
        showToast(`✗ Échec du batch : ${detail}`, 'error');
      }
    });
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
    _loadSessionFromStorage();
    document.querySelectorAll('.tax-subtab').forEach(b => {
      b.addEventListener('click', () => activateSubtab(b.dataset.view));
    });
    sel.addEventListener('change', () => {
      state.profile = sel.value;
      state.data = null;
      state.selectedRelPath = null;
      state.loaded = false;
      state.editingNameByPath.clear();
      state.renaming = false;
      state.searchQuery = '';
      state.bulkSelected.clear();
      // Session log is keyed per profile so each profile has its own
      // history. Load the new profile's log; the previous one stays in
      // sessionStorage untouched.
      _loadSessionFromStorage();
      const searchInput = $('#tax-rename-search');
      if (searchInput) searchInput.value = '';
      const clearBtn = $('#tax-rename-search-clear');
      if (clearBtn) clearBtn.hidden = true;
      const active = document.querySelector('.tax-subtab.active');
      if (active && active.dataset.view === 'rename') loadAndRender();
      else renderSession();
      _refreshHistoryCount();
    });

    // ── Search bar: debounced filter on candidate names ──
    const searchInput = $('#tax-rename-search');
    const searchClear = $('#tax-rename-search-clear');
    if (searchInput) {
      let debounceId = null;
      searchInput.addEventListener('input', () => {
        if (searchClear) searchClear.hidden = !searchInput.value;
        clearTimeout(debounceId);
        debounceId = setTimeout(() => {
          state.searchQuery = searchInput.value;
          renderList();
        }, 150);
      });
      // Esc clears the search field
      searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && searchInput.value) {
          searchInput.value = '';
          state.searchQuery = '';
          if (searchClear) searchClear.hidden = true;
          renderList();
          e.stopPropagation();
        }
      });
    }
    if (searchClear) {
      searchClear.addEventListener('click', () => {
        if (!searchInput) return;
        searchInput.value = '';
        state.searchQuery = '';
        searchClear.hidden = true;
        renderList();
        searchInput.focus();
      });
    }

    // ── Session log: collapse toggle + clear ──
    const sessionToggle = $('#tax-rename-session-toggle');
    if (sessionToggle) {
      sessionToggle.addEventListener('click', () => _toggleSessionPanel());
    }
    const sessionClear = $('#tax-rename-session-clear');
    // Initial render of the session panel so the count/list reflect
    // sessionStorage from the start (even before the user clicks Rename).
    renderSession();

    // ── Bulk action bar ──
    const bulkApplyBtn = $('#tax-rename-bulkbar-apply');
    if (bulkApplyBtn) {
      bulkApplyBtn.addEventListener('click', () => _applyBulkSuggestion());
    }
    const bulkClearBtn = $('#tax-rename-bulkbar-clear');
    if (bulkClearBtn) {
      bulkClearBtn.addEventListener('click', () => _clearBulk());
    }

    // ── 📜 Renommages modal: open / close / refresh count + tabs ──
    const historyBtn = $('#tax-rename-history-btn');
    if (historyBtn) {
      historyBtn.addEventListener('click', () => openHistoryModal());
    }
    const historyClose = $('#tax-rename-history-close');
    if (historyClose) {
      historyClose.addEventListener('click', () => closeHistoryModal());
    }
    document.querySelectorAll('.tax-rename-history-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        if (!tab || tab === state.historyTab) return;
        state.historyTab = tab;
        document.querySelectorAll('.tax-rename-history-tab').forEach(b => {
          const active = b.dataset.tab === tab;
          b.classList.toggle('active', active);
          b.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        _renderHistoryActiveTab();
      });
    });
    const historyModal = $('#tax-rename-history-modal');
    if (historyModal) {
      // Click on backdrop closes; Esc too
      historyModal.addEventListener('click', (e) => {
        if (e.target === historyModal) closeHistoryModal();
      });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape'
            && historyModal.style.display === 'flex') {
          closeHistoryModal();
        }
      });
    }
    // Fetch the journal count in the background so the 📜 button shows
    // the right number on first load. Cheap: GET-only, capped at 200.
    fetchJournal(200)
      .then(data => {
        const btnCount = $('#tax-rename-history-count');
        if (btnCount) btnCount.textContent = String(data.n_active || 0);
      })
      .catch(() => { /* silent — the modal will retry on open */ });
    if (sessionClear) {
      sessionClear.addEventListener('click', async () => {
        if (state.sessionRenames.length === 0) return;
        const ok = await showConfirm({
          title: 'Vider le journal de session ?',
          body: `${state.sessionRenames.length} entrée(s) vont disparaître de cette vue. Les renommages eux-mêmes restent en place dans le journal durable (undo possible plus tard).`,
          confirmLabel: 'Vider',
          cancelLabel: 'Garder',
          variant: 'danger',
        });
        if (!ok) return;
        state.sessionRenames = [];
        _saveSessionToStorage();
        renderSession();
      });
    }
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
