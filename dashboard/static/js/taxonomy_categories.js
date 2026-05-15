/* Taxonomy / Catégories sub-tab — Phase A (read-only).
 *
 * Renders the parsed `categories.yaml` for the active profile in a
 * 3-column layout: groups+entries / entry detail / keyword chips.
 *
 * Phase A scope: just inspection. No add/edit/delete — the action
 * buttons render disabled and the chips have no × handler. Writes
 * come in Phase B.
 */
(function () {
  'use strict';

  // ── Tiny helpers (independent from taxonomy.js to avoid cross-IIFE) ──

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
    snapshot: null,
    selectedEntry: null,       // { group, chemin }
    collapsedGroups: new Set(),// group names currently collapsed
    loaded: false,
  };

  // ── API ──────────────────────────────────────────────────────────────

  async function fetchSnapshot(force) {
    const url = `/api/categories/snapshot?profile=${encodeURIComponent(state.profile)}${force ? '&force=true' : ''}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error('snapshot HTTP ' + r.status);
    return r.json();
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
    // Stats banner switches between the two
    const sMap = $('#tax-stats');
    const sCat = $('#tax-stats-categories');
    if (sMap) sMap.style.display = (view === 'mappings') ? '' : 'none';
    if (sCat) sCat.style.display = (view === 'categories') ? '' : 'none';
    if (view === 'categories' && !state.loaded) {
      loadAndRender();
    }
  }

  // ── Header stats ─────────────────────────────────────────────────────

  function renderStats() {
    const s = state.snapshot && state.snapshot.stats;
    const el = $('#tax-stats-categories');
    if (!el) return;
    if (!state.snapshot || !state.snapshot.exists) {
      el.innerHTML = '<span class="muted">Pas de categories.yaml dans ce profil</span>';
      return;
    }
    el.innerHTML =
      `<span><strong>${s.n_groups}</strong> catégories</span>` +
      `<span class="dot"></span><span><strong>${s.n_entries}</strong> entries</span>` +
      `<span class="dot"></span><span><strong>${s.n_keywords}</strong> mots-clés</span>` +
      `<span class="dot"></span><span><strong>${s.avg_keywords}</strong> mots-clés/entry</span>`;
  }

  // ── Column 1 — tree (groups + entries) ──────────────────────────────

  function renderTree() {
    const wrap = $('#tax-cat-tree');
    const sub = $('#tax-cat-tree-sub');
    wrap.innerHTML = '';
    if (!state.snapshot || !state.snapshot.exists) {
      wrap.appendChild(el('div', { class: 'muted small', style: 'padding:30px;text-align:center;' },
        ['Aucun categories.yaml — créez le fichier pour démarrer.']));
      sub.textContent = '';
      return;
    }
    const s = state.snapshot.stats;
    sub.textContent = `${s.n_groups} groupes · ${s.n_entries} entries`;
    for (const g of state.snapshot.groups) {
      const collapsed = state.collapsedGroups.has(g.group);
      const header = el('div', {
        class: 'tax-cat-group' + (collapsed ? ' collapsed' : ''),
        onclick: () => {
          if (state.collapsedGroups.has(g.group)) state.collapsedGroups.delete(g.group);
          else state.collapsedGroups.add(g.group);
          renderTree();
        },
      }, [
        el('span', { class: 'tax-cat-group-chevron' }, [collapsed ? '▶' : '▼']),
        ' ', g.group, ' ',
        el('span', { class: 'muted small' }, [`(${g.n_entries})`]),
      ]);
      wrap.appendChild(header);
      if (collapsed) continue;
      for (const e of g.entries) {
        const isSel = state.selectedEntry
                   && state.selectedEntry.group === g.group
                   && state.selectedEntry.chemin === e.chemin;
        const isDormant = e.n_keywords === 0;
        wrap.appendChild(el('div', {
          class: 'tax-cat-entry'
                 + (isSel ? ' selected' : '')
                 + (isDormant ? ' dormant' : ''),
          title: e.chemin,
          onclick: () => selectEntry(g.group, e.chemin),
        }, [
          el('span', { class: 'tax-cat-entry-path' }, [shortenPath(e.chemin)]),
          el('span', { class: 'tax-cat-entry-prio', title: 'Priorité' },
                     ['P' + e.priorite]),
        ]));
      }
    }
  }

  function shortenPath(p) {
    // Drop the top-level segment so entries read shorter (it's already
    // grouped by category above). Fallback to full path if not splittable.
    const parts = p.split('/');
    if (parts.length <= 1) return p;
    return parts.slice(1).join('/');
  }

  // ── Column 2 — entry detail ─────────────────────────────────────────

  function findEntry(group, chemin) {
    if (!state.snapshot) return null;
    const g = state.snapshot.groups.find(x => x.group === group);
    if (!g) return null;
    return g.entries.find(e => e.chemin === chemin) || null;
  }

  function selectEntry(group, chemin) {
    state.selectedEntry = { group, chemin };
    renderTree();
    renderDetail();
    renderKeywords();
  }

  function renderDetail() {
    const wrap = $('#tax-cat-detail');
    const sub = $('#tax-cat-detail-sub');
    wrap.innerHTML = '';
    if (!state.selectedEntry) {
      sub.textContent = 'Sélectionne une entry';
      wrap.appendChild(el('div', { class: 'muted small', style: 'padding:30px;text-align:center;' },
        ['Aucune entry sélectionnée.']));
      return;
    }
    const e = findEntry(state.selectedEntry.group, state.selectedEntry.chemin);
    if (!e) {
      sub.textContent = '(introuvable)';
      return;
    }
    sub.textContent = `${state.selectedEntry.group} → ${shortenPath(e.chemin)}`;
    // Field: chemin
    wrap.appendChild(el('div', { class: 'tax-cat-field' }, [
      el('label', null, ['Chemin cible']),
      el('div', { class: 'value' }, [e.chemin]),
    ]));
    // Field: priorité (read-only in phase A)
    wrap.appendChild(el('div', { class: 'tax-cat-field' }, [
      el('label', null, ['Priorité']),
      el('div', null, [
        el('span', { class: 'tax-cat-prio-badge' }, ['P' + e.priorite]),
        ' ',
        el('span', { class: 'muted small' },
                  ['(plus bas = plus prioritaire si plusieurs catégories matchent)']),
      ]),
    ]));
    // Field: keyword counts
    wrap.appendChild(el('div', { class: 'tax-cat-field' }, [
      el('label', null, ['Couverture']),
      el('div', { class: 'tax-cat-stats' }, [
        el('span', null, [
          el('strong', null, [String(e.n_keywords)]), ' mot-clé(s)',
        ]),
      ]),
    ]));
    // Phase A: actions are visible but disabled
    wrap.appendChild(el('div', { class: 'tax-cat-field', style: 'border-bottom:none;' }, [
      el('label', null, ['Actions']),
      el('div', { class: 'muted small', style: 'margin-bottom:6px;' },
                 ['Édition disponible en Phase B.']),
      el('button', { class: 'btn-secondary tax-cat-action', disabled: true,
                     title: 'Disponible en Phase B' },
                   ['Renommer chemin']),
      ' ',
      el('button', { class: 'btn-secondary tax-cat-action', disabled: true,
                     title: 'Disponible en Phase B' },
                   ['Modifier priorité']),
      ' ',
      el('button', { class: 'btn-danger tax-cat-action', disabled: true,
                     title: 'Disponible en Phase B' },
                   ['Supprimer entry']),
    ]));
  }

  // ── Column 3 — keywords (chips) ─────────────────────────────────────

  function renderKeywords() {
    const wrap = $('#tax-cat-kw');
    const sub = $('#tax-cat-kw-sub');
    wrap.innerHTML = '';
    if (!state.selectedEntry) {
      sub.textContent = '';
      wrap.appendChild(el('div', { class: 'muted small', style: 'padding:30px;text-align:center;' },
        ['Sélectionne une entry à gauche.']));
      return;
    }
    const e = findEntry(state.selectedEntry.group, state.selectedEntry.chemin);
    if (!e) return;
    sub.textContent = `${e.n_keywords} mot-clé(s)`;
    // Phase A: add input + × buttons are present but disabled / non-functional
    wrap.appendChild(el('div', { class: 'tax-cat-kw-add' }, [
      el('input', { type: 'text', placeholder: 'ajouter un mot-clé… (Phase B)',
                    disabled: true }),
      el('button', { class: 'btn-primary', disabled: true,
                     title: 'Disponible en Phase B' }, ['+ Ajouter']),
    ]));
    if (e.n_keywords === 0) {
      wrap.appendChild(el('div', {
        class: 'muted small',
        style: 'padding:20px;text-align:center;',
      }, ['⚠ Aucun mot-clé — cette entry ne classifiera rien.']));
      return;
    }
    const list = el('div', { class: 'tax-cat-kw-list' });
    for (const k of e.mots_cles) {
      list.appendChild(el('span', { class: 'tax-cat-kw' }, [
        k, ' ',
        el('span', { class: 'tax-cat-kw-x', title: 'Disponible en Phase B' }, ['×']),
      ]));
    }
    wrap.appendChild(list);
    wrap.appendChild(el('div', {
      class: 'muted small',
      style: 'padding:10px 14px;border-top:1px solid var(--border, #30363d);',
    }, [
      '💡 Phase C ajoutera la détection des mots-clés qu\'aucun fichier de la lib ne contient.',
    ]));
  }

  // ── Init ─────────────────────────────────────────────────────────────

  function renderAll() {
    renderStats();
    renderTree();
    renderDetail();
    renderKeywords();
  }

  async function loadAndRender() {
    try {
      state.snapshot = await fetchSnapshot();
      state.loaded = true;
      renderAll();
    } catch (e) {
      const wrap = $('#tax-cat-tree');
      if (wrap) {
        wrap.innerHTML = '';
        wrap.appendChild(el('div', { class: 'error', style: 'padding:20px;' },
          ['✗ ' + e.message]));
      }
    }
  }

  function init() {
    // Find profile from the main page's select
    const sel = document.querySelector('#tax-profile-select');
    if (!sel) return;
    state.profile = sel.value;
    // Wire sub-tab buttons
    document.querySelectorAll('.tax-subtab').forEach(b => {
      b.addEventListener('click', () => activateSubtab(b.dataset.view));
    });
    // React to profile changes: drop our cache and reload if active
    sel.addEventListener('change', () => {
      state.profile = sel.value;
      state.snapshot = null;
      state.selectedEntry = null;
      state.loaded = false;
      const active = document.querySelector('.tax-subtab.active');
      if (active && active.dataset.view === 'categories') loadAndRender();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
