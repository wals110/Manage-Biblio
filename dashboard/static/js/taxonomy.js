/* Taxonomy tab — Phase 1 redesigned.
   Layout:
     [Arbre] [Viewer + LLM Card + Treemap (single-level)] [Themes mapped + LLM universe]
   Selection rules:
     - folder selected   → treemap shows direct children of THAT folder
     - file selected     → treemap shows direct children of file's GRANDPARENT
                           (= siblings of the folder containing the file)
                           Viewer + LLM card become visible.
*/

(function () {
  'use strict';

  const SECTION_COLORS = {
    '01-SCIENCES':   '#1f6feb',
    '02-INFORMATIQUE': '#8957e5',
    '03-INGENIERIE': '#f78166',
    '04-SHS':        '#3fb950',
    '05-RELIGIONS':  '#d29922',
    '06-MEDECINE':   '#f85149',
    '07-LANGUES':    '#56d4dd',
    '07-VIE':        '#56d4dd',
    '08-LOISIRS':    '#db61a2',
    '09-BUSINESS':   '#a371f7',
    '_A-TRIER':      '#6e7681',
  };
  const FILE_PAGE = 50;       // tree file pagination
  const MAX_PAGES = 5;        // viewer multipage cap (existing thumbnail infra limit)

  const state = {
    profile: null,
    snapshot: null,

    // Selection: type='folder'|'file'|null, path = relative path within target.
    selection: { type: null, path: '' },

    expanded: new Set(),
    filesByPath: new Map(),    // cache key = "path#offset"

    // Viewer
    viewerMeta: null,
    viewerCurrentPage: 1,
    viewerOpenedPages: new Set(),  // pages user has actually generated/loaded

    // LLM panel
    search: '',
    orphOnly: false,

    // Popover for "+ Mapper"
    popover: { open: false, theme: null, anchorEl: null },

    // Treemap scale mode — 'sqrt' = lissé (defaut, mieux pour navigation),
    // 'linear' = proportion réelle.
    treemapScale: 'sqrt',

    // Tree search — when non-empty, the tree is filtered to nodes whose
    // path contains the query (case-insensitive). Parent ancestors of
    // matching nodes are kept visible to preserve hierarchy.
    treeSearch: '',

    // Drag-drop of folders. Holds the path being dragged so dragover can
    // decide whether to allow the drop (cycle prevention).
    draggingFolderPath: null,

    // Tree bulk selection for file delete. Set of rel_paths checked
    // in the tree's hover-revealed checkboxes. Cleared on profile
    // change and on a successful bulk delete.
    treeBulkSelected: new Set(),

    // Mapped panel — when an item is clicked, fetches the list of files
    // concerned (future = vision_cache theme, current = files in target folder).
    // selectedMappedTheme stores the lowercased theme key for highlight.
    // mappedFilesByTheme caches the fetched payload per theme.
    selectedMappedTheme: null,
    mappedFilesByTheme: new Map(),
    mappedFilesTab: 'future',  // 'future' | 'current'

    // Mapped panel — multi-sélection pour bulk delete. Scopée au folder
    // courant : changement de folder = clear automatique (cf. helper
    // bulkSelectionForPath). Permet de cocher plusieurs thèmes et de
    // les supprimer en un seul appel (POST /mappings/bulk-delete).
    bulkMappedSelection: { folderPath: null, keysLower: new Set() },

    // Panneau LLM (colonne Thèmes LLM) — multi-sélection. Calque le patron
    // bulkMappedSelection mais SANS scope folder : la sélection porte sur
    // l'univers global des thèmes LLM, pas sur un dossier. Clés = thème en
    // minuscule. Reset au re-render complet du snapshot (renderAll). Sert
    // de socle aux actions bulk Mapper→dossier / Suggérer (Tâches 7-8).
    bulkLLMSelection: new Set(),

    // Panneau de revue « Suggérer + Mapper » (Tâche 8). Liste éditable des
    // suggestions renvoyées par l'endpoint suggest. Chaque item :
    //   { theme, folder, confidence, source, reason, accepted }
    // L'user peut cocher/décocher (accepted), corriger le folder inline, et
    // relancer une passe LLM sur les seuls non-résolus. Lecture seule jusqu'au
    // clic « Appliquer » qui POST en bulk-add. null = panneau fermé.
    suggestReview: null,

    // Cache du breakdown 3-way (stable / incoming / outgoing) par path.
    // Permet à l'UI d'afficher ce qui va RESTER, ARRIVER, PARTIR au prochain
    // reclassify sans devoir lancer un dryrun. Lazily fetché à l'ouverture
    // du panel Routage. Invalidé par renderAll() après une mutation.
    folderBreakdown: new Map(),
  };

  // Touched folders / themes — derived from snapshot.stats (which diffs
  // current mapping vs oldest backup). Cleared automatically when all
  // backups are gone (undone to baseline). Survives refresh.
  function touchedFoldersSet() {
    return new Set((state.snapshot && state.snapshot.stats.touched_folders) || []);
  }
  function touchedThemesSet() {
    return new Set((state.snapshot && state.snapshot.stats.touched_themes) || []);
  }
  // Path ancestors of touched folders — for "on-path" visual hint.
  // Example: touched={'01-SCIENCES/MATHEMATIQUES/02-Analyse'}
  // → ancestors={'01-SCIENCES', '01-SCIENCES/MATHEMATIQUES'}
  function touchedAncestorsSet(touched) {
    const out = new Set();
    for (const p of touched) {
      const parts = p.split('/');
      for (let i = 1; i < parts.length; i++) {
        out.add(parts.slice(0, i).join('/'));
      }
    }
    return out;
  }

  // ── DOM helpers ──────────────────────────────────────────────────────

  function $(sel) { return document.querySelector(sel); }
  function el(tag, props, children) {
    const e = document.createElement(tag);
    if (props) for (const k in props) {
      if (k === 'class') e.className = props[k];
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), props[k]);
      else e.setAttribute(k, props[k]);
    }
    if (children) for (const c of children) {
      if (c == null) continue;
      e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return e;
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, ch =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }
  function sectionOf(path) { return path ? path.split('/')[0] : ''; }
  function dirname(p) { const i = p.lastIndexOf('/'); return i < 0 ? '' : p.substring(0, i); }
  function basename(p) { const i = p.lastIndexOf('/'); return i < 0 ? p : p.substring(i + 1); }

  function colorForPath(path, depth) {
    const base = SECTION_COLORS[sectionOf(path)] || '#6e7681';
    return d3.color(base).darker(Math.min(0.3, (depth - 1) * 0.12)).toString();
  }
  function findNode(root, path) {
    if (!path) return root;
    if (root.path === path) return root;
    if (!root.children) return null;
    for (const c of root.children) {
      const found = findNode(c, path);
      if (found) return found;
    }
    return null;
  }

  // The folder context shown in the treemap, mapped panel, etc.
  // Folder selected   → that folder
  // File selected     → file's grandparent (so we see the siblings of its parent)
  // No selection      → root ('')
  function getActiveFolder() {
    if (state.selection.type === 'folder') return state.selection.path;
    if (state.selection.type === 'file') {
      const parent = dirname(state.selection.path);     // folder containing the file
      return dirname(parent);                           // grandparent = section context
    }
    return '';
  }

  // ── API ──────────────────────────────────────────────────────────────

  async function fetchSnapshot(force) {
    const url = `/api/taxonomy/snapshot?profile=${encodeURIComponent(state.profile)}${force ? '&force=true' : ''}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error('snapshot HTTP ' + r.status);
    // Any snapshot refetch invalidates the theme→files cache (mappings,
    // folder contents, vision_cache may all have changed under a write).
    if (state.mappedFilesByTheme && state.mappedFilesByTheme.size > 0) {
      state.mappedFilesByTheme.clear();
    }
    if (state.folderBreakdown && state.folderBreakdown.size > 0) {
      state.folderBreakdown.clear();
    }
    return r.json();
  }
  async function fetchFiles(path, offset, limit) {
    const url = `/api/taxonomy/folder/files?profile=${encodeURIComponent(state.profile)}` +
                `&path=${encodeURIComponent(path)}&offset=${offset}&limit=${limit}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error('files HTTP ' + r.status);
    return r.json();
  }
  async function fetchFolderBreakdown(path) {
    const url = `/api/taxonomy/folder/theme-breakdown?profile=${encodeURIComponent(state.profile)}` +
                `&path=${encodeURIComponent(path)}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error('breakdown HTTP ' + r.status);
    return r.json();
  }
  async function fetchFileMetadata(path) {
    const url = `/api/taxonomy/file/metadata?profile=${encodeURIComponent(state.profile)}` +
                `&path=${encodeURIComponent(path)}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error('metadata HTTP ' + r.status);
    return r.json();
  }
  async function fetchFileFullPipeline(path) {
    const url = `/api/taxonomy/file/full_pipeline?profile=${encodeURIComponent(state.profile)}` +
                `&path=${encodeURIComponent(path)}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error('full_pipeline HTTP ' + r.status);
    return r.json();
  }
  function thumbnailURL(path, page) {
    return `/api/taxonomy/file/thumbnail?profile=${encodeURIComponent(state.profile)}` +
           `&path=${encodeURIComponent(path)}&page=${page}`;
  }
  async function postMapping(theme, folder) {
    const r = await fetch('/api/taxonomy/mapping', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, theme, folder }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  async function patchMapping(theme, folder) {
    const r = await fetch('/api/taxonomy/mapping', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, theme, folder }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  async function deleteMapping(theme) {
    const r = await fetch('/api/taxonomy/mapping', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, theme }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  async function postUndo() {
    const r = await fetch('/api/taxonomy/undo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  async function postCreateFolder(parent, name) {
    const r = await fetch('/api/taxonomy/folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, parent, name }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  async function patchRenameFolder(path, new_name) {
    const r = await fetch('/api/taxonomy/folder', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, path, new_name }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  async function postMoveFolder(path, new_parent) {
    const r = await fetch('/api/taxonomy/folder/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, path, new_parent }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  async function fetchDeletePreview(path) {
    const url = `/api/taxonomy/folder/delete-preview?profile=${encodeURIComponent(state.profile)}` +
                `&path=${encodeURIComponent(path)}`;
    const r = await fetch(url);
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  async function deleteFolder(path, force) {
    const r = await fetch('/api/taxonomy/folder', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, path, force: !!force }),
    });
    const body = await r.json();
    if (!r.ok) {
      const err = new Error(body.error || ('HTTP ' + r.status));
      err.status = r.status;
      err.preview = body.preview;
      throw err;
    }
    return body;
  }
  async function fetchDormantMappings() {
    const r = await fetch(`/api/taxonomy/dormant-mappings?profile=${encodeURIComponent(state.profile)}`);
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  async function postBulkDeleteMappings(keys) {
    const r = await fetch('/api/taxonomy/mappings/bulk-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, keys }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  async function postBulkAddMappings(mappings) {
    // mappings = [{theme, folder}, …] avec theme dans sa casse d'origine.
    const r = await fetch('/api/taxonomy/mappings/bulk-add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, mappings }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(body.error || ('HTTP ' + r.status));
      err.status = r.status;       // 423 = lock (édition verrouillée)
      throw err;
    }
    return body;
  }
  async function postSuggestMappings(themes, useLlm) {
    // themes = noms dans leur casse d'origine. Lecture seule (pas de lock).
    const r = await fetch('/api/taxonomy/mappings/suggest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, themes, use_llm: !!useLlm }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(body.error || ('HTTP ' + r.status));
      err.status = r.status;
      throw err;
    }
    return body;
  }
  async function fetchThemeFiles(theme, limit) {
    const url = `/api/taxonomy/theme/files?profile=${encodeURIComponent(state.profile)}`
              + `&theme=${encodeURIComponent(theme)}&limit=${limit || 50}`;
    const r = await fetch(url);
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  async function fetchPreview(action, theme, folder) {
    const r = await fetch('/api/taxonomy/mapping/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, action, theme, folder }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }

  // ── Toast ────────────────────────────────────────────────────────────

  function showToast(msg, type) {
    const t = $('#tax-toast');
    t.textContent = msg;
    t.className = 'tax-toast ' + (type || 'info');
    t.style.display = 'block';
    clearTimeout(showToast._tid);
    showToast._tid = setTimeout(() => { t.style.display = 'none'; }, 4500);
  }

  // Suffixe à concaténer aux toasts rename/move quand des entries de
  // categories.yaml ont été cascadées en plus du mapping (mention "merged"
  // si une collision a fusionné des mots-clés).
  function _catSuffix(r) {
    const n = r && r.n_categories_updated;
    if (!n) return '';
    const m = r.n_categories_merged || 0;
    return m > 0
      ? `, ${n} catégorie(s) cascadée(s) [${m} fusionnée(s)]`
      : `, ${n} catégorie(s) cascadée(s)`;
  }

  // ── Busy overlay ─────────────────────────────────────────────────────
  // Locks the UI during async write actions (snapshot refetch takes 2-3s
  // and the user shouldn't be able to fire concurrent writes).
  //
  //   await withBusy("Mise à jour…", async () => { … });
  //
  // Guarantees:
  //   - overlay shown immediately (no delay → no double-click window),
  //   - setBusy(false) ALWAYS called via finally,
  //   - 30s safety timeout: if the action hangs, the overlay forces itself
  //     off with an error toast so the user isn't trapped.
  //   - clicks captured at the document level while active are swallowed
  //     (defense in depth; the backdrop's pointer-events also blocks them).

  const _busy = { depth: 0, safety: null };

  function setBusy(active, label) {
    const overlay = $('#tax-busy');
    if (!overlay) return;
    if (active) {
      _busy.depth += 1;
      if (label) $('#tax-busy-label').textContent = label;
      overlay.classList.add('is-active');
      overlay.setAttribute('aria-hidden', 'false');
    } else {
      _busy.depth = Math.max(0, _busy.depth - 1);
      if (_busy.depth === 0) {
        overlay.classList.remove('is-active');
        overlay.setAttribute('aria-hidden', 'true');
      }
    }
  }

  async function withBusy(label, fn) {
    setBusy(true, label || 'Mise à jour…');
    const safety = setTimeout(() => {
      // Force-release if the action hangs longer than 30s.
      _busy.depth = 0;
      const overlay = $('#tax-busy');
      if (overlay) {
        overlay.classList.remove('is-active');
        overlay.setAttribute('aria-hidden', 'true');
      }
      showToast('Action trop longue — vérifie l’état de l’application', 'error');
    }, 30_000);
    try {
      return await fn();
    } finally {
      clearTimeout(safety);
      setBusy(false);
    }
  }

  // ── Confirm modal ────────────────────────────────────────────────────
  // Drop-in replacement for window.confirm() that matches the rest of the
  // app's modal style. Returns a Promise<boolean>.
  //
  //   const ok = await showConfirm({
  //     title: "Annuler la modification ?",
  //     body: "Restauration depuis le backup le plus récent.",
  //     confirmLabel: "Annuler la modif",
  //     cancelLabel: "Garder",
  //     variant: "primary" | "danger",
  //   });

  function showConfirm({title, body, confirmLabel, cancelLabel, variant}) {
    return new Promise((resolve) => {
      const modal = $('#tax-confirm-modal');
      const okBtn = $('#tax-confirm-ok');
      const cancelBtn = $('#tax-confirm-cancel');
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
      // Focus the primary action so Enter validates by default
      setTimeout(() => okBtn.focus(), 50);
    });
  }

  // ── Audit modal — dormant mappings ───────────────────────────────────
  // ── Reclassify dry-run (feature C-1) ─────────────────────────────────
  // Projects what would move at the next `klodo classify --execute` using
  // only the cached folder_index. Read-only, instant once the index is warm.

  async function openReclassifyModal() {
    const modal = $('#tax-reclassify-modal');
    const body = $('#tax-reclassify-body');
    body.innerHTML = '<div class="muted">Projection en cours…</div>';
    modal.style.display = 'flex';
    await withBusy('Projection des déplacements…', async () => {
      try {
        const r = await fetch(
          `/api/taxonomy/reclassify/dryrun?profile=${encodeURIComponent(state.profile)}&sample=50`);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const data = await r.json();
        renderReclassifyBody(data);
      } catch (e) {
        body.innerHTML = '<div class="error">✗ ' + e.message + '</div>';
      }
    });
  }

  function closeReclassifyModal() {
    stopApplyPoll();
    $('#tax-reclassify-modal').style.display = 'none';
  }

  // ── Audit log / Historique modal (feature I) ─────────────────────────
  // Lists backups for theme_mapping (Mappings sub-tab) or categories.yaml
  // (Catégories sub-tab) and lets the user restore a specific one — with
  // a pre-restore backup so the operation is itself undoable.

  async function openHistoryModal() {
    const active = document.querySelector('.tax-subtab.active');
    const isCategories = active && active.dataset.view === 'categories';
    const title = isCategories
      ? '📜 Historique — categories.yaml'
      : '📜 Historique — theme_mapping.yaml + tree.yaml';
    $('#tax-history-title').textContent = title;
    const body = $('#tax-history-body');
    body.innerHTML = '<div class="muted">Chargement…</div>';
    $('#tax-history-modal').style.display = 'flex';
    await withBusy('Lecture des backups…', async () => {
      try {
        const url = isCategories
          ? `/api/categories/backups?profile=${encodeURIComponent(state.profile)}`
          : `/api/taxonomy/backups?profile=${encodeURIComponent(state.profile)}`;
        const r = await fetch(url);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const data = await r.json();
        renderHistoryBody(data, isCategories);
      } catch (e) {
        body.innerHTML = '<div class="error">✗ ' + e.message + '</div>';
      }
    });
  }

  function closeHistoryModal() {
    $('#tax-history-modal').style.display = 'none';
  }

  function renderHistoryBody(data, isCategories) {
    const body = $('#tax-history-body');
    body.innerHTML = '';
    if (data.n_total === 0) {
      body.appendChild(el('div', { class: 'tax-audit-empty muted' }, [
        '✅ Aucun backup pour l\'instant.',
        el('br'),
        el('span', { class: 'small' }, [
          'Un backup est créé automatiquement avant chaque écriture (rotation 20).',
        ]),
      ]));
      return;
    }
    body.appendChild(el('div', { class: 'tax-audit-header' }, [
      el('div', null, [
        el('strong', null, [String(data.n_total)]),
        ' backup(s) disponible(s) — du plus récent au plus ancien',
      ]),
      el('div', { class: 'muted small' }, [
        'Restaurer une version remplace l\'état actuel. ',
        'L\'état pré-restore est lui-même sauvegardé, donc l\'opération est réversible via Annuler.',
      ]),
    ]));
    const list = el('div', { class: 'tax-audit-list tax-history-list' });
    for (const b of data.backups) {
      const kindLabel = (b.kind === 'tree') ? 'Tree'
                     : (b.kind === 'mapping') ? 'Mapping'
                     : 'Categories';
      const sizeKb = (b.size_bytes / 1024).toFixed(1);
      const row = el('div', { class: 'tax-history-row' }, [
        el('span', {
          class: 'tax-history-kind tax-history-kind-' + b.kind,
          title: 'Type de backup',
        }, [kindLabel]),
        el('span', {
          class: 'tax-history-time',
          title: b.filename,
        }, ['il y a ' + b.age_human]),
        el('span', { class: 'tax-history-size muted small' },
                   [`${sizeKb} KB`]),
        el('button', {
          class: 'btn-secondary tax-history-restore',
          onclick: () => confirmRestore(b.filename, isCategories, b.kind),
        }, ['Restaurer']),
      ]);
      list.appendChild(row);
    }
    body.appendChild(list);
  }

  async function confirmRestore(filename, isCategories, kind) {
    const ok = await showConfirm({
      title: `Restaurer ce backup ?`,
      body: `${filename}\n\nL'état actuel sera sauvegardé avant le restore. `
          + `Tu pourras revenir avec ↶ Annuler si besoin.`,
      confirmLabel: 'Restaurer',
    });
    if (!ok) return;
    const url = isCategories
      ? '/api/categories/backups/restore'
      : '/api/taxonomy/backups/restore';
    await withBusy('Restauration…', async () => {
      try {
        const r = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ profile: state.profile, filename }),
        });
        const body = await r.json();
        if (!r.ok) throw new Error(body.error || 'HTTP ' + r.status);
        showToast(`✓ Restauré : ${filename}`, 'success');
        closeHistoryModal();
        if (isCategories) {
          // Notify the categories module so it drops its cached snapshot
          // and re-renders the visible view. taxonomy.js doesn't own the
          // categories DOM, so we just signal via a custom event.
          document.dispatchEvent(new CustomEvent('tax-categories-reload'));
        } else {
          state.snapshot = await fetchSnapshot();
          renderAll();
        }
      } catch (e) {
        showToast('✗ ' + e.message, 'error');
      }
    });
  }

  // ── Apply global (reclassify) ────────────────────────────────────────
  // Greffe l'application réelle des déplacements sur la modale dry-run.
  // - case « inclure P2 » → re-fetch du preview
  // - bouton Appliquer + confirmation danger + barre de progression (poll 2s)
  // - bouton Annuler le dernier apply (undo)

  async function fetchApplyPreview(keyword) {
    const r = await fetch('/api/taxonomy/reclassify/apply/preview?profile='
      + encodeURIComponent(state.profile) + '&keyword=' + (keyword ? 'true' : 'false'));
    if (!r.ok) throw new Error((await r.json()).error || r.status);
    return r.json();
  }
  async function applyPost(path) {
    const r = await fetch('/api/taxonomy/reclassify/apply/' + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    return d;
  }
  let applyPollTimer = null;
  function stopApplyPoll() {
    if (applyPollTimer) { clearInterval(applyPollTimer); applyPollTimer = null; }
  }
  function renderApplyControls(host, keyword) {
    host.innerHTML = '<div class="muted small">Chargement…</div>';
    fetchApplyPreview(keyword).then(p => {
      const st = p.state || {};
      let html = '<div class="rca-apply-bar-host">';
      html += '<label><input type="checkbox" id="rca-kw"' + (keyword ? ' checked' : '')
            + '> inclure les matchs par mot-clé (P2, faux positifs possibles)</label>';
      html += '<div class="muted small">' + p.n_moves + ' à déplacer ('
            + p.n_p1 + ' P1' + (keyword ? ' · ' + p.n_p2 + ' P2' : '') + ')</div>';
      if (st.executed) {
        html += '<button class="btn-secondary rca-danger" id="rca-undo">↩ Annuler le dernier apply</button>';
      } else {
        html += '<button class="btn-primary" id="rca-apply"'
              + (p.n_moves ? '' : ' disabled') + '>Appliquer (' + p.n_moves + ')</button>';
      }
      html += '<div id="rca-progress"></div></div>';
      host.innerHTML = html;
      document.getElementById('rca-kw').addEventListener('change', e =>
        renderApplyControls(host, e.target.checked));
      const applyBtn = document.getElementById('rca-apply');
      if (applyBtn) applyBtn.addEventListener('click', async () => {
        const dests = (p.top_destinations || []).slice(0, 5)
          .map(d => d.folder + ' (' + d.n + ')').join(', ');
        const ok = await showConfirm({
          title: 'Appliquer ' + p.n_moves + ' déplacement(s) ?',
          body: p.n_p1 + ' P1' + (keyword ? ' + ' + p.n_p2 + ' P2' : '')
              + '. Top destinations : ' + dests
              + '.\nAnnulable via le journal.',
          confirmLabel: 'Appliquer', variant: 'danger',
        });
        if (!ok) return;
        applyBtn.disabled = true;
        const kwApply = document.getElementById('rca-kw');
        if (kwApply) kwApply.disabled = true;
        try { await applyPost('execute'); startApplyPoll(host, keyword); }
        catch (e) {
          applyBtn.disabled = false;
          if (kwApply) kwApply.disabled = false;
          showToast('✗ ' + e.message, 'error');
        }
      });
      const undoBtn = document.getElementById('rca-undo');
      if (undoBtn) undoBtn.addEventListener('click', async () => {
        const ok = await showConfirm({
          title: 'Annuler le dernier apply ?',
          body: 'Re-déplace les fichiers vers leur emplacement d\'origine.',
          confirmLabel: 'Annuler', variant: 'danger',
        });
        if (!ok) return;
        undoBtn.disabled = true;
        const kwUndo = document.getElementById('rca-kw');
        if (kwUndo) kwUndo.disabled = true;
        try { await applyPost('undo'); startApplyPoll(host, keyword); }
        catch (e) {
          undoBtn.disabled = false;
          if (kwUndo) kwUndo.disabled = false;
          showToast('✗ ' + e.message, 'error');
        }
      });
    }).catch(() => { host.innerHTML = ''; });
  }
  function startApplyPoll(host, keyword) {
    stopApplyPoll();
    const tick = async () => {
      let d;
      try {
        const r = await fetch('/api/taxonomy/reclassify/apply/status?profile='
          + encodeURIComponent(state.profile));
        d = await r.json();
      } catch (e) { return; }
      const prog = d.progress; const pe = document.getElementById('rca-progress');
      if (!prog) return;
      if (prog.status === 'running') {
        const pct = prog.n_total ? Math.round(100 * prog.n_done / prog.n_total) : 0;
        if (pe) pe.innerHTML = '<div class="rca-bar"><div class="rca-fill" style="width:'
          + pct + '%"></div></div><div class="muted small">' + prog.n_done + '/' + prog.n_total
          + (prog.n_skipped ? ' · ' + prog.n_skipped + ' skip(s)' : '')
          + (prog.n_failed ? ' · ' + prog.n_failed + ' échec(s)' : '') + '</div>';
      } else {
        stopApplyPoll();
        if (prog.status === 'done') showToast('✓ Apply terminé'
          + (prog.report ? ' — logs/' + prog.report : ''), 'success');
        else if (prog.status === 'error') showToast('✗ ' + (prog.error || 'erreur'), 'error');
        renderApplyControls(host, keyword);
      }
    };
    tick(); applyPollTimer = setInterval(tick, 2000);
  }

  function renderReclassifyBody(data) {
    const body = $('#tax-reclassify-body');
    body.innerHTML = '';
    const s = data.stats;
    const step2 = data.limits.step2_included;
    // Top stats line: à déplacer / déjà en place / via étape 1 / via étape 2
    // / sans prédiction / total
    body.appendChild(el('div', { class: 'tax-reclassify-stats' }, [
      el('div', { class: 'tax-reclassify-stat tax-reclassify-stat-moving' }, [
        el('div', { class: 'tax-reclassify-stat-num' }, [String(s.n_moving)]),
        el('div', { class: 'tax-reclassify-stat-label' }, ['à déplacer']),
      ]),
      el('div', { class: 'tax-reclassify-stat tax-reclassify-stat-stable' }, [
        el('div', { class: 'tax-reclassify-stat-num' }, [String(s.n_stable)]),
        el('div', { class: 'tax-reclassify-stat-label' }, ['déjà en place']),
      ]),
      el('div', { class: 'tax-reclassify-stat tax-reclassify-stat-step1' }, [
        el('div', { class: 'tax-reclassify-stat-num' }, [String(s.n_via_step1)]),
        el('div', { class: 'tax-reclassify-stat-label' }, ['via étape 1']),
      ]),
      el('div', { class: 'tax-reclassify-stat tax-reclassify-stat-step2' }, [
        el('div', { class: 'tax-reclassify-stat-num' }, [String(s.n_via_step2)]),
        el('div', { class: 'tax-reclassify-stat-label' }, ['via étape 2']),
      ]),
      el('div', { class: 'tax-reclassify-stat tax-reclassify-stat-orphan' }, [
        el('div', { class: 'tax-reclassify-stat-num' }, [String(s.n_no_prediction)]),
        el('div', { class: 'tax-reclassify-stat-label' }, ['sans prédiction']),
      ]),
      el('div', { class: 'tax-reclassify-stat' }, [
        el('div', { class: 'tax-reclassify-stat-num' }, [String(s.n_in_lib)]),
        el('div', { class: 'tax-reclassify-stat-label' }, ['total lib']),
      ]),
    ]));
    body.appendChild(el('div', {
      class: 'tax-reclassify-caveat muted small',
    }, [
      step2
        ? '✓ Pipeline étapes 1 (theme_mapping) + 2 (KeywordClassifier via categories.yaml). '
        : '⚠ Étape 2 non disponible (categories.yaml absent ou vide). ',
      'Étape 3 (LLM Mapper, payant) non simulée — les fichiers sans prédiction y seraient envoyés en production.',
    ]));

    // By-destination section
    const dests = data.by_destination.filter(d => d.n_incoming > 0).slice(0, 20);
    if (dests.length) {
      body.appendChild(el('h3', { class: 'tax-reclassify-section-h' },
                          [`Top destinations (${dests.length})`]));
      const destList = el('div', { class: 'tax-reclassify-dest-list' });
      for (const d of dests) {
        const sources = Object.entries(d.from);
        destList.appendChild(el('div', { class: 'tax-reclassify-dest-row' }, [
          el('div', { class: 'tax-reclassify-dest-head' }, [
            el('span', { class: 'tax-reclassify-dest-folder' }, [d.folder]),
            el('span', { class: 'tax-reclassify-dest-count' },
                       [`+${d.n_incoming}`]),
          ]),
          el('div', { class: 'tax-reclassify-dest-sources muted small' },
                     ['venant de : ' + sources.map(
                        ([f, n]) => `${f} (${n})`).join(', ')]),
        ]));
      }
      body.appendChild(destList);
    }

    // Sample moves section
    if (data.sample_moves.length) {
      body.appendChild(el('h3', { class: 'tax-reclassify-section-h' }, [
        `Échantillon de ${data.sample_moves.length} fichiers (sur ${s.n_moving})`,
      ]));
      const tbl = el('div', { class: 'tax-reclassify-moves' });
      // Header row
      tbl.appendChild(el('div', { class: 'tax-reclassify-move tax-reclassify-move-head' }, [
        el('span', { class: 'tax-reclassify-move-src' }, ['Étape']),
        el('span', { class: 'tax-reclassify-move-name' }, ['Fichier']),
        el('span', { class: 'tax-reclassify-move-arrow' }, ['']),
        el('span', { class: 'tax-reclassify-move-from' }, ['Depuis']),
        el('span', { class: 'tax-reclassify-move-to' }, ['Vers']),
        el('span', { class: 'tax-reclassify-move-theme' }, ['Signal']),
      ]));
      for (const m of data.sample_moves) {
        const i = m.rel_path.lastIndexOf('/');
        const name = i < 0 ? m.rel_path : m.rel_path.substring(i + 1);
        const isStep2 = (m.source || '').startsWith('Keyword');
        // Trigger shown for the "Signal" column:
        //  - step 1 → top theme that resolved (e.g. « Mathematical Physics »)
        //  - step 2 → keyword that fired (parsed from "Keyword (deep learning)")
        let trigger = '« ' + (m.top_theme || '?') + ' »';
        if (isStep2) {
          const match = (m.source || '').match(/\(([^)]+)\)/);
          trigger = match ? 'kw: « ' + match[1] + ' »' : 'kw match';
        }
        tbl.appendChild(el('div', {
          class: 'tax-reclassify-move',
          title: m.rel_path + '\n' + (m.source || ''),
          onclick: () => {
            closeReclassifyModal();
            openFileFromPath(m.rel_path);
          },
        }, [
          el('span', {
            class: 'tax-reclassify-move-src tax-reclassify-move-src-'
                 + (isStep2 ? 'step2' : 'step1'),
          }, [isStep2 ? '2' : '1']),
          el('span', { class: 'tax-reclassify-move-name' }, [name]),
          el('span', { class: 'tax-reclassify-move-arrow' }, ['→']),
          el('span', { class: 'tax-reclassify-move-from' }, [m.from]),
          el('span', { class: 'tax-reclassify-move-to' }, [m.to]),
          el('span', { class: 'tax-reclassify-move-theme' }, [trigger]),
        ]));
      }
      body.appendChild(tbl);
    } else if (s.n_moving === 0) {
      body.appendChild(el('div', { class: 'tax-reclassify-empty muted' },
        ['✅ Aucun déplacement projeté — tout est déjà bien placé selon l\'étape 1.']));
    }

    // Apply global controls (case P2 + bouton Appliquer/Annuler + progression)
    const rcaHost = el('div', { id: 'rca-apply-controls', class: 'rca-apply-controls' });
    body.appendChild(rcaHost);
    renderApplyControls(rcaHost, false);
  }

  // ── Audit modal — dormant mappings ───────────────────────────────────
  // Lists mapping keys that no file's top theme resolves through, so the
  // user can purge them in batch from theme_mapping.yaml.

  const auditState = {
    data: null,           // dormant mappings response
    conflicts: null,      // mapping_conflicts response (lazy-loaded)
    selected: new Set(),  // mapping keys ticked for deletion (dormant tab only)
    tab: 'dormants',      // 'dormants' | 'conflicts' | 'duplicates'
  };

  async function fetchMappingConflicts() {
    const r = await fetch(
      `/api/taxonomy/mapping-conflicts?profile=${encodeURIComponent(state.profile)}`);
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }

  async function openAuditModal() {
    const modal = $('#tax-audit-modal');
    const body = $('#tax-audit-body');
    body.innerHTML = '<div class="muted">Chargement…</div>';
    modal.style.display = 'flex';
    auditState.selected.clear();
    auditState.tab = 'dormants';
    await withBusy('Analyse des mappings…', async () => {
      try {
        // Fetch both in parallel: dormants + conflicts/duplicates
        const [dormants, conflicts] = await Promise.all([
          fetchDormantMappings(),
          fetchMappingConflicts(),
        ]);
        auditState.data = dormants;
        auditState.conflicts = conflicts;
      } catch (e) {
        body.innerHTML = '<div class="error">✗ ' + e.message + '</div>';
        return;
      }
      renderAuditBody();
    });
  }

  function closeAuditModal() {
    $('#tax-audit-modal').style.display = 'none';
    auditState.data = null;
    auditState.conflicts = null;
    auditState.selected.clear();
  }

  function renderAuditBody() {
    const body = $('#tax-audit-body');
    body.innerHTML = '';
    const data = auditState.data;
    const conf = auditState.conflicts;
    if (!data) return;
    // Top header line with global stats
    body.appendChild(el('div', { class: 'tax-audit-header' }, [
      el('div', null, [
        el('strong', null, [String(data.n_total)]), ' mappings · ',
        el('strong', null, [String(data.n_active)]), ' actifs · ',
        el('strong', { class: 'tax-audit-dormant-count' },
                    [String(data.n_dormant)]), ' dormants',
      ]),
    ]));
    // Tabs
    const tabs = el('div', { class: 'tax-audit-tabs' });
    const tabDefs = [
      { id: 'dormants',
        label: `Dormants (${data.n_dormant})` },
      { id: 'conflicts',
        label: `Conflits substring (${conf ? conf.stats.n_substring_conflicts : 0})` },
      { id: 'duplicates',
        label: `Doublons (${conf ? conf.stats.n_duplicate_groups : 0})` },
    ];
    for (const t of tabDefs) {
      tabs.appendChild(el('button', {
        class: 'tax-audit-tab' + (auditState.tab === t.id ? ' active' : ''),
        onclick: () => { auditState.tab = t.id; renderAuditBody(); },
      }, [t.label]));
    }
    body.appendChild(tabs);

    if (auditState.tab === 'dormants') renderAuditDormants();
    else if (auditState.tab === 'conflicts') renderAuditSubstringConflicts();
    else renderAuditDuplicates();
    updateAuditDeleteBtn();
  }

  function renderAuditDormants() {
    const body = $('#tax-audit-body');
    const data = auditState.data;
    body.appendChild(el('div', { class: 'muted small', style: 'padding:8px 16px;' }, [
      'Dormant = aucun fichier de la lib n\'a un top theme qui résout vers ce mapping. ',
      'Supprimer ces clés ne changera la classification d\'aucun fichier.',
    ]));
    if (data.n_dormant === 0) {
      body.appendChild(el('div', { class: 'tax-audit-empty muted' }, [
        '✅ Aucun mapping dormant. Tout est utilisé.',
      ]));
      return;
    }
    // Bulk selection controls
    body.appendChild(el('div', { class: 'tax-audit-controls' }, [
      el('button', {
        class: 'btn-secondary tax-audit-select-all',
        onclick: () => {
          for (const d of data.dormant) auditState.selected.add(d.key);
          renderAuditBody();
        },
      }, [`Tout sélectionner (${data.n_dormant})`]),
      el('button', {
        class: 'btn-secondary',
        onclick: () => { auditState.selected.clear(); renderAuditBody(); },
      }, ['Tout désélectionner']),
    ]));
    const list = el('div', { class: 'tax-audit-list' });
    for (const d of data.dormant) {
      const isChecked = auditState.selected.has(d.key);
      const checkbox = el('input', { type: 'checkbox' });
      checkbox.checked = isChecked;
      const row = el('label', {
        class: 'tax-audit-row' + (isChecked ? ' selected' : ''),
      }, [
        checkbox,
        el('span', { class: 'tax-audit-key' }, [d.key]),
        el('span', { class: 'tax-audit-arrow muted small' }, ['→']),
        el('span', { class: 'tax-audit-folder' }, [d.folder]),
      ]);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) {
          auditState.selected.add(d.key);
          row.classList.add('selected');
        } else {
          auditState.selected.delete(d.key);
          row.classList.remove('selected');
        }
        updateAuditDeleteBtn();
      });
      list.appendChild(row);
    }
    body.appendChild(list);
  }

  function renderAuditSubstringConflicts() {
    const body = $('#tax-audit-body');
    const conf = auditState.conflicts;
    body.appendChild(el('div', { class: 'muted small', style: 'padding:8px 16px;' }, [
      'Conflit substring = un mapping dormant éclipsé par une clé plus longue ',
      'qui le contient comme sous-chaîne (longest-substring rule). ',
      'Pour réveiller le perdant, renomme le gagnant plus spécifiquement.',
    ]));
    const items = conf.substring_conflicts;
    if (items.length === 0) {
      body.appendChild(el('div', { class: 'tax-audit-empty muted' }, [
        '✅ Aucun conflit substring détecté.',
      ]));
      return;
    }
    const list = el('div', { class: 'tax-audit-list' });
    for (const c of items) {
      list.appendChild(el('div', { class: 'tax-audit-conflict-row' }, [
        el('div', { class: 'tax-audit-conflict-side' }, [
          el('div', { class: 'tax-audit-conflict-label muted small' }, ['LOSER (dormant)']),
          el('div', { class: 'tax-audit-key' }, [c.loser]),
          el('div', { class: 'tax-audit-folder' }, [c.loser_folder]),
        ]),
        el('div', { class: 'tax-audit-conflict-arrow' }, ['eclipsé par →']),
        el('div', { class: 'tax-audit-conflict-side tax-audit-conflict-winner' }, [
          el('div', { class: 'tax-audit-conflict-label muted small' },
                     [`WINNER (${c.winner_files} files)`]),
          el('div', { class: 'tax-audit-key' }, [c.winner]),
          el('div', { class: 'tax-audit-folder' }, [c.winner_folder]),
        ]),
      ]));
    }
    body.appendChild(list);
  }

  function renderAuditDuplicates() {
    const body = $('#tax-audit-body');
    const conf = auditState.conflicts;
    body.appendChild(el('div', { class: 'muted small', style: 'padding:8px 16px;' }, [
      'Doublon = plusieurs clés pointent vers le MÊME dossier. ',
      'Pas un bug — différentes orthographes du même thème peuvent légitimement converger ',
      '— mais à consolider si certaines clés n\'apportent rien.',
    ]));
    const groups = conf.duplicate_groups;
    if (groups.length === 0) {
      body.appendChild(el('div', { class: 'tax-audit-empty muted' }, [
        '✅ Aucun doublon. Chaque mapping pointe vers un dossier différent.',
      ]));
      return;
    }
    const list = el('div', { class: 'tax-audit-list' });
    for (const g of groups) {
      const groupEl = el('div', { class: 'tax-audit-duplicate-group' }, [
        el('div', { class: 'tax-audit-duplicate-head' }, [
          el('span', { class: 'tax-audit-folder' }, [g.folder]),
          el('span', { class: 'tax-audit-duplicate-stats muted small' },
                     [`${g.n_keys} clés · ${g.total_files} fichiers`]),
        ]),
      ]);
      const keysEl = el('div', { class: 'tax-audit-duplicate-keys' });
      for (const k of g.keys) {
        const n = g.per_key[k] || 0;
        keysEl.appendChild(el('span', {
          class: 'tax-audit-duplicate-key'
                 + (n === 0 ? ' tax-audit-duplicate-key-zero' : ''),
          title: n === 0 ? 'aucun fichier' : `${n} fichier(s)`,
        }, [
          k, ' ',
          el('span', { class: 'tax-audit-duplicate-key-count' }, [String(n)]),
        ]));
      }
      groupEl.appendChild(keysEl);
      list.appendChild(groupEl);
    }
    body.appendChild(list);
  }

  function updateAuditDeleteBtn() {
    const btn = $('#tax-audit-delete');
    // Bulk delete only makes sense on the dormants tab. Hide for other tabs
    // (substring conflicts + duplicates are pure information).
    if (auditState.tab !== 'dormants') {
      btn.style.display = 'none';
      return;
    }
    btn.style.display = '';
    const n = auditState.selected.size;
    btn.textContent = `Supprimer la sélection (${n})`;
    btn.disabled = (n === 0);
  }

  async function confirmAuditDelete() {
    const keys = Array.from(auditState.selected);
    if (!keys.length) return;
    const ok = await showConfirm({
      title: `Supprimer ${keys.length} mapping(s) dormant(s) ?`,
      body: 'Un backup unique sera créé avant la suppression. '
          + 'Réversible via le bouton Annuler.',
      confirmLabel: `Supprimer ${keys.length} clé(s)`,
      variant: 'danger',
    });
    if (!ok) return;
    await withBusy(`Suppression de ${keys.length} mapping(s)…`, async () => {
      try {
        const r = await postBulkDeleteMappings(keys);
        showToast(`✓ ${r.n_deleted} mapping(s) supprimé(s)`, 'success');
        if (r.not_found && r.not_found.length) {
          showToast(`⚠ ${r.not_found.length} introuvable(s) ignoré(s)`, 'info');
        }
        closeAuditModal();
        state.snapshot = await fetchSnapshot();
        renderAll();
      } catch (e) {
        showToast('✗ ' + e.message, 'error');
      }
    });
  }

  // Click swallower: any click that lands on the overlay itself is killed
  // at capture phase before propagation. Without this, a click on a
  // *transparent* part of the backdrop could still bubble into the page.
  document.addEventListener('click', (e) => {
    const overlay = $('#tax-busy');
    if (overlay && overlay.classList.contains('is-active')) {
      if (e.target === overlay
          || e.target.classList.contains('tax-busy-bar')
          || e.target.classList.contains('tax-busy-label')) {
        e.stopPropagation();
        e.preventDefault();
      }
    }
  }, true);

  // ── Tree (column 1) ──────────────────────────────────────────────────

  function renderTree() {
    const root = state.snapshot.tree;
    const container = $('#tax-tree');
    container.innerHTML = '';
    // Precompute visible nodes when a search is active
    const filter = computeTreeSearchFilter();
    // Compute spotlight from the currently expanded mapped theme. Search
    // takes precedence (filter is exclusive); the spotlight only kicks in
    // when no search is active.
    const spotlight = filter ? null : computeThemeSpotlight();
    container.appendChild(renderTreeNode(root, 0, filter, spotlight));
    const total = state.snapshot.stats.tree_nodes;
    const total_files = state.snapshot.stats.total_files;
    const subEl = $('#tax-tree-sub');
    subEl.innerHTML = '';
    if (filter) {
      subEl.textContent = `${filter.matchCount} match(es) sur ${total} dossiers`;
    } else if (spotlight) {
      const tab = spotlight.mode === 'future' ? 'futur' : 'actuel';
      subEl.appendChild(el('span', { class: 'tax-spotlight-sub' }, [
        `🔦 ${spotlight.affectedCount} dossier(s) — ${tab} de « ${spotlight.theme} »`,
      ]));
      subEl.appendChild(el('button', {
        class: 'tax-spotlight-exit',
        title: 'Quitter le mode focus',
        onclick: e => { e.stopPropagation(); clearSpotlight(); },
      }, ['×']));
    } else {
      subEl.textContent = `${total} dossiers · ${total_files} fichiers`;
    }
  }

  function clearSpotlight() {
    if (!state.selectedMappedTheme) return;
    state.selectedMappedTheme = null;
    renderMappedPanel();
    renderTree();
  }

  /**
   * Build a "spotlight" view of the tree highlighting folders affected by
   * the currently expanded mapped theme. Returns null if no theme is
   * selected or the data isn't loaded yet.
   *
   * Shape: {
   *   theme: <clicked theme>,
   *   mode: 'future' | 'current',
   *   counts: Map<path, number>,      // count badge per folder
   *   ancestors: Set<path>,            // kept fully visible
   *   mappedFolder: <path | null>,     // target of the mapping (highlight)
   *   affectedCount: <number>,
   * }
   */
  function computeThemeSpotlight() {
    const key = state.selectedMappedTheme;
    if (!key) return null;
    const data = state.mappedFilesByTheme.get(key);
    if (!data || data.error) return null;
    const mode = state.mappedFilesTab || 'future';
    const rawCounts = (mode === 'future') ? data.by_folder_future
                                          : data.by_folder_current;
    if (!rawCounts) return null;

    const counts = new Map();
    const ancestors = new Set();
    for (const [folder, n] of Object.entries(rawCounts)) {
      // The backend uses "(racine)" for files at the target root — normalize
      // to empty string here so it matches state.snapshot tree paths.
      const path = (folder === '(racine)') ? '' : folder;
      counts.set(path, n);
      // Collect every ancestor so the path stays visible through dimming
      let p = path;
      while (p) {
        const j = p.lastIndexOf('/');
        p = j < 0 ? '' : p.substring(0, j);
        ancestors.add(p);
      }
    }
    // Build a per-file set for the row-level marking in the tree.
    // Fetched with limit=500 → covers >99% of themes; rare cases above
    // that just lose individual-file highlights but keep folder badges.
    const items = (mode === 'future') ? (data.future || []) : (data.current || []);
    const affectedFiles = new Set(items.map(it => it.rel_path));
    return {
      theme: data.theme || key,
      mode,
      counts,
      ancestors,
      mappedFolder: data.mapped_folder || null,
      affectedCount: counts.size,
      affectedFiles,
      totalAffected: (mode === 'future') ? data.n_future : data.n_current,
    };
  }

  /**
   * Walk the tree and compute the set of paths to keep visible when the
   * tree search box is non-empty. A node is kept if:
   *   - its own path matches the query, OR
   *   - one of its descendants matches (so we can drill down to a match)
   *
   * Returns null when no search is active (skip filtering).
   * Returns { visible: Set<path>, matchCount, autoExpand: Set<path> }
   * otherwise. autoExpand contains paths to force-expand so matches are
   * visible without the user having to click chevrons.
   */
  function computeTreeSearchFilter() {
    const q = (state.treeSearch || '').trim().toLowerCase();
    if (!q) return null;
    const visible = new Set();
    const autoExpand = new Set();
    let matchCount = 0;
    function walk(node) {
      // A node "matches" if its own path (or name) contains the query
      const path = (node.path || '').toLowerCase();
      const name = (node.name || '').toLowerCase();
      const selfMatches = path.includes(q) || name.includes(q);
      let descendantMatches = false;
      if (node.children) {
        for (const c of node.children) {
          if (walk(c)) descendantMatches = true;
        }
      }
      if (selfMatches || descendantMatches) {
        visible.add(node.path);
        if (descendantMatches) autoExpand.add(node.path);
        if (selfMatches) matchCount++;
        return true;
      }
      return false;
    }
    walk(state.snapshot.tree);
    return { visible, matchCount, autoExpand };
  }

  function renderTreeNode(node, depth, filter, spotlight) {
    const isRoot = !node.path;
    // When a search filter or spotlight is active, expand any ancestor of
    // a match so the relevant rows are visible without manual chevrons.
    const isExpanded = isRoot
      || state.expanded.has(node.path)
      || (filter && filter.autoExpand.has(node.path))
      || (spotlight && spotlight.ancestors.has(node.path));
    const hasChildren = node.children && node.children.length > 0;
    const hasFiles = (node.file_count || 0) > 0;
    const isExpandable = hasChildren || hasFiles;
    const isSelected = state.selection.type === 'folder' && state.selection.path === node.path;
    const touched = touchedFoldersSet();
    const ancestors = touchedAncestorsSet(touched);
    const isTouched = touched.has(node.path);
    const isOnPath = !isTouched && ancestors.has(node.path);

    // Spotlight classification: a node is "concerned" if it has a count
    // (= contains affected files directly), or it's the mapping's target
    // folder. Ancestors are "on-path" — kept visible without highlight.
    // Other nodes are dimmed.
    let spotlightClass = '';
    let spotlightCount = null;
    if (spotlight) {
      const count = spotlight.counts.get(node.path);
      const isMappedTarget = spotlight.mappedFolder
                             && node.path === spotlight.mappedFolder;
      const isOnSpotlightPath = spotlight.ancestors.has(node.path);
      if (count != null) {
        spotlightClass = ' spotlight-affected';
        spotlightCount = count;
      } else if (isMappedTarget) {
        spotlightClass = ' spotlight-target';
      } else if (isOnSpotlightPath || isRoot) {
        spotlightClass = ' spotlight-on-path';
      } else {
        spotlightClass = ' spotlight-dim';
      }
      if (isMappedTarget) spotlightClass += ' spotlight-target';
    }

    let classes = 'tax-tree-row';
    if (isSelected) classes += ' selected';
    if (isTouched) classes += ' touched';
    else if (isOnPath) classes += ' on-path';
    classes += spotlightClass;
    const row = el('div', {
      class: classes,
      style: `padding-left:${depth * 14 + 6}px;`,
      ondragover: e => {
        e.preventDefault();
        // Move drag → check guards (can't drop into self or descendants)
        const draggingPath = state.draggingFolderPath;
        if (draggingPath !== null
            && (node.path === draggingPath
                || node.path.startsWith(draggingPath + '/'))) {
          e.dataTransfer.dropEffect = 'none';
          row.classList.add('drop-forbidden');
          return;
        }
        e.dataTransfer.dropEffect = draggingPath !== null ? 'move' : 'copy';
        row.classList.add('drop-target');
      },
      ondragleave: () => {
        row.classList.remove('drop-target');
        row.classList.remove('drop-forbidden');
      },
      ondrop: e => {
        row.classList.remove('drop-target');
        row.classList.remove('drop-forbidden');
        onDropOnFolder(e, node.path);
      },
    });
    // Root row is not draggable (you can't move the racine)
    if (!isRoot) {
      row.setAttribute('draggable', 'true');
      row.addEventListener('dragstart', (e) => {
        // Skip if the drag was initiated on an interactive child (button,
        // chevron, etc.) — those handle their own clicks.
        const targetTag = (e.target && e.target.tagName) || '';
        if (targetTag === 'BUTTON' || targetTag === 'INPUT') {
          e.preventDefault();
          return;
        }
        e.dataTransfer.setData('application/x-tax-folder', node.path);
        e.dataTransfer.setData('text/plain', node.path);   // fallback
        e.dataTransfer.effectAllowed = 'move';
        state.draggingFolderPath = node.path;
        row.classList.add('dragging');
      });
      row.addEventListener('dragend', () => {
        row.classList.remove('dragging');
        state.draggingFolderPath = null;
        // Cleanup any straggling drop-target classes
        document.querySelectorAll('.tax-tree-row.drop-target, .tax-tree-row.drop-forbidden')
          .forEach(r => {
            r.classList.remove('drop-target');
            r.classList.remove('drop-forbidden');
          });
      });
    }
    row.dataset.path = node.path;

    if (isExpandable) {
      row.appendChild(el('span', {
        class: 'tax-tree-chevron' + (isExpanded ? ' expanded' : ''),
        onclick: e => { e.stopPropagation(); toggleExpand(node.path); },
      }, [isExpanded ? '▼' : '▶']));
    } else {
      row.appendChild(el('span', { class: 'tax-tree-chevron-empty' }));
    }
    row.appendChild(el('span', { class: 'tax-tree-icon' }, ['📁']));
    row.appendChild(el('span', { class: 'tax-tree-name', onclick: () => selectFolder(node.path) },
      [isRoot ? 'racine' : node.name]));

    // Badge d'état config↔disque (Fix A/B). Jamais sur la racine.
    if (!isRoot) {
      if (node.on_disk === false && node.in_config) {
        row.appendChild(el('span', {
          class: 'tax-tree-badge tax-badge-ghost',
          title: 'Déclaré dans tree.yaml mais absent du disque',
        }, ['non créé']));
      } else if (node.in_config === false && node.on_disk) {
        row.appendChild(el('span', {
          class: 'tax-tree-badge tax-badge-orphan',
          title: 'Présent sur le disque mais hors de tree.yaml — non mappable tant qu\'il n\'est pas adopté',
        }, ['hors config']));
      }
    }

    // Fixed-width slot for the touched dot — kept empty when no dot so
    // every row's count badge sits at the same X-coord.
    const dotSlot = el('span', { class: 'tax-touched-slot' });
    if (isTouched || isOnPath) {
      dotSlot.appendChild(el('span', {
        class: 'tax-touched-dot' + (isOnPath ? ' tax-touched-dot-hollow' : ''),
        title: isTouched
          ? 'Modifié — annulable via le bouton Annuler'
          : 'Contient un dossier modifié',
      }));
    }
    row.appendChild(dotSlot);

    row.appendChild(el('span', { class: 'tax-tree-count', title: 'fichiers directs' },
      [String(node.file_count)]));

    // Spotlight badge — shows the count of files concerned by the selected
    // mapped theme that live directly in this folder (for the active tab).
    if (spotlightCount != null) {
      const tabLabel = spotlight.mode === 'future' ? 'futurs' : 'actuels';
      row.appendChild(el('span', {
        class: 'tax-tree-spotlight-badge',
        title: `${spotlightCount} fichier(s) ${tabLabel} dans ce dossier`,
      }, ['📍 ' + spotlightCount]));
    }

    // Actions grouped — fixed area, hidden until hover, right-aligned end.
    const actions = el('span', { class: 'tax-tree-actions' });
    // « Adopter » d'abord sur les nœuds hors-config (seul geste possible).
    if (!isRoot && node.in_config === false && node.on_disk) {
      actions.appendChild(el('button', {
        class: 'tax-tree-add-btn tax-tree-adopt-btn',
        title: 'Adopter ce dossier dans tree.yaml (le rendre mappable)',
        onclick: e => { e.stopPropagation(); adoptFolder(node.path); },
      }, ['adopter']));
    }
    // Les actions de mutation tree.yaml n'ont de sens que sur des dossiers
    // déjà dans la config (un hors-config doit d'abord être adopté).
    if (isRoot || node.in_config !== false) {
      actions.appendChild(el('button', {
        class: 'tax-tree-add-btn',
        title: 'Créer un sous-dossier',
        onclick: e => { e.stopPropagation(); openCreateFolderPopover(node.path, e.currentTarget); },
      }, ['+']));
    }
    if (!isRoot && node.in_config !== false) {
      actions.appendChild(el('button', {
        class: 'tax-tree-add-btn tax-tree-rename-btn',
        title: 'Renommer ce dossier',
        onclick: e => {
          e.stopPropagation();
          openRenamePopover(node.path, node.name, e.currentTarget);
        },
      }, ['✎']));
      actions.appendChild(el('button', {
        class: 'tax-tree-add-btn tax-tree-move-btn',
        title: 'Déplacer ce dossier vers un autre parent',
        onclick: e => {
          e.stopPropagation();
          openMovePopover(node.path, e.currentTarget);
        },
      }, ['⤴']));
      actions.appendChild(el('button', {
        class: 'tax-tree-add-btn tax-tree-delete-btn',
        title: 'Supprimer ce dossier',
        onclick: e => {
          e.stopPropagation();
          startDeleteFolder(node.path);
        },
      }, ['🗑']));
    }
    row.appendChild(actions);

    const wrap = el('div', { class: 'tax-tree-node' }, [row]);

    if (isExpanded && isExpandable) {
      const childWrap = el('div', { class: 'tax-tree-children' });
      if (hasChildren) {
        for (const c of node.children) {
          // Skip children that don't belong to the visible set when filter is active
          if (filter && !filter.visible.has(c.path)) continue;
          childWrap.appendChild(renderTreeNode(c, depth + 1, filter, spotlight));
        }
      }
      // Don't lazy-load files when filtering — search is folder-only
      if (hasFiles && !filter) {
        const filesWrap = el('div', {
          class: 'tax-tree-files',
          style: `padding-left:${(depth + 1) * 14 + 6}px;`,
        });
        childWrap.appendChild(filesWrap);
        lazyLoadFiles(node.path, filesWrap, 0);
      }
      wrap.appendChild(childWrap);
    }
    return wrap;
  }

  function toggleExpand(path) {
    if (state.expanded.has(path)) state.expanded.delete(path);
    else state.expanded.add(path);
    renderTree();
  }

  async function lazyLoadFiles(path, wrap, offset) {
    const cacheKey = path + '#' + offset;
    let data;
    if (state.filesByPath.has(cacheKey)) {
      data = state.filesByPath.get(cacheKey);
    } else {
      wrap.appendChild(el('div', { class: 'muted small' }, ['Chargement fichiers…']));
      try { data = await fetchFiles(path, offset, FILE_PAGE); state.filesByPath.set(cacheKey, data); }
      catch (e) { wrap.innerHTML = ''; wrap.appendChild(el('div', { class: 'small text-error' },
        ['Erreur fichiers: ' + e.message])); return; }
      wrap.innerHTML = '';
    }
    const spotlight = computeThemeSpotlight();
    for (const f of data.files) {
      const filePath = path ? path + '/' + f.name : f.name;
      const isSel = state.selection.type === 'file' && state.selection.path === filePath;
      // Mark files belonging to the currently spotlighted theme so the
      // user can scan from the folder badge down to the actual rows.
      const isSpotlit = spotlight && spotlight.affectedFiles.has(filePath);
      const isDimmed = spotlight && !isSpotlit;
      let cls = 'tax-tree-file';
      if (isSel) cls += ' selected';
      if (isSpotlit) cls += ' spotlight-file-affected';
      else if (isDimmed) cls += ' spotlight-file-dim';
      const isChecked = state.treeBulkSelected.has(filePath);
      const checkbox = el('input', {
        type: 'checkbox',
        class: 'tax-tree-file-check',
        title: 'Cocher pour inclure dans la sélection multiple',
      });
      // Use the .checked PROPERTY (runtime state) rather than the
      // `checked` attribute (default state). setAttribute('checked', ...)
      // sets defaultChecked, which can desync with the visible state
      // after re-renders and was causing rows to render as already
      // checked on first load.
      checkbox.checked = isChecked;
      checkbox.addEventListener('click', (e) => e.stopPropagation());
      checkbox.addEventListener('change', () => {
        _toggleTreeBulk(filePath, checkbox.checked, checkbox);
      });
      if (isChecked) cls += ' bulk-selected';
      const children = [
        checkbox,
        el('span', { class: 'tax-tree-icon' }, ['📄']),
        el('span', { class: 'tax-tree-name' }, [f.name]),
      ];
      if (isSpotlit) {
        children.push(el('span', {
          class: 'tax-tree-file-pin',
          title: `Concerné par « ${spotlight.theme} » (${spotlight.mode === 'future' ? 'futur' : 'actuel'})`,
        }, ['📍']));
      }
      // Inline delete button, hover-revealed. Reuses deleteCurrentFile()
      // which handles confirm modal + soft delete + tree refresh, so the
      // tree action is just shorthand for "select then delete".
      children.push(el('button', {
        type: 'button',
        class: 'tax-tree-file-delete',
        title: 'Supprimer ce fichier (corbeille — réversible via Finder)',
        'aria-label': 'Supprimer ' + f.name,
        onclick: (e) => {
          e.stopPropagation();
          deleteCurrentFile(filePath);
        },
      }, ['🗑']));
      wrap.appendChild(el('div', {
        class: cls,
        title: f.name,
        onclick: () => selectFile(filePath),
      }, children));
    }
    const shown = offset + data.files.length;
    if (shown < data.total) {
      wrap.appendChild(el('div', {
        class: 'tax-tree-more',
        onclick: e => { e.stopPropagation(); e.currentTarget.remove(); lazyLoadFiles(path, wrap, shown); },
      }, [`⋯ Afficher ${Math.min(FILE_PAGE, data.total - shown)} fichiers de plus (${shown}/${data.total})`]));
    }
  }

  // ── Treemap (column 2 bottom) ─ single-level only ───────────────────

  function renderTreemap() {
    const container = $('#tax-treemap');
    container.innerHTML = '';
    const activePath = getActiveFolder();
    const node = findNode(state.snapshot.tree, activePath);
    if (!node) { container.appendChild(emptyMsg('Dossier introuvable.')); return; }
    const w = container.clientWidth, h = container.clientHeight;
    if (w <= 0 || h <= 0) return;

    const kids = (node.children || []).filter(c => sumLeaves(c) > 0);
    // Direct files of activeFolder (those not in any sub-folder) — virtual rectangle
    const directCount = node.file_count || 0;
    if (directCount > 0) {
      kids.push({
        name: '(directs)',
        path: node.path,           // map to the active folder itself
        file_count: directCount,
        children: [],
        _direct: true,             // flag for styling / drag-drop handling
      });
    }
    if (kids.length === 0) {
      container.appendChild(emptyMsg(
        node.children && node.children.length
          ? 'Aucun fichier dans ce sous-arbre.'
          : 'Aucun sous-dossier — explore les fichiers via l\'arbre.'));
      return;
    }

    // Build flat hierarchy: virtual root + the kids (1 level)
    const root = d3.hierarchy({ name: activePath || 'racine', children: kids })
      .sum(d => d.children && d.children.length ? 0 : (d.file_count || 0));

    // 1) Compute real count + apply scale function for layout area.
    const scaleFn = state.treemapScale === 'sqrt' ? Math.sqrt : (v => v);
    root.each(d => {
      if (d.depth === 1) {
        const raw = d.data._direct ? d.data.file_count : sumLeaves(d.data);
        d.data._realCount = raw;
        d.value = scaleFn(raw) || 0.01;
      }
    });

    // 2) En mode "Lissé" : floor à 8% de la somme courante pour que chaque
    //    rectangle reste cliquable même sur des écarts extrêmes.
    if (state.treemapScale === 'sqrt') {
      const sum = root.children.reduce((s, c) => s + c.value, 0);
      const floor = sum * 0.08;
      root.children.forEach(c => { if (c.value < floor) c.value = floor; });
      // Re-sum parent (d3 va lire root.value pour le layout)
      root.value = root.children.reduce((s, c) => s + c.value, 0);
    }

    d3.treemap().size([w, h]).paddingInner(3).round(true)
      .tile(d3.treemapSquarify.ratio(1.6))(root);

    const svg = d3.select(container).append('svg').attr('width', w).attr('height', h);
    const cells = svg.selectAll('g').data(root.descendants().filter(d => d.depth === 1))
      .enter().append('g').attr('transform', d => `translate(${d.x0},${d.y0})`);

    // Highlight target: if file selected, highlight its parent folder rectangle.
    const highlightPath = state.selection.type === 'file'
      ? dirname(state.selection.path)
      : state.selection.path;
    const touched = touchedFoldersSet();
    const ancestors = touchedAncestorsSet(touched);
    const isOnPathCell = d => !touched.has(d.data.path) && ancestors.has(d.data.path);

    cells.append('rect')
      .attr('class', d => 'tax-cell tax-cell-leaf' +
            (d.data._direct ? ' tax-cell-direct' : '') +
            (d.data.path === highlightPath && !d.data._direct ? ' selected' : '') +
            (touched.has(d.data.path) ? ' tax-cell-touched' : '') +
            (isOnPathCell(d) ? ' tax-cell-onpath' : ''))
      .attr('width',  d => Math.max(0, d.x1 - d.x0))
      .attr('height', d => Math.max(0, d.y1 - d.y0))
      .attr('fill',   d => colorForPath(d.data.path, d.depth))
      .attr('fill-opacity', d => d.data._direct ? 0.35 : 1)
      .attr('stroke-dasharray', d => d.data._direct ? '5,3' : null)
      .on('mouseenter', (e, d) => showTooltip(e, d))
      .on('mousemove', moveTooltip).on('mouseleave', hideTooltip)
      .on('click', (e, d) => selectFolder(d.data.path))
      .on('dragover', (e) => { e.preventDefault(); e.currentTarget.classList.add('drop-target'); })
      .on('dragleave', (e) => e.currentTarget.classList.remove('drop-target'))
      .on('drop', (e, d) => { e.currentTarget.classList.remove('drop-target');
                              onDropOnFolder(e, d.data.path); });

    cells.filter(d => (d.x1 - d.x0) > 50 && (d.y1 - d.y0) > 16)
      .append('text').attr('class', 'tax-cell-label')
      .attr('x', 6).attr('y', 16).text(d => d.data.name);

    cells.filter(d => (d.x1 - d.x0) > 50 && (d.y1 - d.y0) > 32)
      .append('text').attr('class', 'tax-cell-label tax-cell-count')
      .attr('x', 6).attr('y', 32).text(d => (d.data._realCount || 0) + ' fichiers');

    // Corner badge for touched / on-path cells (top-right small circle)
    const badgeCells = cells.filter(d =>
      (touched.has(d.data.path) || isOnPathCell(d)) &&
      (d.x1 - d.x0) > 26 && (d.y1 - d.y0) > 20 && !d.data._direct
    );
    badgeCells.append('circle')
      .attr('cx', d => (d.x1 - d.x0) - 10)
      .attr('cy', 10)
      .attr('r', 4)
      .attr('class', d => 'tax-cell-badge' + (touched.has(d.data.path) ? ' solid' : ' hollow'));
  }

  // Recursive file count for a tree node (incl. all descendants)
  function sumLeaves(node) {
    if (!node) return 0;
    let s = node.file_count || 0;
    if (node.children) for (const c of node.children) s += sumLeaves(c);
    return s;
  }

  function emptyMsg(text) {
    return el('div', { class: 'muted', style: 'padding:24px;' }, [text]);
  }

  function showTooltip(e, d) {
    const tip = $('#tax-tooltip');
    tip.style.display = 'block';
    const mappings = (state.snapshot.mapping_by_folder[d.data.path] || []).length;
    const count = d.data._realCount != null ? d.data._realCount : d.value;
    tip.innerHTML =
      `<strong>${escapeHtml(d.data.path || 'racine')}</strong><br>` +
      `${count} fichiers · ${mappings} thèmes mappés`;
    moveTooltip(e);
  }
  function moveTooltip(e) {
    const tip = $('#tax-tooltip');
    tip.style.left = (e.pageX + 14) + 'px';
    tip.style.top  = (e.pageY + 14) + 'px';
  }
  function hideTooltip() { $('#tax-tooltip').style.display = 'none'; }

  function renderBreadcrumb() {
    const bc = $('#tax-breadcrumb');
    bc.innerHTML = '';
    const path = getActiveFolder();
    bc.appendChild(el('a', { class: 'tax-crumb', onclick: () => selectFolder('') }, ['racine']));
    if (!path) return;
    const parts = path.split('/');
    let acc = '';
    for (const p of parts) {
      acc = acc ? acc + '/' + p : p;
      const at = acc;
      bc.appendChild(document.createTextNode(' / '));
      bc.appendChild(el('a', { class: 'tax-crumb', onclick: () => selectFolder(at) }, [p]));
    }
  }

  // ── Viewer + LLM Card (column 2 top + middle) ────────────────────────

  async function renderFileSection() {
    const viewer = $('#tax-viewer-body');
    const pager = $('#tax-viewer-pager');
    const card = $('#tax-llm-card');
    const cardBody = $('#tax-llm-card-body');
    const cardSub = $('#tax-llm-card-sub');
    const viewerSub = $('#tax-viewer-sub');

    if (state.selection.type !== 'file') {
      viewer.innerHTML = '<div class="tax-viewer-empty">📄 Aucun fichier sélectionné</div>';
      pager.style.display = 'none';
      cardSub.textContent = '';
      $('#tax-llm-card-body').innerHTML =
        '<div class="muted small">Sélectionne un fichier pour voir son analyse LLM</div>';
      viewerSub.textContent = 'Sélectionne un fichier dans l\'arbre';
      return;
    }
    const path = state.selection.path;
    viewerSub.textContent = basename(path);
    viewer.innerHTML = '<div class="tax-viewer-loading">Chargement…</div>';

    let meta;
    try { meta = await fetchFileMetadata(path); }
    catch (e) {
      viewer.innerHTML = `<div class="tax-viewer-empty">Erreur: ${escapeHtml(e.message)}</div>`;
      card.style.display = 'none';
      return;
    }
    state.viewerMeta = meta;
    state.viewerCurrentPage = 1;
    // Multipage permanent : on charge en parallèle toutes les pages
    // disponibles dès la sélection (le browser parallélise les <img>).
    const totalPages = (meta.file && meta.file.page_count_estimate) || 1;
    state.viewerOpenedPages = new Set();
    for (let i = 1; i <= totalPages; i++) state.viewerOpenedPages.add(i);

    renderViewer();
    renderLLMCard(meta);
    cardSub.textContent = path;
  }

  function renderViewer() {
    const viewer = $('#tax-viewer-body');
    const pager = $('#tax-viewer-pager');
    const path = state.selection.path;
    const totalPages = (state.viewerMeta && state.viewerMeta.file && state.viewerMeta.file.page_count_estimate) || 1;

    viewer.innerHTML = '';
    const img = el('img', {
      class: 'tax-viewer-img',
      src: thumbnailURL(path, state.viewerCurrentPage),
      alt: `page ${state.viewerCurrentPage}`,
      onerror: () => { img.replaceWith(el('div', { class: 'tax-viewer-empty' }, ['Aperçu indisponible'])); },
    });
    viewer.appendChild(img);

    // Pager — multipage strip permanent
    pager.innerHTML = '';
    pager.style.display = 'flex';
    if (totalPages <= 1) {
      pager.appendChild(el('span', { class: 'muted small' }, ['1 page disponible']));
      return;
    }
    // Strip thumbs visible direct + flèches nav + indicator
    pager.appendChild(el('button', {
      class: 'btn-icon', title: 'page précédente (Cmd+←)',
      onclick: () => goToPage(state.viewerCurrentPage - 1),
    }, ['◀']));
    for (let i = 1; i <= totalPages; i++) {
      const thumb = el('div', {
        class: 'tax-thumb' + (i === state.viewerCurrentPage ? ' active' : ''),
        onclick: () => goToPage(i),
        title: `Page ${i}`,
      }, [el('img', { src: thumbnailURL(path, i), alt: `pg${i}`, loading: 'lazy' })]);
      pager.appendChild(thumb);
    }
    pager.appendChild(el('button', {
      class: 'btn-icon', title: 'page suivante (Cmd+→)',
      onclick: () => goToPage(state.viewerCurrentPage + 1),
    }, ['▶']));
    pager.appendChild(el('span', { class: 'muted small' },
      [`pg ${state.viewerCurrentPage}/${totalPages}`]));
  }

  function goToPage(p) {
    const total = (state.viewerMeta && state.viewerMeta.file.page_count_estimate) || 1;
    if (p < 1 || p > total) return;
    state.viewerCurrentPage = p;
    state.viewerOpenedPages.add(p);
    renderViewer();
  }

  function renderLLMCard(meta) {
    const body = $('#tax-llm-card-body');
    body.innerHTML = '';
    if (!meta.vision) {
      body.appendChild(el('div', { class: 'muted' },
        ['🔍 Pas d\'analyse LLM en cache pour ce fichier.']));
      return;
    }
    const v = meta.vision;
    body.appendChild(el('div', { class: 'tax-llm-meta-title' }, [v.title || '(sans titre)']));
    if (v.author) body.appendChild(el('div', { class: 'tax-llm-meta-author' }, [v.author]));
    const badges = el('div', { class: 'tax-llm-meta-badges' });
    if (v.language) badges.appendChild(el('span', { class: 'badge' }, [v.language.toUpperCase()]));
    badges.appendChild(el('span', { class: 'badge' }, [`conf ${(v.confidence * 100).toFixed(0)}%`]));
    body.appendChild(badges);

    body.appendChild(el('div', { class: 'tax-llm-meta-h' }, ['Thèmes détectés']));
    const themes = el('ul', { class: 'tax-llm-meta-themes' });
    v.themes.forEach((t, idx) => {
      const dest = t.mapped_to || '(orphelin)';
      themes.appendChild(el('li', null, [
        el('span', { class: idx === 0 ? 'dot-primary' : 'dot-secondary' }, [idx === 0 ? '●' : '○']),
        ' ',
        el('strong', null, [t.theme]),
        '  ',
        el('span', { class: 'muted small' }, [`conf ${(t.confidence * 100).toFixed(0)}%`]),
        '  → ',
        el('code', null, [dest]),
      ]));
    });
    body.appendChild(themes);

    body.appendChild(el('div', { class: 'tax-llm-meta-h' }, ['Localisation']));
    body.appendChild(el('div', null, [
      '📁 Actuel : ', el('code', null, [meta.file.current_folder || '(racine)']),
    ]));
    // Best-effort explanation of WHY the file is in its current folder
    // (the pipeline doesn't log per-file decisions; this is reconstructed
    // by matching the detected themes against the current mapping).
    if (meta.classification_reason && meta.classification_reason.kind !== 'no_themes') {
      const r = meta.classification_reason;
      const iconByKind = {
        consistent: '✅',
        secondary_theme: '🔄',
        ancestor_match: '🌿',
        no_theme_match: '❓',
      };
      const icon = iconByKind[r.kind] || 'ℹ️';
      body.appendChild(el('div', {
        class: 'tax-llm-reason tax-llm-reason-' + r.kind,
        style: 'margin-top:4px;',
      }, [
        icon + ' Pourquoi ici ? ',
        el('span', { class: 'muted small' }, [r.explanation]),
      ]));
    }
    if (meta.prediction) {
      body.appendChild(el('div', null, [
        '🎯 Prédiction theme-only : ',
        el('code', null, [meta.prediction.dest]),
        ' ', el('span', { class: 'muted small' }, [`(via "${meta.prediction.used_theme}")`]),
      ]));
      // Heuristic banner: if the chosen theme isn't the file's top theme,
      // explain why the dashboard preferred a different one.
      // (The dashboard prefers a mapping with a "/" — i.e. a specific
      // sub-folder — over a generic top-level destination.)
      const topTheme = (v.themes && v.themes[0]) ? v.themes[0].theme : null;
      if (topTheme && meta.prediction.used_theme !== topTheme) {
        body.appendChild(el('div', {
          class: 'tax-llm-heuristic',
          style: 'margin-top:4px;',
        }, [
          'ℹ️ ',
          el('span', { class: 'muted small' }, [
            `Heuristique : « ${meta.prediction.used_theme} » a un mapping plus précis que le top thème « ${topTheme} » (sous-dossier vs racine).`,
          ]),
        ]));
      }
      body.appendChild(el('div', { class: 'muted small', style: 'margin-top:4px;' },
        [meta.prediction.label]));
    } else {
      body.appendChild(el('div', { class: 'muted' },
        ['🎯 Aucune prédiction theme-only — tous les thèmes sont orphelins.']));
    }
    // Renommage — cross-link to the Rename sub-tab. Shows the rename
    // diagnostic for this file (category + similarity vs the suggested
    // name) and a button to jump to the Rename view where the user can
    // act on it.
    if (meta.rename_diagnostic) {
      body.appendChild(el('div', { class: 'tax-llm-meta-h' }, ['Renommage']));
      const rd = meta.rename_diagnostic;
      if (rd.kind === 'ok') {
        const catLabel = {
          placeholder: '🏷  Placeholder',
          divergent:   '⚠ Divergent',
          minor_case:  'ℹ️ Minor case',
          ok:          '✅ OK',
        }[rd.category] || rd.category;
        const sim = Math.round((rd.similarity || 0) * 100);
        body.appendChild(el('div', {
          class: 'tax-llm-rename tax-llm-rename-' + rd.category,
        }, [
          el('div', null, [
            el('strong', null, [catLabel]),
            '  ',
            el('span', { class: 'muted small' }, [`sim ${sim}%`]),
          ]),
          el('div', { class: 'muted small', style: 'margin-top:2px;' }, [
            '→ ', el('code', null, [rd.suggested_name]),
          ]),
        ]));
        // Jump button — switches sub-tab + selects this file in Rename
        body.appendChild(el('button', {
          class: 'tax-llm-full-btn',
          title: 'Ouvrir cet item dans la vue Rename',
          onclick: () => {
            const target = meta.file && meta.file.rel_path;
            // Switch sub-tab
            const renameBtn = document.querySelector('.tax-subtab[data-view="rename"]');
            if (renameBtn) renameBtn.click();
            // Ask the rename module to focus this candidate (event-based,
            // because the JS modules live in separate IIFEs).
            if (target) {
              document.dispatchEvent(new CustomEvent('tax-rename-select',
                { detail: { rel_path: target } }));
            }
          },
        }, ['Ouvrir dans Rename →']));
      } else if (rd.kind === 'low_confidence') {
        body.appendChild(el('div', { class: 'muted small' }, [
          `Titre LLM en confiance trop basse (${Math.round((rd.confidence||0)*100)}% < ${Math.round((rd.min_required||0)*100)}%) — diagnostic non calculé.`,
        ]));
      } else if (rd.kind === 'render_failed') {
        body.appendChild(el('div', { class: 'muted small' }, [
          'Le template a échoué : ' + (rd.issues || []).join('; '),
        ]));
      } else {
        // no_metadata: stay silent — we already say it elsewhere
      }
    }
    // Bouton "Pipeline complet" (Phase 2 C) — recalcul avec KeywordClassifier
    const fullBtn = el('button', {
      class: 'tax-llm-full-btn',
      title: 'Calcule la prédiction avec le pipeline complet (KeywordClassifier inclus)',
      onclick: () => loadFullPipelinePrediction(fullBtn),
    }, ['Voir prédiction pipeline (étapes 1+2) →']);
    body.appendChild(fullBtn);

    // ── Actions sur le fichier (feature/mapping-file-actions) ──
    // Per-file FS ops: soft-delete (to .trash/) + move-to-folder with
    // impact preview. Only shown when a file is actually selected.
    const filePath = meta.file && meta.file.rel_path;
    if (filePath) {
      const actions = el('div', { class: 'tax-llm-file-actions' });
      actions.appendChild(el('button', {
        class: 'btn-danger',
        title: 'Déplacer ce fichier vers la corbeille (.trash/<ts>/) du target — réversible via Finder',
        onclick: () => deleteCurrentFile(filePath),
      }, ['🗑 Supprimer']));
      actions.appendChild(el('button', {
        class: 'btn-move',
        title: 'Déplacer ce fichier vers un autre dossier — affiche d\'abord l\'impact (cohérence avec le mapping de thème)',
        onclick: (e) => openMoveFileDialog(filePath, e.currentTarget),
      }, ['➡ Déplacer vers…']));
      body.appendChild(actions);
    }
  }

  async function loadFullPipelinePrediction(btn) {
    btn.disabled = true;
    btn.textContent = 'Calcul en cours…';
    try {
      const r = await fetchFileFullPipeline(state.selection.path);
      btn.remove();
      const body = $('#tax-llm-card-body');
      const wrap = el('div', { class: 'tax-llm-full-result' });
      if (r.prediction) {
        wrap.appendChild(el('div', null, [
          '⚙ Prédiction pipeline (étapes 1+2) : ',
          el('code', null, [r.prediction.dest]),
        ]));
        wrap.appendChild(el('div', { class: 'muted small', style: 'margin-top:4px;' },
          [`source : ${r.prediction.source} · score : ${r.prediction.score.toFixed(2)}`]));
        wrap.appendChild(el('div', { class: 'muted small', style: 'margin-top:2px;' },
          [r.prediction.label]));
      } else {
        wrap.appendChild(el('div', { class: 'muted' },
          ['⚙ Prédiction pipeline (étapes 1+2) : aucune destination trouvée (theme_mapping + KeywordClassifier ont tous deux échoué)']));
      }
      body.appendChild(wrap);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Voir prédiction pipeline (étapes 1+2) →';
      showToast('Erreur pipeline complet : ' + e.message, 'error');
    }
  }

  // ── File operations (feature/mapping-file-actions) ──────────────────

  async function deleteCurrentFile(relPath) {
    if (!relPath) return;
    const basename = relPath.split('/').pop();
    const ok = await showConfirm({
      title: 'Supprimer ce fichier ?',
      body: `${basename}\n\nLe fichier sera déplacé vers la corbeille :\n<target>/.trash/<timestamp>/\n\nIl reste accessible via Finder — tu peux le restaurer ou le purger toi-même.`,
      confirmLabel: 'Supprimer',
      cancelLabel: 'Annuler',
      variant: 'danger',
    });
    if (!ok) return;
    try {
      const r = await fetch('/api/taxonomy/file/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          profile: state.profile,
          rel_path: relPath,
        }),
      });
      const body = await r.json();
      if (!r.ok) {
        showToast('✗ ' + (body.error || `HTTP ${r.status}`), 'error');
        return;
      }
      showToast(`🗑 Déplacé en corbeille : ${basename}`, 'success');
      // Drop the selection (file no longer at rel_path) and reload tree
      state.selection = { type: null, path: '' };
      state.snapshot = await fetchSnapshot(true);
      renderAll();
    } catch (e) {
      showToast('✗ Échec de la suppression : ' + e.message, 'error');
    }
  }

  // ── Tree bulk selection + bulk delete ────────────────────────────────

  function _toggleTreeBulk(relPath, checked, anchorEl) {
    if (checked) state.treeBulkSelected.add(relPath);
    else state.treeBulkSelected.delete(relPath);
    renderTreeBulkbar();
    // Targeted DOM update: just toggle the .bulk-selected class on this
    // one row. A full renderTree() would destroy the checkbox the user
    // just clicked and was producing inconsistent states on toggle.
    if (anchorEl) {
      const row = anchorEl.closest('.tax-tree-file');
      if (row) row.classList.toggle('bulk-selected', checked);
    }
  }

  function _clearTreeBulk() {
    if (state.treeBulkSelected.size === 0) return;
    state.treeBulkSelected.clear();
    renderTreeBulkbar();
    // Uncheck every visible checkbox + drop the .bulk-selected class on
    // every row. Cheap: only visible rows (lazy-loaded, capped at ~50/dir).
    document.querySelectorAll('.tax-tree-file-check').forEach(cb => {
      cb.checked = false;
    });
    document.querySelectorAll('.tax-tree-file.bulk-selected').forEach(row => {
      row.classList.remove('bulk-selected');
    });
  }

  function renderTreeBulkbar() {
    const bar = $('#tax-tree-bulkbar');
    const n = $('#tax-tree-bulkbar-n');
    if (!bar || !n) return;
    const count = state.treeBulkSelected.size;
    n.textContent = String(count);
    bar.hidden = count === 0;
  }

  async function _deleteTreeBulk() {
    if (state.treeBulkSelected.size === 0) return;
    const paths = Array.from(state.treeBulkSelected);
    const preview = paths.slice(0, 5)
      .map(p => '• ' + (p.split('/').pop()))
      .join('\n');
    const extra = paths.length > 5
      ? `\n…et ${paths.length - 5} autres` : '';
    const ok = await showConfirm({
      title: `Supprimer ${paths.length} fichier(s) ?`,
      body: `Les fichiers seront déplacés vers la corbeille du target :\n<target>/.trash/<timestamp>/\n\n${preview}${extra}\n\nRéversible via Finder.`,
      confirmLabel: `Supprimer ${paths.length} fichier(s)`,
      cancelLabel: 'Annuler',
      variant: 'danger',
    });
    if (!ok) return;
    try {
      const r = await fetch('/api/taxonomy/file/delete-bulk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          profile: state.profile,
          rel_paths: paths,
        }),
      });
      const body = await r.json();
      if (!r.ok) {
        showToast('✗ ' + (body.error || `HTTP ${r.status}`), 'error');
        return;
      }
      // Drop selection (the files are gone), refresh snapshot, rerender.
      // Clear the LLM-card selection if the displayed file was in the batch.
      if (state.selection.type === 'file'
          && state.treeBulkSelected.has(state.selection.path)) {
        state.selection = { type: null, path: '' };
      }
      state.treeBulkSelected.clear();
      renderTreeBulkbar();
      state.snapshot = await fetchSnapshot(true);
      renderAll();
      if (body.n_errors > 0) {
        showToast(
          `🗑 ${body.n_deleted} supprimé(s), ${body.n_errors} en erreur (voir console)`,
          'info');
        console.warn('Bulk delete errors:', body.errors);
      } else {
        showToast(
          `🗑 ${body.n_deleted} fichier(s) déplacé(s) en corbeille`,
          'success');
      }
    } catch (e) {
      showToast('✗ Échec : ' + e.message, 'error');
    }
  }

  // Move dialog: clones the folder-move popover positioning logic but
  // wires the per-file impact preview underneath the suggestion list.
  let _moveFileState = null;

  function openMoveFileDialog(relPath, anchorEl) {
    const popover = $('#tax-movefile-popover');
    const srcEl = $('#tax-movefile-source');
    const input = $('#tax-movefile-input');
    const sugWrap = $('#tax-movefile-suggestions');
    const impactWrap = $('#tax-movefile-impact');
    const confirmBtn = $('#tax-movefile-confirm');
    if (!popover) return;
    _moveFileState = { relPath, chosen: null, impact: null };
    srcEl.textContent = relPath;
    input.value = '';
    sugWrap.innerHTML = '';
    impactWrap.innerHTML = '';
    impactWrap.hidden = true;
    confirmBtn.disabled = true;
    // Position the popover near the click anchor
    const rect = anchorEl.getBoundingClientRect();
    popover.style.display = 'block';
    popover.style.left = Math.min(rect.left, window.innerWidth - 360) + 'px';
    popover.style.top = (rect.bottom + 6) + 'px';
    // Populate suggestions from the snapshot's folder tree
    renderMoveFileSuggestions('');
    setTimeout(() => input.focus(), 50);
  }

  function renderMoveFileSuggestions(query) {
    const sugWrap = $('#tax-movefile-suggestions');
    if (!sugWrap || !state.snapshot) return;
    const folders = (state.snapshot.folders || [])
      .filter(f => !query || f.toLowerCase().includes(query.toLowerCase()))
      .slice(0, 50);
    sugWrap.innerHTML = '';
    if (folders.length === 0) {
      sugWrap.appendChild(el('div', { class: 'muted small',
                                       style: 'padding:8px;' },
        ['Aucun dossier ne matche.']));
      return;
    }
    for (const f of folders) {
      sugWrap.appendChild(el('div', {
        class: 'tax-map-suggestion',
        onclick: () => _moveFilePickDest(f),
      }, [f]));
    }
  }

  async function _moveFilePickDest(folder) {
    const input = $('#tax-movefile-input');
    input.value = folder;
    _moveFileState.chosen = folder;
    // Fetch the impact preview
    const impactWrap = $('#tax-movefile-impact');
    impactWrap.hidden = false;
    impactWrap.className = 'tax-movefile-impact';
    impactWrap.innerHTML = '<span class="muted">Calcul de l\'impact…</span>';
    try {
      const url = `/api/taxonomy/file/move-impact?profile=${encodeURIComponent(state.profile)}`
                + `&path=${encodeURIComponent(_moveFileState.relPath)}`
                + `&dest=${encodeURIComponent(folder)}`;
      const r = await fetch(url);
      const body = await r.json();
      if (!r.ok) {
        impactWrap.className = 'tax-movefile-impact warn';
        impactWrap.innerHTML = '✗ ' + (body.error || `HTTP ${r.status}`);
        $('#tax-movefile-confirm').disabled = true;
        return;
      }
      _moveFileState.impact = body;
      _renderMoveFileImpact(body);
      $('#tax-movefile-confirm').disabled = false;
    } catch (e) {
      impactWrap.className = 'tax-movefile-impact warn';
      impactWrap.innerHTML = '✗ ' + e.message;
    }
  }

  function _renderMoveFileImpact(impact) {
    const wrap = $('#tax-movefile-impact');
    wrap.innerHTML = '';
    if (impact.predicted_folder === null) {
      wrap.className = 'tax-movefile-impact no-prediction';
      wrap.appendChild(el('div', null, [
        el('span', { class: 'tax-movefile-impact-icon' }, ['ℹ️']),
        'Aucune prédiction (vision_cache absent ou classifier en échec). ',
        'Le déplacement est sans risque côté reclassify.',
      ]));
      return;
    }
    if (impact.is_consistent) {
      wrap.className = 'tax-movefile-impact ok';
      wrap.appendChild(el('div', null, [
        el('span', { class: 'tax-movefile-impact-icon' }, ['✓']),
        'Cohérent avec le mapping de thème — pas d\'effet de bord au prochain reclassify.',
      ]));
      if (impact.themes_used.length) {
        wrap.appendChild(el('div', { class: 'muted small',
                                      style: 'margin-top:4px;' }, [
          'Thèmes utilisés : ' + impact.themes_used.slice(0, 3).join(', '),
        ]));
      }
    } else {
      wrap.className = 'tax-movefile-impact warn';
      const topTheme = impact.themes_used[0] || '(inconnu)';
      wrap.appendChild(el('div', null, [
        el('span', { class: 'tax-movefile-impact-icon' }, ['⚠']),
        'Au prochain reclassify, ce fichier sera proposé pour retour vers ',
        el('code', null, [impact.predicted_folder]),
        ' (théme « ', topTheme, ' »).',
      ]));
      wrap.appendChild(el('div', { class: 'muted small',
                                    style: 'margin-top:6px;' }, [
        'Pour rendre ce déplacement permanent, mappe le thème « ',
        topTheme,
        ' » vers ',
        el('code', null, [impact.dest_folder]),
        ' (via drag-drop dans la vue Mappings).',
      ]));
    }
  }

  async function _moveFileConfirm() {
    if (!_moveFileState || !_moveFileState.chosen) return;
    const popover = $('#tax-movefile-popover');
    try {
      const r = await fetch('/api/taxonomy/file/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          profile: state.profile,
          rel_path: _moveFileState.relPath,
          dest_folder: _moveFileState.chosen,
        }),
      });
      const body = await r.json();
      if (!r.ok) {
        showToast('✗ ' + (body.error || `HTTP ${r.status}`), 'error');
        return;
      }
      popover.style.display = 'none';
      showToast(`➡ Déplacé : ${body.new_rel_path || _moveFileState.relPath}`,
                'success');
      // Re-select the file at its new location + refresh tree
      const newRel = body.new_rel_path;
      state.selection = { type: 'file', path: newRel };
      state.snapshot = await fetchSnapshot(true);
      renderAll();
    } catch (e) {
      showToast('✗ Échec du déplacement : ' + e.message, 'error');
    }
  }

  function _moveFileCancel() {
    const popover = $('#tax-movefile-popover');
    if (popover) popover.style.display = 'none';
    _moveFileState = null;
  }

  // ── Selection (synchronizes everything) ──────────────────────────────

  function selectFolder(path) {
    state.selection = { type: path === '' ? null : 'folder', path: path || '' };
    if (path) {
      const parts = path.split('/');
      let acc = '';
      for (const p of parts) { acc = acc ? acc + '/' + p : p; state.expanded.add(acc); }
    }
    rerenderAfterSelection();
  }
  function selectFile(path) {
    state.selection = { type: 'file', path };
    // Expand all ancestors for visibility
    const parent = dirname(path);
    if (parent) {
      const parts = parent.split('/');
      let acc = '';
      for (const p of parts) { acc = acc ? acc + '/' + p : p; state.expanded.add(acc); }
    }
    rerenderAfterSelection();
  }
  function rerenderAfterSelection() {
    renderTree();
    renderTreemap();
    renderBreadcrumb();
    renderMappedPanel();
    renderFileSection();
  }

  // ── Mapped themes panel (column 3 top) ───────────────────────────────

  function renderMappedPanel() {
    const sub = $('#tax-mapped-sub');
    const list = $('#tax-mapped-list');
    list.innerHTML = '';

    // Path resolution — two modes:
    //   1. Spotlight active → freeze the panel on the spotlit theme's
    //      mapped folder (preserves the visual link to the tree spotlight).
    //   2. No spotlight → follow the current selection (file → its parent,
    //      folder → itself).
    let path = null;
    let frozenBySpotlight = false;
    if (state.selectedMappedTheme) {
      const data = state.mappedFilesByTheme.get(state.selectedMappedTheme);
      if (data && !data.error && data.mapped_folder != null) {
        path = data.mapped_folder;
        frozenBySpotlight = true;
      }
    }
    if (path === null) {
      if (state.selection.type === null) {
        sub.textContent = 'Sélectionne un dossier ou un fichier';
        return;
      }
      path = state.selection.type === 'file'
        ? dirname(state.selection.path)
        : state.selection.path;
    }
    const mappings = state.snapshot.mapping_by_folder[path] || [];
    const suffix = frozenBySpotlight ? ' (focus thème)' : '';
    // Compteur "X fichiers actuels · Y prévus au reclassify"
    // - actuels  = file_count direct du folder dans tree.yaml (FS scan)
    // - prévus   = somme des counts des thèmes LLM qui mappent vers ce folder
    //   (= ce que classify_by_theme acheminerait au prochain reclassify)
    const node = state.snapshot.tree
        ? findNode(state.snapshot.tree, path) : null;
    const nCurrent = node ? (node.file_count || 0) : 0;
    const mappedLower = new Set(mappings.map(t => t.toLowerCase()));
    const themesLLM = state.snapshot.themes_llm || [];
    let nReclassify = 0;
    for (const t of themesLLM) {
      if (mappedLower.has((t.theme || '').toLowerCase())) {
        nReclassify += (t.count || 0);
      }
    }
    sub.textContent = `${path || 'racine'} · ${mappings.length} règle(s) · `
                    + `${nCurrent} fichier(s) actuel(s) · `
                    + `~${nReclassify} prévu(s) au reclassify${suffix}`;
    sub.title = 'Compte des fichiers physiquement dans le dossier (snapshot FS) '
              + 'vs estimation des fichiers qui y arriveraient au prochain '
              + 'reclassify (somme des occurrences des thèmes mappés ici dans '
              + 'le vision_cache).';

    // ── Breakdown 3-way (Stables / Entrants / Sortants) ──
    // Affiché AVANT la liste des règles, donne l'aperçu du churn au prochain
    // reclassify. Lazily fetché : premier passage = "Chargement…", refetch
    // automatique au prochain renderAll() après mutation.
    list.appendChild(renderBreakdownSections(path));

    if (mappings.length === 0) {
      list.appendChild(el('li', { class: 'muted small' },
        ['Aucun thème mappé. Glisse un thème LLM ici ou utilise « + Mapper ».']));
      return;
    }
    // selectedMappedTheme is the SPOTLIGHT state — independent from the
    // current selection. We keep it alive even when navigating to a folder
    // that doesn't contain the spotlit theme. The user exits via the
    // 🔦× button in the tree subtitle.
    const touched = touchedThemesSet();
    const bulkSet = bulkSelectionForPath(path);

    // Mini-toolbar de sélection bulk (apparaît dès qu'au moins 1 cochée
    // OU si le user a fait Tout cocher sur ce folder).
    if (bulkSet.size > 0) {
      list.appendChild(renderBulkMappedToolbar(path, mappings, bulkSet));
    }

    for (const t of mappings) {
      const keyL = t.toLowerCase();
      const cb = el('input', {
        type: 'checkbox',
        class: 'tax-mapped-cb',
        title: 'Sélectionner pour suppression en lot',
        onclick: e => {
          e.stopPropagation();
          toggleBulkMapping(t, path);
          renderMappedPanel();
        },
      });
      cb.checked = bulkSet.has(keyL);
      const editBtn = el('button', {
        class: 'tax-mapped-action',
        title: 'Rediriger : changer la destination de cette règle de routage. '
             + 'Les fichiers déjà rangés ne bougent pas — seulement où ils '
             + 'iraient au prochain reclassify.',
        onclick: e => { e.stopPropagation(); openMapPopover({ theme: t, count: '?', is_edit: true }, e.currentTarget); },
      }, ['↗']);
      const delBtn = el('button', {
        class: 'tax-mapped-action tax-mapped-del',
        title: 'Supprimer cette règle de routage. Les fichiers déjà rangés '
             + 'ne bougent pas. Au prochain reclassify, les fichiers portant '
             + 'ce thème deviendront orphelins (à reclasser via Keyword '
             + 'Classifier ou LLM Mapper).',
        onclick: e => { e.stopPropagation(); confirmDeleteMapping(t); },
      }, ['×']);
      const isTouched = touched.has(t);
      const isSelected = state.selectedMappedTheme === keyL;
      const isInBulk = bulkSet.has(keyL);
      const dot = isTouched
        ? el('span', { class: 'tax-touched-dot', title: 'Modifié — annulable via le bouton Annuler' })
        : null;
      const item = el('li', {
        class: 'tax-mapped-item'
               + (isTouched ? ' touched' : '')
               + (isSelected ? ' selected' : '')
               + (isInBulk ? ' bulk-selected' : ''),
        title: t + ' — clic pour voir les fichiers concernés',
        onclick: () => toggleMappedThemeSelection(t),
      }, [cb, el('span', { class: 'tax-mapped-key' }, [t]), dot, editBtn, delBtn]);
      list.appendChild(item);
      if (isSelected) list.appendChild(renderMappedFilesExpand(t));
    }
  }

  // ── Breakdown 3-way : Stables / Entrants / Sortants ────────────────────
  //
  // Donne à l'utilisateur un aperçu IMMÉDIAT de ce qui se passera au prochain
  // reclassify sur ce dossier, SANS lancer de dry-run :
  //   ✓ Stables : déjà ici + mapping pointe ici → restent
  //   → Entrants : mapping pointe ici mais fichier ailleurs → arriveront
  //   ← Sortants : ici mais mapping ailleurs (ou orphelin) → partiront
  //
  // Fetch lazy (premier appel) + cache par path. Invalidation : renderAll()
  // après mutation purge le cache via fetchSnapshot().

  function renderBreakdownSections(path) {
    const wrap = el('li', { class: 'tax-breakdown-wrap' });
    const cached = state.folderBreakdown.get(path);

    if (cached === undefined) {
      // Premier passage : déclenche le fetch et affiche un placeholder.
      // Note: le fetch reschedule un re-render asynchrone.
      wrap.appendChild(el('div', { class: 'tax-breakdown-loading muted small' },
        ['Chargement de la décomposition…']));
      state.folderBreakdown.set(path, null);  // marqueur "fetch en cours"
      (async () => {
        try {
          const data = await fetchFolderBreakdown(path);
          state.folderBreakdown.set(path, data);
        } catch (e) {
          state.folderBreakdown.set(path, { error: e.message });
        }
        renderMappedPanel();
      })();
      return wrap;
    }
    if (cached === null) {
      wrap.appendChild(el('div', { class: 'tax-breakdown-loading muted small' },
        ['Chargement de la décomposition…']));
      return wrap;
    }
    if (cached.error) {
      wrap.appendChild(el('div', { class: 'tax-breakdown-error error small' },
        ['✗ ' + cached.error]));
      return wrap;
    }

    const stable = cached.stable || [];
    const incoming = cached.incoming || [];
    const outgoing = cached.outgoing || [];
    const totalThemes = stable.length + incoming.length + outgoing.length;
    if (totalThemes === 0) {
      wrap.appendChild(el('div', { class: 'muted small tax-breakdown-empty' },
        ['Aucun thème détecté ici, ni mappé vers ici.']));
      return wrap;
    }

    wrap.appendChild(renderBreakdownSection({
      icon: '✓',
      label: 'Stables',
      items: stable,
      cssMod: 'stable',
      countKey: 'count_in_folder',
      hint: 'Thèmes présents dans les fichiers actuels ET mappés vers ce dossier. '
          + 'Au prochain reclassify, ces fichiers RESTENT ici.',
      emptyText: '— aucun thème stable —',
    }));
    wrap.appendChild(renderBreakdownSection({
      icon: '→',
      label: 'Entrants',
      items: incoming,
      cssMod: 'incoming',
      countKey: 'count_expected',
      hint: 'Thèmes mappés vers ce dossier mais ABSENTS des fichiers actuels. '
          + 'Au prochain reclassify, ces fichiers ARRIVENT depuis ailleurs.',
      emptyText: '— rien à recevoir —',
    }));
    wrap.appendChild(renderBreakdownSection({
      icon: '←',
      label: 'Sortants',
      items: outgoing,
      cssMod: 'outgoing',
      countKey: 'count_in_folder',
      hint: 'Thèmes des fichiers actuels mais mappés vers un autre dossier '
          + '(ou orphelins). Au prochain reclassify, ces fichiers PARTENT.',
      emptyText: '— rien ne sort —',
      renderExtra: x => x.target
        ? el('span', { class: 'tax-breakdown-target', title: `Destination : ${x.target}` },
              [` → ${shortPath(x.target)}`])
        : el('span', { class: 'tax-breakdown-orphan', title: 'Aucun mapping pour ce thème' },
              [' · orphelin']),
    }));
    return wrap;
  }

  function shortPath(p) {
    // Affichage compact : "01-SCIENCES/PHYSIQUE/QUANTIQUE" → ".../QUANTIQUE"
    if (!p) return '';
    const parts = p.split('/').filter(Boolean);
    if (parts.length <= 1) return p;
    if (parts.length === 2) return p;
    return '…/' + parts[parts.length - 1];
  }

  function renderBreakdownSection(cfg) {
    const { icon, label, items, cssMod, countKey, hint, emptyText, renderExtra } = cfg;
    const n = items.length;
    const totalFiles = items.reduce((acc, x) => acc + (x[countKey] || 0), 0);
    const header = el('div', { class: 'tax-breakdown-header', title: hint }, [
      el('span', { class: 'tax-breakdown-icon' }, [icon]),
      el('span', { class: 'tax-breakdown-label' }, [label]),
      el('span', { class: 'tax-breakdown-count' },
        [n ? ` · ${n} thème(s) · ${totalFiles} fichier(s)` : ' · 0']),
    ]);
    const body = el('ul', { class: 'tax-breakdown-list' });
    if (n === 0) {
      body.appendChild(el('li', { class: 'muted small tax-breakdown-empty-row' },
        [emptyText]));
    } else {
      // Limite l'affichage à 8 pour ne pas noyer le panneau. Le détail
      // complet reste accessible via la liste des règles (entrants) ou le
      // dryrun reclassify (sortants).
      const display = items.slice(0, 8);
      for (const x of display) {
        const row = el('li', { class: 'tax-breakdown-item' }, [
          el('span', { class: 'tax-breakdown-theme', title: x.theme }, [x.theme]),
          el('span', { class: 'tax-breakdown-num' }, [` ${x[countKey]}`]),
          renderExtra ? renderExtra(x) : null,
        ]);
        body.appendChild(row);
      }
      if (items.length > display.length) {
        body.appendChild(el('li', { class: 'muted small tax-breakdown-more' },
          [`+ ${items.length - display.length} autre(s)`]));
      }
    }
    return el('div', { class: 'tax-breakdown-section tax-breakdown-' + cssMod },
      [header, body]);
  }

  // ── Bulk delete des mappings d'un folder ───────────────────────────────

  function bulkSelectionForPath(path) {
    // Si le path change, on réinitialise la sélection : éviter de
    // supprimer accidentellement des thèmes d'un folder qu'on ne voit plus.
    if (state.bulkMappedSelection.folderPath !== path) {
      state.bulkMappedSelection = { folderPath: path, keysLower: new Set() };
    }
    return state.bulkMappedSelection.keysLower;
  }

  function toggleBulkMapping(theme, path) {
    const set = bulkSelectionForPath(path);
    const k = theme.toLowerCase();
    if (set.has(k)) set.delete(k); else set.add(k);
  }

  function selectAllMappingsForPath(path, mappings) {
    const set = bulkSelectionForPath(path);
    for (const t of mappings) set.add(t.toLowerCase());
  }

  function clearBulkMappingSelection() {
    state.bulkMappedSelection.keysLower.clear();
  }

  function renderBulkMappedToolbar(path, mappings, bulkSet) {
    const n = bulkSet.size;
    const total = mappings.length;
    const allSelected = n >= total;
    const toolbar = el('li', { class: 'tax-mapped-bulk-toolbar' }, [
      el('span', { class: 'tax-mapped-bulk-count' },
         [`${n} sélectionné${n > 1 ? 's' : ''}`]),
      el('button', {
        class: 'tax-mapped-bulk-btn', title: 'Cocher tous les thèmes du dossier',
        disabled: allSelected,
        onclick: e => {
          e.stopPropagation();
          selectAllMappingsForPath(path, mappings);
          renderMappedPanel();
        },
      }, [allSelected ? '☑ Tout coché' : `☑ Tout (${total})`]),
      el('button', {
        class: 'tax-mapped-bulk-btn', title: 'Tout désélectionner',
        onclick: e => {
          e.stopPropagation();
          clearBulkMappingSelection();
          renderMappedPanel();
        },
      }, ['Annuler sélection']),
      el('button', {
        class: 'tax-mapped-bulk-btn tax-mapped-bulk-danger',
        title: 'Supprimer les mappings cochés',
        onclick: e => {
          e.stopPropagation();
          confirmBulkDeleteMappings(path, mappings);
        },
      }, [`🗑 Supprimer (${n})`]),
    ]);
    return toolbar;
  }

  async function confirmBulkDeleteMappings(path, mappings) {
    const bulkSet = bulkSelectionForPath(path);
    if (bulkSet.size === 0) return;
    // Récupère les clés EXACTES (casse préservée) depuis mappings
    const keys = mappings.filter(t => bulkSet.has(t.toLowerCase()));
    const preview = keys.slice(0, 5).map(k => `  • ${k}`).join('\n');
    const more = keys.length > 5 ? `\n  + ${keys.length - 5} autre(s)` : '';
    const ok = await showConfirm({
      title: `Supprimer ${keys.length} mapping(s) ?`,
      body: `Folder : ${path || 'racine'}\n\n${preview}${more}\n\n`
          + 'Les thèmes bruts restent dans le vision_cache — seuls les '
          + 'mappings (theme_mapping.yaml) sont retirés. Backup auto + '
          + 'Annulable via le bouton Annuler du header.',
      confirmLabel: `Supprimer ${keys.length} clé(s)`,
      variant: 'danger',
    });
    if (!ok) return;
    await withBusy(`Suppression de ${keys.length} mapping(s)…`, async () => {
      try {
        const r = await postBulkDeleteMappings(keys);
        showToast(`✓ ${r.n_deleted} mapping(s) supprimé(s)`, 'success');
        if (r.not_found && r.not_found.length) {
          showToast(`⚠ ${r.not_found.length} introuvable(s) ignoré(s)`, 'info');
        }
        clearBulkMappingSelection();
        state.snapshot = await fetchSnapshot();
        renderAll();
      } catch (e) {
        showToast('✗ ' + e.message, 'error');
      }
    });
  }

  function toggleMappedThemeSelection(theme) {
    const key = theme.toLowerCase();
    if (state.selectedMappedTheme === key) {
      state.selectedMappedTheme = null;
    } else {
      state.selectedMappedTheme = key;
      // Cache miss → fetch + re-render once received
      if (!state.mappedFilesByTheme.has(key)) {
        withBusy('Recherche des fichiers concernés…', async () => {
          try {
            // Fetch with a high limit so the spotlight can mark individual
            // files in the tree (the inline expand still only displays the
            // first ~50). 500 covers >99% of themes in practice.
            const data = await fetchThemeFiles(theme, 500);
            state.mappedFilesByTheme.set(key, data);
          } catch (err) {
            state.mappedFilesByTheme.set(key, { error: err.message });
          }
          renderMappedPanel();
          renderTree();   // spotlight depends on the just-fetched data
        });
      }
    }
    renderMappedPanel();
    renderTree();         // refresh spotlight on selection toggle
  }

  function renderMappedFilesExpand(theme) {
    const key = theme.toLowerCase();
    const data = state.mappedFilesByTheme.get(key);
    const wrap = el('li', { class: 'tax-mapped-expand' });
    if (!data) {
      wrap.appendChild(el('div', { class: 'muted small' }, ['Chargement…']));
      return wrap;
    }
    if (data.error) {
      wrap.appendChild(el('div', { class: 'error small' }, ['✗ ' + data.error]));
      return wrap;
    }
    const tab = state.mappedFilesTab || 'future';
    const tabs = el('div', { class: 'tax-mapped-tabs' }, [
      el('button', {
        class: 'tax-mapped-tab' + (tab === 'future' ? ' active' : ''),
        onclick: e => {
          e.stopPropagation();
          state.mappedFilesTab = 'future';
          renderMappedPanel(); renderTree();
        },
        title: 'Fichiers ayant ce thème dans le LLM cache (impact au prochain reclassify)',
      }, [`Impact futur (${data.n_future})`]),
      el('button', {
        class: 'tax-mapped-tab' + (tab === 'current' ? ' active' : ''),
        onclick: e => {
          e.stopPropagation();
          state.mappedFilesTab = 'current';
          renderMappedPanel(); renderTree();
        },
        title: 'Fichiers actuellement présents dans le dossier mappé',
      }, [`Actuellement (${data.n_current})`]),
    ]);
    wrap.appendChild(tabs);

    const items = tab === 'future' ? data.future : data.current;
    const n_total = tab === 'future' ? data.n_future : data.n_current;
    if (!items || items.length === 0) {
      wrap.appendChild(el('div', { class: 'muted small tax-mapped-empty' },
        [tab === 'future'
          ? 'Aucun fichier avec ce thème dans le cache vision.'
          : (data.mapped_folder
            ? `Dossier « ${data.mapped_folder} » vide.`
            : 'Aucun dossier mappé pour ce thème.')]));
      return wrap;
    }
    const listEl = el('ul', { class: 'tax-mapped-files' });
    // Column header — same grid as the rows so labels align with values.
    listEl.appendChild(el('li', { class: 'tax-mapped-file tax-mapped-file-head' }, [
      el('span', { class: 'tax-mapped-file-name' }, ['Fichier']),
      el('span', { class: 'tax-mapped-file-toptheme' }, ['Top thème']),
      el('span', { class: 'tax-mapped-file-folder' }, ['Dossier actuel']),
      el('span', { class: 'tax-mapped-file-conf', title: 'Confidence LLM' }, ['Conf']),
    ]));
    for (const f of items) {
      const name = basename(f.rel_path);
      const folder = f.current_folder || '(racine)';
      // Future items carry top_theme + top_confidence (the theme that
      // drove the prediction). Current items carry confidence (this
      // theme's confidence on the file).
      const conf = (f.top_confidence != null) ? f.top_confidence
                : (f.confidence != null) ? f.confidence
                : null;
      // For future items, show the top theme that drove the prediction
      // (often differs from the clicked theme key — e.g. clicked "physics"
      // but resolved via "Mathematical Physics" substring match). The slot
      // is ALWAYS rendered (empty if not applicable) so the grid columns
      // line up across rows.
      const showTopTheme = f.top_theme
        && f.top_theme.toLowerCase() !== (state.selectedMappedTheme || '');
      const topThemeEl = el('span', {
        class: 'tax-mapped-file-toptheme',
        title: showTopTheme ? 'Prédit via le thème top du fichier' : '',
      }, [showTopTheme ? '« ' + f.top_theme + ' »' : '']);
      const confEl = el('span', {
        class: 'tax-mapped-file-conf',
        title: 'Confidence LLM',
      }, [conf != null ? conf.toFixed(2) : '']);
      listEl.appendChild(el('li', {
        class: 'tax-mapped-file',
        title: f.rel_path,
        onclick: e => { e.stopPropagation(); openFileFromPath(f.rel_path); },
      }, [
        el('span', { class: 'tax-mapped-file-name' }, [name]),
        topThemeEl,
        el('span', { class: 'tax-mapped-file-folder' }, [folder]),
        confEl,
      ]));
    }
    wrap.appendChild(listEl);
    if (items.length < n_total) {
      wrap.appendChild(el('div', { class: 'muted small tax-mapped-more' },
        [`+${n_total - items.length} autres fichiers — affinage à venir`]));
    }
    return wrap;
  }

  // Helper: navigate to a file by relative path (expand parents + select)
  function openFileFromPath(relPath) {
    const parent = dirname(relPath);
    // Expand all ancestors so the file becomes visible
    let p = parent;
    while (p) {
      state.expanded.add(p);
      const j = p.lastIndexOf('/');
      if (j < 0) break;
      p = p.substring(0, j);
    }
    state.expanded.add(parent);
    state.selection = { type: 'file', path: relPath };
    rerenderAfterSelection();
  }

  function basename(p) {
    const i = p.lastIndexOf('/');
    return i < 0 ? p : p.substring(i + 1);
  }

  // ── LLM universe panel (column 3 bottom) ─────────────────────────────

  function renderLLMPanel() {
    const sub = $('#tax-llm-sub');
    const list = $('#tax-llm-list');
    list.innerHTML = '';
    const all = state.snapshot.themes_llm || [];
    let filtered = all;
    if (state.orphOnly) filtered = filtered.filter(t => t.is_orphan);
    if (state.search) {
      const q = state.search;
      filtered = filtered.filter(t => t.theme.toLowerCase().includes(q));
    }
    sub.textContent = `${filtered.length}/${all.length} affichés`;
    // Mémorise les thèmes actuellement visibles (après filtres recherche /
    // orphelins) — sert au bouton « Tout sélectionner (filtrés) » de la
    // toolbar bulk pour ne cocher QUE ce que l'utilisateur voit.
    const MAX = 300;
    const visible = filtered.slice(0, MAX);
    state.bulkLLMVisible = visible.map(t => t.theme);
    const bulkSet = state.bulkLLMSelection;
    for (const t of visible) {
      const keyL = t.theme.toLowerCase();
      const cb = el('input', {
        type: 'checkbox',
        class: 'tax-llm-cb',
        title: 'Sélectionner pour une action en lot',
        // Le clic sur la checkbox ne doit PAS démarrer un drag ni cliquer la
        // ligne : on stoppe la propagation. mousedown bloque l'amorce de drag.
        onmousedown: e => { e.stopPropagation(); },
        onclick: e => {
          e.stopPropagation();
          toggleBulkLLM(t.theme);
          renderLLMPanel();
        },
      });
      cb.checked = bulkSet.has(keyL);
      const mainLine = el('div', { class: 'tax-llm-item-main' }, [
        cb,
        el('span', { class: 'tax-llm-name' }, [t.theme]),
        el('span', { class: 'tax-llm-count' }, [String(t.count)]),
        el('button', {
          class: 'tax-llm-map-btn',
          title: t.is_orphan ? 'Mapper ce thème (orphelin)' : 'Re-mapper ce thème',
          onclick: e => { e.stopPropagation(); openMapPopover(t, e.currentTarget); },
        }, ['+']),
      ]);
      // Ligne 2 : destination actuelle si mappé, sinon badge orphelin.
      // Both rendered as pill-style tags (blue vs orange) for symmetry —
      // every theme tells you where it goes (or that it goes nowhere).
      const subLine = el('div', { class: 'tax-llm-item-sub' }, [
        t.is_orphan
          ? el('span', { class: 'tax-llm-orph-tag' }, ['orphelin'])
          : el('span', { class: 'tax-llm-dest-tag', title: t.mapped_to },
                       [t.mapped_to]),
      ]);
      const row = el('div', {
        class: 'tax-llm-item' + (t.is_orphan ? ' orphan' : '')
               + (bulkSet.has(keyL) ? ' bulk-selected' : ''),
        draggable: 'true',
        title: (t.sample_titles && t.sample_titles.length
                ? 'Échantillon : ' + t.sample_titles.join(' / ') : ''),
        ondragstart: e => {
          e.dataTransfer.setData('text/plain', t.theme);
          e.dataTransfer.effectAllowed = 'copy';
        },
      }, [mainLine, subLine]);
      list.appendChild(row);
    }
    if (filtered.length > MAX) {
      list.appendChild(el('div', { class: 'muted small', style: 'padding:8px;' },
        [`(${filtered.length - MAX} de plus — affine la recherche)`]));
    }
    renderLLMBulkToolbar();
  }

  // ── Multi-sélection des thèmes LLM (colonne Thèmes LLM) ────────────────
  //
  // Calque le patron bulkMappedSelection des thèmes mappés. La toolbar
  // n'apparaît que dès 1 thème coché. Les actions réelles (Mapper→dossier,
  // Suggérer) sont posées par les Tâches 7-8 ; ici seulement « Tout
  // sélectionner (filtrés) » + « Effacer ».

  function toggleBulkLLM(theme) {
    const k = theme.toLowerCase();
    if (state.bulkLLMSelection.has(k)) state.bulkLLMSelection.delete(k);
    else state.bulkLLMSelection.add(k);
  }

  function selectAllVisibleLLM() {
    // Coche tous les thèmes actuellement VISIBLES (après filtre recherche /
    // orphelins + cap MAX), pas l'univers entier.
    for (const theme of (state.bulkLLMVisible || [])) {
      state.bulkLLMSelection.add(theme.toLowerCase());
    }
  }

  function clearBulkLLMSelection() {
    state.bulkLLMSelection.clear();
  }

  function renderLLMBulkToolbar() {
    const host = $('#tax-llm-bulk-toolbar');
    if (!host) return;
    host.innerHTML = '';
    const n = state.bulkLLMSelection.size;
    if (n === 0) {
      host.style.display = 'none';
      return;
    }
    host.style.display = 'flex';
    const visible = state.bulkLLMVisible || [];
    // « Tout sélectionner (filtrés) » désactivé si tous les visibles sont
    // déjà cochés (rien de plus à ajouter).
    const allVisibleSelected = visible.length > 0
      && visible.every(t => state.bulkLLMSelection.has(t.toLowerCase()));
    host.appendChild(el('span', { class: 'tax-llm-bulk-count' },
      [`${n} sélectionné${n > 1 ? 's' : ''}`]));
    host.appendChild(el('button', {
      class: 'tax-llm-bulk-btn',
      title: 'Cocher tous les thèmes actuellement affichés (après filtres)',
      disabled: allVisibleSelected,
      onclick: e => { e.stopPropagation(); selectAllVisibleLLM(); renderLLMPanel(); },
    }, [allVisibleSelected
        ? '☑ Tous cochés'
        : `☑ Tout sélectionner (${visible.length})`]));
    host.appendChild(el('button', {
      class: 'tax-llm-bulk-btn',
      title: 'Vider la sélection',
      onclick: e => { e.stopPropagation(); clearBulkLLMSelection(); renderLLMPanel(); },
    }, ['Effacer']));
    // Action 1 (Tâche 7) : mapper toute la sélection vers UN dossier.
    host.appendChild(el('button', {
      class: 'tax-llm-bulk-btn tax-llm-bulk-action',
      title: 'Choisir un dossier et y affecter tous les thèmes sélectionnés',
      onclick: e => { e.stopPropagation(); openBulkMapPopover(e.currentTarget); },
    }, ['Mapper la sélection → dossier…']));
    // Action 2 (Tâche 8) : suggère un dossier par thème (déterministe →
    // LLM optionnel), ouvre un panneau de revue accept/édition.
    host.appendChild(el('button', {
      class: 'tax-llm-bulk-btn tax-llm-bulk-action',
      title: 'Suggérer un dossier par thème puis réviser avant d\'appliquer',
      onclick: e => { e.stopPropagation(); openSuggestReview(); },
    }, ['💡 Suggérer + Mapper']));
  }

  // ── "+ Mapper" popover with folder autocomplete ──────────────────────

  function openMapPopover(theme, anchorEl) {
    state.popover = { open: true, theme, anchorEl, mode: 'single' };
    const pop = $('#tax-map-popover');
    const isEdit = theme.is_edit === true;
    pop.querySelector('.tax-map-popover-title').textContent =
      isEdit ? 'Rediriger ce mapping' : 'Mapper le thème vers un dossier';
    const subText = isEdit
      ? `« ${theme.theme} »  (actuel : ${getActiveFolder() || 'racine'})`
      : `« ${theme.theme} »  (${theme.count} fichiers)`;
    $('#tax-map-popover-theme').textContent = subText;
    $('#tax-map-popover-confirm').textContent = isEdit ? 'Rediriger' : 'Mapper';
    const input = $('#tax-map-popover-input');
    input.value = isEdit ? (getActiveFolder() || '') : '';
    renderPopoverSuggestions(input.value);
    // Position near the anchor button
    const rect = anchorEl.getBoundingClientRect();
    pop.style.display = 'block';
    const popW = 360;
    pop.style.left = Math.min(window.innerWidth - popW - 12, rect.left - popW + 30) + 'px';
    pop.style.top  = (rect.bottom + 8) + 'px';
    setTimeout(() => { input.focus(); input.select(); }, 50);
  }
  function closeMapPopover() {
    state.popover = { open: false, theme: null, anchorEl: null, mode: 'single' };
    $('#tax-map-popover').style.display = 'none';
  }
  function renderPopoverSuggestions(query) {
    const folders = state.snapshot.folders || [];
    const q = (query || '').toLowerCase();
    const matches = folders.filter(f => !q || f.toLowerCase().includes(q)).slice(0, 8);
    const wrap = $('#tax-map-popover-suggestions');
    wrap.innerHTML = '';
    for (const f of matches) {
      wrap.appendChild(el('div', {
        class: 'tax-map-suggestion', onclick: () => { $('#tax-map-popover-input').value = f; },
      }, [f]));
    }
    if (!matches.length) wrap.appendChild(el('div', { class: 'muted small' }, ['Aucun dossier ne correspond']));
  }
  async function confirmMapPopover() {
    const folder = $('#tax-map-popover-input').value.trim();
    if (!folder) return;
    // Mode « bulk » : mapper toute la sélection des thèmes LLM vers ce dossier.
    if (state.popover.mode === 'bulk') {
      closeMapPopover();
      await confirmBulkMapToFolder(folder);
      return;
    }
    if (!state.popover.theme) return;
    const theme = state.popover.theme.theme;
    const isEdit = state.popover.theme.is_edit === true;
    closeMapPopover();
    if (isEdit) await doUpdateMapping(theme, folder);
    else        await doAddMapping(theme, folder);
  }

  // ── Bulk « Mapper la sélection → dossier » (Tâche 7) ──────────────────
  //
  // Réutilise le popover/autocomplete #tax-map-popover en mode « bulk » :
  // l'utilisateur choisit UN dossier cible, puis tous les thèmes cochés
  // (state.bulkLLMSelection) y sont affectés via POST bulk-add (un backup,
  // une transaction). Les thèmes sont stockés en minuscule dans la
  // sélection — on récupère leur casse d'origine via state.snapshot.themes_llm
  // pour envoyer le vrai nom au backend.

  function openBulkMapPopover(anchorEl) {
    const n = state.bulkLLMSelection.size;
    if (n === 0) return;
    state.popover = { open: true, theme: null, anchorEl, mode: 'bulk' };
    const pop = $('#tax-map-popover');
    pop.querySelector('.tax-map-popover-title').textContent =
      'Mapper la sélection vers un dossier';
    $('#tax-map-popover-theme').textContent =
      `${n} thème${n > 1 ? 's' : ''} sélectionné${n > 1 ? 's' : ''}`;
    $('#tax-map-popover-confirm').textContent = 'Mapper la sélection';
    const input = $('#tax-map-popover-input');
    input.value = '';
    renderPopoverSuggestions('');
    const rect = anchorEl.getBoundingClientRect();
    pop.style.display = 'block';
    const popW = 360;
    pop.style.left = Math.min(window.innerWidth - popW - 12,
                             Math.max(12, rect.left - popW + 30)) + 'px';
    pop.style.top  = (rect.bottom + 8) + 'px';
    setTimeout(() => { input.focus(); input.select(); }, 50);
  }

  async function confirmBulkMapToFolder(folder) {
    const selKeys = [...state.bulkLLMSelection];
    if (!selKeys.length || !folder) return;
    // Récupère le vrai nom (casse d'origine) de chaque thème via le snapshot.
    const byLower = new Map();
    for (const t of (state.snapshot.themes_llm || [])) {
      byLower.set(t.theme.toLowerCase(), t);
    }
    const mappings = selKeys.map(k => {
      const rec = byLower.get(k);
      return { theme: rec ? rec.theme : k, folder };
    });
    // Confirmation (symétrie avec le flux unitaire previewAndConfirm) : un
    // mapping EN LOT est plus impactant qu'un seul, on confirme toujours, avec
    // l'estimation de fichiers (somme des counts des thèmes sélectionnés).
    let estFiles = 0;
    for (const k of selKeys) { const rec = byLower.get(k); if (rec) estFiles += (rec.count || 0); }
    const n = mappings.length;
    const ok = await showConfirm({
      title: `Mapper ${n} thème${n > 1 ? 's' : ''} → ${folder} ?`,
      body: `Ces ${n} thème${n > 1 ? 's' : ''} totalisent ≈ ${estFiles} fichier${estFiles > 1 ? 's' : ''} qui seraient reclassés au prochain reclassify. Les thèmes déjà mappés seront ignorés.`,
      confirmLabel: 'Mapper la sélection',
      cancelLabel: 'Annuler',
    });
    if (!ok) return;
    await withBusy(`Mapping de ${mappings.length} thème(s) → ${folder}…`, async () => {
      try {
        const r = await postBulkAddMappings(mappings);
        const added = r.added || [];
        const skipped = r.skipped || [];
        // M = somme des counts des thèmes effectivement ajoutés.
        const addedLower = new Set(added.map(a => a.theme.toLowerCase()));
        let files = 0;
        for (const t of (state.snapshot.themes_llm || [])) {
          if (addedLower.has(t.theme.toLowerCase())) files += (t.count || 0);
        }
        const nAdded = r.n_added != null ? r.n_added : added.length;
        let msg = `✓ ${nAdded} thème${nAdded > 1 ? 's' : ''} mappé${nAdded > 1 ? 's' : ''} → ${folder}`
                + ` · ${files} fichier${files > 1 ? 's' : ''} au prochain reclassify`;
        if (skipped.length) {
          const already = skipped.filter(s => s.reason === 'already_mapped').length;
          msg += already === skipped.length
            ? ` · ${skipped.length} ignoré${skipped.length > 1 ? 's' : ''} (déjà mappé${skipped.length > 1 ? 's' : ''})`
            : ` · ${skipped.length} ignoré${skipped.length > 1 ? 's' : ''}`;
        }
        showToast(msg, nAdded > 0 ? 'success' : 'info');
        clearBulkLLMSelection();
        state.snapshot = await fetchSnapshot();
        renderAll();
      } catch (e) {
        if (e.status === 423) {
          showToast('✗ Édition verrouillée (run en cours) — réessaie plus tard', 'error');
        } else {
          showToast('✗ ' + e.message, 'error');
        }
      }
    });
  }

  // ── Bulk « Suggérer + Mapper » (Tâche 8) ─────────────────────────────
  //
  // Calque le patron casse-d'origine / toast / refresh / lock de la Tâche 7,
  // mais en deux temps : on demande d'abord à l'endpoint suggest un dossier
  // par thème (passe déterministe gratuite), on affiche un panneau de revue
  // éditable, et on n'applique en bulk-add QUE les suggestions cochées avec
  // un dossier non vide. Un bouton complète à la demande les non-résolus
  // via le LLM (use_llm=true sur ce sous-ensemble uniquement).

  const SUGGEST_SOURCE_META = {
    deterministic: { label: 'déterministe', cls: 'tax-suggest-src-det' },
    llm:           { label: 'LLM',          cls: 'tax-suggest-src-llm' },
    unresolved:    { label: 'non résolu',   cls: 'tax-suggest-src-unres' },
  };

  // Résout la casse d'origine des thèmes de bulkLLMSelection (minuscule)
  // via le snapshot. Retourne la liste des vrais noms.
  function selectedThemeNames() {
    const byLower = new Map();
    for (const t of (state.snapshot.themes_llm || [])) {
      byLower.set(t.theme.toLowerCase(), t.theme);
    }
    return [...state.bulkLLMSelection].map(k => byLower.get(k) || k);
  }

  async function openSuggestReview() {
    const themes = selectedThemeNames();
    if (!themes.length) return;
    const modal = $('#tax-suggest-modal');
    const body = $('#tax-suggest-body');
    body.innerHTML = '<div class="muted">Suggestion en cours…</div>';
    $('#tax-suggest-apply').disabled = true;
    $('#tax-suggest-apply').textContent = 'Appliquer (0)';
    modal.style.display = 'flex';
    await withBusy('Suggestion des dossiers (LLM, 1 appel groupé)…', async () => {
      try {
        // use_llm=true : le déterministe fiable tourne d'abord côté backend
        // (matchs de nom exacts, gratuits), puis le LLM batché résout le reste.
        const r = await postSuggestMappings(themes, true);
        state.suggestReview = (r.suggestions || []).map(s => ({
          theme: s.theme,
          folder: s.folder || '',
          confidence: s.confidence || 0,
          source: s.source || 'unresolved',
          reason: s.reason || '',
          // Non-résolus décochés par défaut ; le reste coché.
          accepted: s.source !== 'unresolved',
        }));
        renderSuggestReview();
      } catch (e) {
        body.innerHTML = '<div class="error">✗ ' + escapeHtml(e.message) + '</div>';
      }
    });
  }

  function closeSuggestReview() {
    $('#tax-suggest-modal').style.display = 'none';
    state.suggestReview = null;
    // Ferme tout autocomplete inline encore ouvert.
    const open = $('#tax-suggest-inline-pop');
    if (open) open.remove();
  }

  function renderSuggestReview() {
    const body = $('#tax-suggest-body');
    body.innerHTML = '';
    const rows = state.suggestReview || [];
    if (!rows.length) {
      body.appendChild(el('div', { class: 'tax-audit-empty muted' },
        ['Aucune suggestion à réviser.']));
      updateSuggestApplyBtn();
      return;
    }
    body.appendChild(el('div', { class: 'muted small', style: 'padding:0 0 10px;' }, [
      `${rows.length} thème(s) · `,
      'coche ceux à appliquer, corrige le dossier si besoin. ',
      'Les non-résolus sont décochés par défaut.',
    ]));
    const list = el('div', { class: 'tax-suggest-list' });
    rows.forEach((row, i) => list.appendChild(renderSuggestRow(row, i)));
    body.appendChild(list);
    updateSuggestApplyBtn();
  }

  function renderSuggestRow(row, i) {
    const meta = SUGGEST_SOURCE_META[row.source] || SUGGEST_SOURCE_META.unresolved;
    const cb = el('input', { type: 'checkbox', class: 'tax-suggest-cb' });
    cb.checked = !!row.accepted;
    cb.addEventListener('change', () => {
      row.accepted = cb.checked;
      rowEl.classList.toggle('accepted', cb.checked);
      updateSuggestApplyBtn();
    });
    // Champ dossier éditable : clic → ouvre l'autocomplete inline.
    const folderField = el('span', {
      class: 'tax-suggest-folder' + (row.folder ? '' : ' empty'),
      title: 'Cliquer pour corriger le dossier',
      onclick: e => { e.stopPropagation(); openSuggestInlinePopover(row, i, e.currentTarget); },
    }, [row.folder || '(choisir un dossier…)']);
    const conf = Math.round((row.confidence || 0) * 100);
    const rowEl = el('div', {
      class: 'tax-suggest-row' + (row.accepted ? ' accepted' : ''),
    }, [
      el('label', { class: 'tax-suggest-cell-cb' }, [cb]),
      el('div', { class: 'tax-suggest-cell-main' }, [
        el('div', { class: 'tax-suggest-theme' }, [row.theme]),
        el('div', { class: 'tax-suggest-meta' }, [
          el('span', { class: 'tax-suggest-src ' + meta.cls }, [meta.label]),
          el('span', {
            class: 'tax-suggest-conf',
            title: row.reason || '',
          }, [
            el('span', { class: 'tax-suggest-conf-bar' }, [
              el('span', {
                class: 'tax-suggest-conf-fill',
                style: `width:${conf}%;`,
              }),
            ]),
            el('span', { class: 'tax-suggest-conf-pct' }, [conf + '%']),
          ]),
          row.reason
            ? el('span', { class: 'tax-suggest-reason muted small' }, [row.reason])
            : null,
        ]),
      ]),
      el('div', { class: 'tax-suggest-cell-folder' }, [
        el('span', { class: 'tax-suggest-arrow muted' }, ['→']),
        folderField,
      ]),
    ]);
    return rowEl;
  }

  // Autocomplete inline de dossier dans une ligne de revue — réutilise la
  // source state.snapshot.folders comme renderPopoverSuggestions.
  function openSuggestInlinePopover(row, i, anchorEl) {
    const existing = $('#tax-suggest-inline-pop');
    if (existing) existing.remove();
    const folders = state.snapshot.folders || [];
    const pop = el('div', { class: 'tax-map-popover tax-suggest-inline-pop', id: 'tax-suggest-inline-pop' });
    const input = el('input', {
      type: 'text', class: 'tax-suggest-inline-input',
      placeholder: 'Filtrer les dossiers…',
    });
    input.value = row.folder || '';
    const sug = el('div', { class: 'tax-map-popover-suggestions' });
    function renderInline(q) {
      const ql = (q || '').toLowerCase();
      const matches = folders.filter(f => !ql || f.toLowerCase().includes(ql)).slice(0, 8);
      sug.innerHTML = '';
      for (const f of matches) {
        sug.appendChild(el('div', {
          class: 'tax-map-suggestion',
          onclick: () => { commitFolder(f); },
        }, [f]));
      }
      if (!matches.length) {
        sug.appendChild(el('div', { class: 'muted small' }, ['Aucun dossier ne correspond']));
      }
    }
    function commitFolder(f) {
      row.folder = f;
      // Choisir un dossier ré-active automatiquement la ligne.
      row.accepted = true;
      pop.remove();
      renderSuggestReview();
    }
    input.addEventListener('input', e => renderInline(e.target.value));
    input.addEventListener('keydown', e => {
      if (e.key === 'Escape') { e.preventDefault(); pop.remove(); }
      if (e.key === 'Enter') {
        e.preventDefault();
        const v = input.value.trim();
        if (v) commitFolder(v);
      }
    });
    pop.appendChild(input);
    pop.appendChild(sug);
    renderInline(input.value);
    document.body.appendChild(pop);
    const rect = anchorEl.getBoundingClientRect();
    const popW = 320;
    pop.style.display = 'block';
    pop.style.position = 'fixed';
    pop.style.zIndex = '4100';
    pop.style.left = Math.min(window.innerWidth - popW - 12, Math.max(12, rect.left)) + 'px';
    pop.style.top = (rect.bottom + 6) + 'px';
    setTimeout(() => { input.focus(); input.select(); }, 30);
    // Ferme au clic extérieur.
    function onDoc(ev) {
      if (!pop.contains(ev.target) && ev.target !== anchorEl) {
        pop.remove();
        document.removeEventListener('mousedown', onDoc, true);
      }
    }
    setTimeout(() => document.addEventListener('mousedown', onDoc, true), 0);
  }

  function updateSuggestApplyBtn() {
    const btn = $('#tax-suggest-apply');
    const rows = state.suggestReview || [];
    const n = rows.filter(r => r.accepted && r.folder).length;
    btn.textContent = `Appliquer les ${n} accepté${n > 1 ? 's' : ''}`;
    btn.disabled = (n === 0);
  }

  // Relance suggest avec use_llm=true sur les seuls thèmes restés unresolved.
  async function applySuggestReview() {
    const rows = state.suggestReview || [];
    const mappings = rows
      .filter(r => r.accepted && r.folder)
      .map(r => ({ theme: r.theme, folder: r.folder }));
    if (!mappings.length) return;
    await withBusy(`Application de ${mappings.length} mapping(s)…`, async () => {
      try {
        const r = await postBulkAddMappings(mappings);
        const added = r.added || [];
        const skipped = r.skipped || [];
        // M = somme des counts des thèmes effectivement ajoutés.
        const addedLower = new Set(added.map(a => a.theme.toLowerCase()));
        let files = 0;
        for (const t of (state.snapshot.themes_llm || [])) {
          if (addedLower.has(t.theme.toLowerCase())) files += (t.count || 0);
        }
        const nAdded = r.n_added != null ? r.n_added : added.length;
        let msg = `✓ ${nAdded} mappé${nAdded > 1 ? 's' : ''}`
                + ` · ${files} fichier${files > 1 ? 's' : ''} au prochain reclassify`;
        if (skipped.length) {
          msg += ` · ${skipped.length} ignoré${skipped.length > 1 ? 's' : ''}`;
        }
        showToast(msg, nAdded > 0 ? 'success' : 'info');
        closeSuggestReview();
        clearBulkLLMSelection();
        state.snapshot = await fetchSnapshot();
        renderAll();
      } catch (e) {
        if (e.status === 423) {
          showToast('✗ Édition verrouillée (run en cours) — réessaie plus tard', 'error');
        } else {
          showToast('✗ ' + e.message, 'error');
        }
      }
    });
  }

  // ── Drag-drop & central mapping handler ──────────────────────────────

  async function onDropOnFolder(e, folder) {
    e.preventDefault();
    // Two kinds of drops:
    //   1. folder onto folder  → move_folder (cascade tree + mappings + FS)
    //   2. theme onto folder   → add_mapping  (theme_mapping write)
    const sourceFolder = e.dataTransfer.getData('application/x-tax-folder');
    if (sourceFolder) {
      await moveFolderViaDrop(sourceFolder, folder);
      return;
    }
    const theme = e.dataTransfer.getData('text/plain');
    if (!theme || folder == null) return;
    await doAddMapping(theme, folder);
  }

  async function moveFolderViaDrop(sourcePath, targetParent) {
    if (!sourcePath || targetParent == null) return;
    // Reject self / descendant / current-parent drops
    if (sourcePath === targetParent) return;
    if (targetParent.startsWith(sourcePath + '/') || targetParent === sourcePath) {
      showToast('✗ Impossible : on ne peut pas déplacer un dossier dans lui-même ou un descendant', 'error');
      return;
    }
    const currentParent = dirname(sourcePath);
    if (currentParent === targetParent) {
      // Dropped on the same parent — silent no-op (user "missed")
      return;
    }
    const sourceName = sourcePath.split('/').pop();
    const newPath = targetParent ? targetParent + '/' + sourceName : sourceName;
    const ok = await showConfirm({
      title: 'Déplacer ce dossier ?',
      body: `${sourcePath}\n→ ${newPath}\n\nLes mappings et fichiers sont déplacés en cascade. Réversible via Annuler.`,
      confirmLabel: 'Déplacer',
    });
    if (!ok) return;
    await withBusy(`Déplacement de « ${sourceName} »…`, async () => {
      try {
        const r = await postMoveFolder(sourcePath, targetParent);
        if (r.unchanged) {
          showToast('Emplacement inchangé', 'info');
          return;
        }
        showToast(
          `✓ Déplacé : ${r.new_path}  ·  ${r.n_tree_entries_renamed} entrée(s) tree, ${r.n_mappings_updated} mapping(s)${_catSuffix(r)}`,
          'success',
        );
        // Rewrite expanded set + selection prefixes (same logic as the
        // move popover handler).
        const newExpanded = new Set();
        for (const p of state.expanded) {
          if (p === sourcePath) newExpanded.add(r.new_path);
          else if (p.startsWith(sourcePath + '/'))
            newExpanded.add(r.new_path + p.slice(sourcePath.length));
          else newExpanded.add(p);
        }
        state.expanded = newExpanded;
        if (state.selection.type) {
          if (state.selection.path === sourcePath
              || state.selection.path.startsWith(sourcePath + '/')) {
            state.selection.path = r.new_path + state.selection.path.slice(sourcePath.length);
          }
        }
        state.filesByPath = new Map();
        state.snapshot = await fetchSnapshot();
        renderAll();
      } catch (err) {
        showToast('✗ ' + err.message, 'error');
      }
    });
  }
  // ── Impact preview modal ─────────────────────────────────────────────

  // Threshold: confirm if > 50 files affected OR any cross-section change.
  function _needsConfirm(p) {
    return (p.n_files_affected > 50) || (p.cross_section_changes > 0);
  }

  function showImpactModal(action, theme, folder, preview) {
    return new Promise(resolve => {
      const modal = $('#tax-impact-modal');
      const body = $('#tax-impact-body');
      const actionLabel = action === 'add' ? 'Ajouter ce mapping'
                        : action === 'update' ? 'Rediriger ce mapping'
                        : 'Supprimer ce mapping';
      const lines = [];
      lines.push(`<div class="tax-impact-action">${actionLabel} : <code>${escapeHtml(theme)}</code>` +
                 (folder ? ` → <code>${escapeHtml(folder)}</code>` : '') + `</div>`);
      lines.push(`<div class="tax-impact-stats">`);
      lines.push(`<div class="tax-impact-stat"><span class="num">${preview.n_files_affected}</span>` +
                 `<span class="lbl">fichier(s) reclassifiés</span></div>`);
      if (preview.cross_section_changes > 0) {
        lines.push(`<div class="tax-impact-stat warn"><span class="num">${preview.cross_section_changes}</span>` +
                   `<span class="lbl">changement(s) de section ⚠</span></div>`);
      }
      lines.push(`</div>`);
      if (preview.examples && preview.examples.length) {
        lines.push(`<div class="tax-impact-examples-title">Exemples :</div>`);
        lines.push(`<ul class="tax-impact-examples">`);
        for (const ex of preview.examples) {
          lines.push(`<li><span class="title">${escapeHtml(ex.title || '(sans titre)')}</span>` +
                     `<div class="path"><code>${escapeHtml(ex.current_dest || '∅')}</code>` +
                     ` → <code>${escapeHtml(ex.new_dest || '∅')}</code></div></li>`);
        }
        lines.push(`</ul>`);
      }
      body.innerHTML = lines.join('');
      modal.style.display = 'flex';
      const cleanup = (ok) => {
        modal.style.display = 'none';
        $('#tax-impact-cancel').onclick = null;
        $('#tax-impact-confirm').onclick = null;
        resolve(ok);
      };
      $('#tax-impact-cancel').onclick = () => cleanup(false);
      $('#tax-impact-confirm').onclick = () => cleanup(true);
    });
  }

  // Wrap any write op: preview → confirmation → action.
  // Règle unique et prévisible : on confirme TOUJOURS (unitaire comme lot),
  // avec l'aperçu d'impact riche. Si l'aperçu échoue, on confirme quand même
  // (jamais d'écriture muette). Returns true if op should proceed.
  async function previewAndConfirm(action, theme, folder) {
    let preview;
    try {
      preview = await withBusy('Analyse de l’impact…',
                               () => fetchPreview(action, theme, folder));
    } catch (e) {
      console.warn('preview failed, confirmation simple:', e);
      const label = action === 'add' ? 'Ajouter' : action === 'update' ? 'Rediriger' : 'Supprimer';
      return await showConfirm({
        title: `${label} le mapping « ${theme} » ?`,
        body: folder ? `→ ${folder}` : '',
        confirmLabel: 'Confirmer',
        cancelLabel: 'Annuler',
      });
    }
    return await showImpactModal(action, theme, folder, preview);
  }

  async function doAddMapping(theme, folder) {
    if (!(await previewAndConfirm('add', theme, folder))) return;
    await withBusy(`Mapping « ${theme} » → ${folder}…`, async () => {
      try {
        await postMapping(theme, folder);
        const themeRec = (state.snapshot.themes_llm || [])
          .find(t => t.theme.toLowerCase() === theme.toLowerCase());
        const impact = themeRec ? themeRec.count : '?';
        showToast(`✓ « ${theme} » → ${folder} · ${impact} fichier(s) au prochain reclassify`, 'success');
        state.snapshot = await fetchSnapshot();
        renderAll();
      } catch (err) {
        showToast('✗ ' + err.message, 'error');
      }
    });
  }
  async function doUpdateMapping(theme, folder) {
    if (!(await previewAndConfirm('update', theme, folder))) return;
    await withBusy(`Mise à jour « ${theme} »…`, async () => {
      try {
        const r = await patchMapping(theme, folder);
        if (r.unchanged) {
          showToast('Mapping inchangé (même cible)', 'info');
          return;
        }
        showToast(`✓ « ${theme} » → ${folder} (avant : ${r.previous_folder})`, 'success');
        state.snapshot = await fetchSnapshot();
        renderAll();
      } catch (err) {
        showToast('✗ ' + err.message, 'error');
      }
    });
  }
  async function confirmDeleteMapping(theme) {
    // Preview always asked for delete (high-impact action by nature)
    let preview;
    try {
      preview = await withBusy('Analyse de l’impact…',
                               () => fetchPreview('delete', theme, null));
    } catch (e) { /* fallback to simple confirm */ }
    if (preview && _needsConfirm(preview)) {
      if (!(await showImpactModal('delete', theme, null, preview))) return;
    } else {
      const ok = await showConfirm({
        title: `Supprimer le mapping « ${theme} » ?`,
        body: "Utilise « Annuler » dans la barre du haut en cas d'erreur.",
        confirmLabel: 'Supprimer',
        variant: 'danger',
      });
      if (!ok) return;
    }
    await withBusy(`Suppression « ${theme} »…`, async () => {
      try {
        const r = await deleteMapping(theme);
        showToast(`✓ « ${theme} » supprimé (était → ${r.previous_folder})`, 'success');
        state.snapshot = await fetchSnapshot();
        renderAll();
      } catch (err) {
        showToast('✗ ' + err.message, 'error');
      }
    });
  }
  // ── Create folder popover ────────────────────────────────────────────

  function openCreateFolderPopover(parent, anchorEl) {
    const pop = $('#tax-newfolder-popover');
    const input = $('#tax-newfolder-input');
    pop.querySelector('.tax-map-popover-title').textContent = 'Créer un sous-dossier';
    pop.querySelector('.tax-map-popover-theme').innerHTML =
      `<span class="muted">dans</span> <code id="tax-newfolder-parent"></code>`;
    $('#tax-newfolder-parent').textContent = parent ? parent : '(racine)';
    $('#tax-newfolder-confirm').textContent = 'Créer';
    input.value = '';
    input.placeholder = 'Nom du nouveau dossier';
    const rect = anchorEl.getBoundingClientRect();
    const popW = 340;
    pop.style.display = 'block';
    pop.style.left = Math.min(window.innerWidth - popW - 12,
                              Math.max(12, rect.right - popW)) + 'px';
    pop.style.top  = (rect.bottom + 8) + 'px';
    pop.dataset.parent = parent;
    pop.dataset.mode = 'create';
    pop.dataset.path = '';
    setTimeout(() => input.focus(), 50);
  }
  function openRenamePopover(path, currentName, anchorEl) {
    const pop = $('#tax-newfolder-popover');
    const input = $('#tax-newfolder-input');
    pop.querySelector('.tax-map-popover-title').textContent = 'Renommer ce dossier';
    pop.querySelector('.tax-map-popover-theme').innerHTML =
      `<span class="muted">renommer</span> <code id="tax-newfolder-parent"></code>`;
    $('#tax-newfolder-parent').textContent = path;
    $('#tax-newfolder-confirm').textContent = 'Renommer';
    input.value = currentName;
    input.placeholder = 'Nouveau nom';
    const rect = anchorEl.getBoundingClientRect();
    const popW = 340;
    pop.style.display = 'block';
    pop.style.left = Math.min(window.innerWidth - popW - 12,
                              Math.max(12, rect.right - popW)) + 'px';
    pop.style.top  = (rect.bottom + 8) + 'px';
    pop.dataset.mode = 'rename';
    pop.dataset.path = path;
    pop.dataset.parent = '';
    setTimeout(() => { input.focus(); input.select(); }, 50);
  }
  function closeCreateFolderPopover() {
    $('#tax-newfolder-popover').style.display = 'none';
  }
  async function confirmCreateFolder() {
    const pop = $('#tax-newfolder-popover');
    const name = $('#tax-newfolder-input').value.trim();
    if (!name) return;
    const mode = pop.dataset.mode || 'create';
    if (mode === 'rename') {
      await doRenameFolder(pop.dataset.path, name);
      return;
    }
    const parent = pop.dataset.parent || '';
    closeCreateFolderPopover();
    await withBusy(`Création « ${name} »…`, async () => {
      try {
        const r = await postCreateFolder(parent, name);
        showToast(`✓ Dossier créé : ${r.path}`, 'success');
        state.snapshot = await fetchSnapshot();
        state.expanded.add(parent);
        renderAll();
      } catch (err) {
        showToast('✗ ' + err.message, 'error');
      }
    });
  }
  // ── Delete folder (with preview + strong confirmation) ──────────────

  async function startDeleteFolder(path) {
    let preview;
    try {
      preview = await withBusy('Analyse du contenu…',
                               () => fetchDeletePreview(path));
    } catch (e) {
      showToast('✗ ' + e.message, 'error');
      return;
    }
    if (preview.is_empty) {
      // Simple cas : dossier vide (FS + tree + mapping)
      const ok = await showConfirm({
        title: 'Supprimer ce dossier vide ?',
        body: path,
        confirmLabel: 'Supprimer',
        variant: 'danger',
      });
      if (!ok) return;
      await withBusy(`Suppression « ${path} »…`, async () => {
        try {
          await deleteFolder(path, false);
          showToast(`✓ Supprimé : ${path}`, 'success');
          await refreshAfterDelete(path);
        } catch (e) {
          showToast('✗ ' + e.message, 'error');
        }
      });
      return;
    }
    // Non-vide → modal détaillé avec dry-run + confirmation par retype
    openDeleteModal(path, preview);
  }

  function openDeleteModal(path, preview) {
    const modal = $('#tax-delete-modal');
    const body = $('#tax-delete-body');
    $('#tax-delete-path').textContent = path;
    const sizeMb = (preview.fs_size_bytes / 1024 / 1024).toFixed(1);
    body.innerHTML =
      `<div class="tax-impact-stats">` +
        `<div class="tax-impact-stat warn"><span class="num">${preview.n_files}</span>` +
          `<span class="lbl">fichier(s) effacé(s) (${sizeMb} MB)</span></div>` +
        `<div class="tax-impact-stat"><span class="num">${preview.n_subfolders}</span>` +
          `<span class="lbl">sous-dossier(s) effacé(s)</span></div>` +
        `<div class="tax-impact-stat"><span class="num">${preview.n_mappings}</span>` +
          `<span class="lbl">mapping(s) cassé(s) (deviendront orphelins)</span></div>` +
      `</div>` +
      `<div class="tax-delete-warn">⚠️ <strong>Action irréversible</strong> côté filesystem.` +
        ` Le bouton Annuler peut restaurer tree.yaml et theme_mapping.yaml mais ` +
        `<strong>PAS</strong> les fichiers PDF supprimés.</div>` +
      `<div class="tax-delete-typecheck">` +
        `Pour confirmer, retape exactement le chemin du dossier :` +
        `<input id="tax-delete-typecheck-input" type="text" autocomplete="off" placeholder="${escapeHtml(path)}">` +
      `</div>`;
    modal.style.display = 'flex';
    const input = $('#tax-delete-typecheck-input');
    const confirmBtn = $('#tax-delete-confirm');
    confirmBtn.disabled = true;
    input.value = '';
    input.addEventListener('input', () => {
      confirmBtn.disabled = (input.value.trim() !== path);
    });
    setTimeout(() => input.focus(), 50);
    const cleanup = (ok) => {
      modal.style.display = 'none';
      $('#tax-delete-cancel').onclick = null;
      confirmBtn.onclick = null;
      if (ok) doDeleteFolderForce(path);
    };
    $('#tax-delete-cancel').onclick = () => cleanup(false);
    confirmBtn.onclick = () => cleanup(true);
  }

  async function doDeleteFolderForce(path) {
    await withBusy(`Suppression « ${path} »…`, async () => {
      try {
        const r = await deleteFolder(path, true);
        showToast(
          `✓ Supprimé : ${path} · ${r.n_files_deleted} fichier(s), ${r.n_mappings_removed} mapping(s) retiré(s)`,
          'success',
        );
        await refreshAfterDelete(path);
      } catch (e) {
        showToast('✗ ' + e.message, 'error');
      }
    });
  }

  async function refreshAfterDelete(path) {
    // Drop expanded/selection state for the deleted subtree
    const newExpanded = new Set();
    for (const p of state.expanded) {
      if (p !== path && !p.startsWith(path + '/')) newExpanded.add(p);
    }
    state.expanded = newExpanded;
    if (state.selection.type) {
      if (state.selection.path === path
          || state.selection.path.startsWith(path + '/')) {
        state.selection = { type: null, path: '' };
      }
    }
    state.filesByPath = new Map();
    state.snapshot = await fetchSnapshot();
    renderAll();
  }

  // ── Adopt folder (hors config → tree.yaml) ──────────────────────────
  // Ajoute un dossier présent sur le disque mais absent de tree.yaml (+ ses
  // parents manquants) à la config, le rendant cible de mapping valide.
  async function adoptFolder(path) {
    const ok = await showConfirm({
      title: 'Adopter « ' + path + ' » ?',
      body: 'Le dossier (et ses parents manquants) sera ajouté à tree.yaml. '
          + 'Il deviendra une cible de mapping valide. Annulable via l\'historique des backups tree.',
      confirmLabel: 'Adopter',
      variant: 'primary',
    });
    if (!ok) return;
    await withBusy(`Adoption « ${path} »…`, async () => {
      try {
        const r = await fetch('/api/taxonomy/folder/adopt', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ profile: state.profile, path: path }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
        showToast('✓ ' + (d.added ? d.added.length : 0) + ' dossier(s) adopté(s)', 'success');
        // Recharge le snapshot + re-render (même pattern que create/delete/move).
        state.snapshot = await fetchSnapshot();
        renderAll();
      } catch (e) {
        showToast('✗ ' + e.message, 'error');
      }
    });
  }

  // ── Move folder popover ──────────────────────────────────────────────

  function openMovePopover(path, anchorEl) {
    const pop = $('#tax-movefolder-popover');
    const input = $('#tax-movefolder-input');
    $('#tax-movefolder-source').textContent = path;
    input.value = '';
    renderMoveSuggestions('', path);
    const rect = anchorEl.getBoundingClientRect();
    const popW = 380;
    pop.style.display = 'block';
    pop.style.left = Math.min(window.innerWidth - popW - 12,
                              Math.max(12, rect.right - popW)) + 'px';
    pop.style.top  = (rect.bottom + 8) + 'px';
    pop.dataset.path = path;
    setTimeout(() => input.focus(), 50);
  }
  function closeMovePopover() {
    $('#tax-movefolder-popover').style.display = 'none';
  }
  function renderMoveSuggestions(query, sourcePath) {
    const MAX = 30;
    const wrap = $('#tax-movefolder-suggestions');
    wrap.innerHTML = '';
    const folders = (state.snapshot && state.snapshot.folders) || [];
    const q = (query || '').toLowerCase().trim();
    // Forbid moving INTO the source itself or any descendant
    const forbidden = (p) => p === sourcePath || p.startsWith(sourcePath + '/');

    // Score each candidate. Score = 0 → filtered out. Forbidden entries
    // keep their score but get a flag so we can render them grayed out.
    //   3 = basename starts with q
    //   2 = basename contains q
    //   1 = path contains q (or no query)
    const scored = [];
    for (const f of folders) {
      const basename = (f.split('/').pop() || '').toLowerCase();
      const path = f.toLowerCase();
      let score = 0;
      if (!q) score = 1;
      else if (basename.startsWith(q)) score = 3;
      else if (basename.includes(q)) score = 2;
      else if (path.includes(q)) score = 1;
      if (score === 0) continue;
      scored.push({ score, path: f, forbidden: forbidden(f) });
    }
    // Sort: allowed first (selectable on top), then score, then path
    scored.sort((a, b) => {
      if (a.forbidden !== b.forbidden) return a.forbidden ? 1 : -1;
      return (b.score - a.score) || a.path.localeCompare(b.path);
    });

    // Always offer "racine" as a possibility (empty parent)
    if (!q || 'racine'.includes(q) || '(racine)'.includes(q)) {
      wrap.appendChild(el('div', {
        class: 'tax-map-suggestion',
        onclick: () => { $('#tax-movefolder-input').value = ''; confirmMovePopover(); },
      }, ['(racine — top-level)']));
    }
    const shown = scored.slice(0, MAX);
    for (const s of shown) {
      if (s.forbidden) {
        // Non-clickable, grayed-out entry with explicit tooltip
        wrap.appendChild(el('div', {
          class: 'tax-map-suggestion forbidden',
          title: `Exclu : ce dossier est à l'intérieur de "${sourcePath}" (déplacement = cycle)`,
        }, [
          el('span', null, [s.path]),
          el('span', { class: 'tax-map-suggestion-badge' }, ['cycle']),
        ]));
      } else {
        wrap.appendChild(el('div', {
          class: 'tax-map-suggestion',
          onclick: () => { $('#tax-movefolder-input').value = s.path; },
        }, [s.path]));
      }
    }
    if (q && scored.length === 0) {
      wrap.appendChild(el('div', { class: 'muted small', style: 'padding:6px 10px;' },
        ['Aucun dossier ne correspond']));
    } else if (scored.length > MAX) {
      wrap.appendChild(el('div', { class: 'muted small', style: 'padding:6px 10px;' },
        [`+${scored.length - MAX} autres résultats — affine la recherche`]));
    }
  }
  async function confirmMovePopover() {
    const pop = $('#tax-movefolder-popover');
    const oldPath = pop.dataset.path;
    const newParent = $('#tax-movefolder-input').value.trim();
    closeMovePopover();
    await withBusy(`Déplacement « ${oldPath} »…`, async () => {
      try {
        const r = await postMoveFolder(oldPath, newParent);
        if (r.unchanged) {
          showToast('Emplacement inchangé', 'info');
          return;
        }
        showToast(
          `✓ Déplacé : ${r.new_path}  ·  ${r.n_tree_entries_renamed} entrée(s) tree, ${r.n_mappings_updated} mapping(s) cascadé(s)${_catSuffix(r)}`,
          'success',
        );
        // Rewrite expanded set + selection prefixes
        const newExpanded = new Set();
        for (const p of state.expanded) {
          if (p === oldPath) newExpanded.add(r.new_path);
          else if (p.startsWith(oldPath + '/')) newExpanded.add(r.new_path + p.slice(oldPath.length));
          else newExpanded.add(p);
        }
        state.expanded = newExpanded;
        if (state.selection.type) {
          if (state.selection.path === oldPath || state.selection.path.startsWith(oldPath + '/')) {
            state.selection.path = r.new_path + state.selection.path.slice(oldPath.length);
          }
        }
        state.filesByPath = new Map();
        state.snapshot = await fetchSnapshot();
        renderAll();
      } catch (err) {
        showToast('✗ ' + err.message, 'error');
      }
    });
  }

  async function doRenameFolder(oldPath, newName) {
    closeCreateFolderPopover();
    await withBusy(`Renommage « ${oldPath} »…`, async () => {
      try {
        const r = await patchRenameFolder(oldPath, newName);
        if (r.unchanged) {
          showToast('Nom inchangé', 'info');
          return;
        }
        showToast(
          `✓ Renommé : ${r.new_path}  ·  ${r.n_tree_entries_renamed} entrée(s) tree, ${r.n_mappings_updated} mapping(s) cascadé(s)${_catSuffix(r)}`,
          'success',
        );
        // Update expanded set: replace old prefix with new
        const newExpanded = new Set();
        for (const p of state.expanded) {
          if (p === oldPath) newExpanded.add(r.new_path);
          else if (p.startsWith(oldPath + '/')) newExpanded.add(r.new_path + p.slice(oldPath.length));
          else newExpanded.add(p);
        }
        state.expanded = newExpanded;
        // Update selection if it was inside the renamed subtree
        if (state.selection.type) {
          if (state.selection.path === oldPath || state.selection.path.startsWith(oldPath + '/')) {
            state.selection.path = r.new_path + state.selection.path.slice(oldPath.length);
          }
        }
        state.filesByPath = new Map();   // file lists are keyed by path
        state.snapshot = await fetchSnapshot();
        renderAll();
      } catch (err) {
        showToast('✗ ' + err.message, 'error');
      }
    });
  }

  async function doUndo() {
    const ok = await showConfirm({
      title: 'Annuler la dernière modification ?',
      body: 'Restauration depuis le backup le plus récent.',
      confirmLabel: 'Annuler la modif',
      cancelLabel: 'Garder',
    });
    if (!ok) return;
    await withBusy('Restauration…', async () => {
      try {
        const r = await postUndo();
        showToast(`↶ Restauré depuis ${r.restored_from}`, 'success');
        state.snapshot = await fetchSnapshot();
        renderAll();
      } catch (err) {
        showToast('✗ ' + err.message, 'error');
      }
    });
  }

  // ── Header stats ─────────────────────────────────────────────────────

  function renderStats() {
    const s = state.snapshot.stats;
    $('#tax-stats').innerHTML =
      `<span><strong>${s.total_files}</strong> fichiers</span>` +
      `<span class="dot"></span><span><strong>${s.tree_nodes}</strong> dossiers</span>` +
      `<span class="dot"></span><span><strong>${s.total_themes_llm}</strong> thèmes LLM</span>` +
      `<span class="dot"></span><span class="orphan-badge"><strong>${s.orphans}</strong> orphelins</span>` +
      `<span class="dot"></span><span><strong>${s.mapped}</strong> mappés</span>`;
    renderUndoState();
  }

  function renderUndoState() {
    const btn = $('#tax-undo');
    const badge = $('#tax-undo-badge');
    const count = (state.snapshot && state.snapshot.stats && state.snapshot.stats.backup_count) || 0;
    if (count > 0) {
      btn.disabled = false;
      btn.title = `Annuler la dernière modification — ${count} étape${count > 1 ? 's' : ''} d'historique disponible${count > 1 ? 's' : ''}`;
      badge.textContent = String(count);
      badge.style.display = '';
    } else {
      btn.disabled = true;
      btn.title = 'Aucune modification à annuler';
      badge.textContent = '';
      badge.style.display = 'none';
    }
  }
  function renderAll() {
    // Le snapshot change (mutation, reload) → la sélection bulk LLM peut
    // référencer des thèmes disparus. On la reset pour rester cohérent
    // (calque le reset per-profil de bulkMappedSelection).
    state.bulkLLMSelection.clear();
    renderStats(); renderTree(); renderTreemap(); renderBreadcrumb();
    renderMappedPanel(); renderLLMPanel(); renderFileSection();
  }

  // ── Init ─────────────────────────────────────────────────────────────

  async function init() {
    const sel = $('#tax-profile-select');
    state.profile = sel.value;
    sel.addEventListener('change', () => withBusy(`Chargement profil « ${sel.value} »…`, async () => {
      state.profile = sel.value;
      state.selection = { type: null, path: '' };
      state.expanded = new Set();
      state.filesByPath = new Map();
      state.selectedMappedTheme = null;   // spotlight is per-profile
      state.treeBulkSelected.clear();     // bulk selection is per-profile
      renderTreeBulkbar();
      await loadAndRender();
    }));
    // Tree bulk delete bar
    const treeBulkDelete = $('#tax-tree-bulkbar-delete');
    if (treeBulkDelete) treeBulkDelete.addEventListener('click', _deleteTreeBulk);
    const treeBulkClear = $('#tax-tree-bulkbar-clear');
    if (treeBulkClear) treeBulkClear.addEventListener('click', _clearTreeBulk);
    $('#tax-refresh').addEventListener('click', () => withBusy('Rechargement…', async () => {
      state.filesByPath = new Map();
      try { state.snapshot = await fetchSnapshot(true); renderAll(); showToast('Snapshot rechargé', 'success'); }
      catch (e) { showToast('Erreur: ' + e.message, 'error'); }
    }));
    $('#tax-undo').addEventListener('click', doUndo);
    $('#tax-audit').addEventListener('click', openAuditModal);
    $('#tax-audit-close').addEventListener('click', closeAuditModal);
    $('#tax-audit-delete').addEventListener('click', confirmAuditDelete);
    $('#tax-reclassify').addEventListener('click', openReclassifyModal);
    $('#tax-reclassify-close').addEventListener('click', closeReclassifyModal);
    $('#tax-history').addEventListener('click', openHistoryModal);
    $('#tax-history-close').addEventListener('click', closeHistoryModal);
    // Suggest + map review modal (Tâche 8)
    $('#tax-suggest-cancel').addEventListener('click', closeSuggestReview);
    $('#tax-suggest-apply').addEventListener('click', applySuggestReview);
    $('#tax-llm-search').addEventListener('input', e => {
      state.search = e.target.value.trim().toLowerCase(); renderLLMPanel();
    });
    $('#tax-llm-orph-only').addEventListener('change', e => {
      state.orphOnly = e.target.checked; renderLLMPanel();
    });
    // Tree search box — filters the tree to nodes matching the query
    const treeSearchInput = $('#tax-tree-search');
    const treeSearchWrap = treeSearchInput.parentElement;
    const treeSearchClear = $('#tax-tree-search-clear');
    let _treeSearchTimer = null;
    function _updateTreeSearchClearVisibility() {
      treeSearchWrap.classList.toggle('has-value', treeSearchInput.value.length > 0);
    }
    treeSearchInput.addEventListener('input', e => {
      clearTimeout(_treeSearchTimer);
      _updateTreeSearchClearVisibility();
      const val = e.target.value;
      _treeSearchTimer = setTimeout(() => {
        state.treeSearch = val;
        renderTree();
      }, 120);
    });
    treeSearchInput.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        e.target.value = '';
        state.treeSearch = '';
        _updateTreeSearchClearVisibility();
        renderTree();
      }
    });
    treeSearchClear.addEventListener('click', () => {
      treeSearchInput.value = '';
      state.treeSearch = '';
      _updateTreeSearchClearVisibility();
      renderTree();
      treeSearchInput.focus();
    });
    // Cross-view navigation: the Catégories sub-tab can dispatch a
    // request to open a file in our tree (e.g. clicking on an entry's
    // matched file). We re-use the same logic as the Theme Files panel's
    // file-click handler.
    document.addEventListener('tax-navigate-file', (e) => {
      const relPath = e.detail && e.detail.rel_path;
      if (!relPath) return;
      openFileFromPath(relPath);
    });
    // Treemap scale toggle (Lissé / Réel)
    document.querySelectorAll('#tax-treemap-scale button').forEach(btn => {
      btn.addEventListener('click', () => {
        state.treemapScale = btn.dataset.scale;
        document.querySelectorAll('#tax-treemap-scale button').forEach(b =>
          b.classList.toggle('active', b.dataset.scale === state.treemapScale));
        renderTreemap();
      });
    });
    $('#tax-map-popover-input').addEventListener('input', e => renderPopoverSuggestions(e.target.value));
    $('#tax-map-popover-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); confirmMapPopover(); }
      if (e.key === 'Escape') closeMapPopover();
    });
    $('#tax-map-popover-cancel').addEventListener('click', closeMapPopover);
    $('#tax-map-popover-confirm').addEventListener('click', confirmMapPopover);
    // Create-folder popover
    $('#tax-newfolder-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); confirmCreateFolder(); }
      if (e.key === 'Escape') closeCreateFolderPopover();
    });
    $('#tax-newfolder-cancel').addEventListener('click', closeCreateFolderPopover);
    $('#tax-newfolder-confirm').addEventListener('click', confirmCreateFolder);
    // Move-folder popover
    $('#tax-movefolder-input').addEventListener('input', e => {
      const pop = $('#tax-movefolder-popover');
      renderMoveSuggestions(e.target.value, pop.dataset.path || '');
    });
    $('#tax-movefolder-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); confirmMovePopover(); }
      if (e.key === 'Escape') closeMovePopover();
    });
    $('#tax-movefolder-cancel').addEventListener('click', closeMovePopover);
    $('#tax-movefolder-confirm').addEventListener('click', confirmMovePopover);
    // Move-file popover (per-file FS op, feature/mapping-file-actions)
    const movefileInput = $('#tax-movefile-input');
    if (movefileInput) {
      movefileInput.addEventListener('input', e => {
        renderMoveFileSuggestions(e.target.value);
        // Clear stale impact when the user re-types
        _moveFileState && (_moveFileState.chosen = null);
        const impactWrap = $('#tax-movefile-impact');
        if (impactWrap) {
          impactWrap.hidden = true;
          impactWrap.innerHTML = '';
        }
        $('#tax-movefile-confirm').disabled = true;
      });
      movefileInput.addEventListener('keydown', e => {
        if (e.key === 'Enter' && _moveFileState && _moveFileState.chosen) {
          e.preventDefault();
          _moveFileConfirm();
        }
        if (e.key === 'Escape') _moveFileCancel();
      });
    }
    const movefileCancel = $('#tax-movefile-cancel');
    if (movefileCancel) movefileCancel.addEventListener('click', _moveFileCancel);
    const movefileConfirm = $('#tax-movefile-confirm');
    if (movefileConfirm) movefileConfirm.addEventListener('click', _moveFileConfirm);
    document.addEventListener('click', e => {
      if (state.popover.open && !e.target.closest('#tax-map-popover')
          && !e.target.classList.contains('tax-llm-map-btn')) closeMapPopover();
      const newFolderPop = $('#tax-newfolder-popover');
      if (newFolderPop.style.display === 'block'
          && !e.target.closest('#tax-newfolder-popover')
          && !e.target.classList.contains('tax-tree-add-btn')) {
        closeCreateFolderPopover();
      }
      const movePop = $('#tax-movefolder-popover');
      if (movePop.style.display === 'block'
          && !e.target.closest('#tax-movefolder-popover')
          && !e.target.classList.contains('tax-tree-move-btn')) {
        closeMovePopover();
      }
    });
    window.addEventListener('resize', () => { renderTreemap(); });
    // Keyboard shortcuts for viewer pagination (Cmd/Ctrl + ← / →)
    document.addEventListener('keydown', e => {
      if (state.selection.type !== 'file') return;
      if (!(e.metaKey || e.ctrlKey)) return;
      // Avoid hijacking when an input/textarea is focused
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); goToPage(state.viewerCurrentPage - 1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); goToPage(state.viewerCurrentPage + 1); }
    });
    await loadAndRender();
  }
  async function loadAndRender() {
    $('#tax-stats').innerHTML = '<span class="muted">Chargement…</span>';
    try { state.snapshot = await fetchSnapshot(); renderAll(); }
    catch (e) { $('#tax-stats').innerHTML = `<span class="text-error">Erreur: ${escapeHtml(e.message)}</span>`; }
  }
  document.addEventListener('DOMContentLoaded', init);
})();
