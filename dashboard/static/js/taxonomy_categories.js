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

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, ...body }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    return data;
  }
  async function patchJSON(url, body) {
    const r = await fetch(url, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, ...body }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    return data;
  }
  async function deleteJSON(url, body) {
    const r = await fetch(url, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: state.profile, ...body }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    return data;
  }

  // ── Toast + busy overlay (minimal stand-ins) ─────────────────────────
  // taxonomy.js has its own implementations inside its IIFE — we can't
  // reach them. Reuse the SAME DOM nodes (#tax-toast, #tax-busy) so the
  // UX is consistent across both views.

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
      showToast('Action trop longue — vérifie l’état', 'error');
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

  // Refresh snapshot + re-render. Selection is preserved if the path
  // still exists; otherwise dropped.
  async function reloadAfterWrite() {
    state.snapshot = await fetchSnapshot(true);
    if (state.selectedEntry) {
      const e = findEntry(state.selectedEntry.group,
                          state.selectedEntry.chemin);
      if (!e) state.selectedEntry = null;
    }
    renderAll();
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
      }, [
        el('span', {
          class: 'tax-cat-group-title',
          onclick: () => {
            if (state.collapsedGroups.has(g.group)) state.collapsedGroups.delete(g.group);
            else state.collapsedGroups.add(g.group);
            renderTree();
          },
        }, [
          el('span', { class: 'tax-cat-group-chevron' }, [collapsed ? '▶' : '▼']),
          ' ', g.group, ' ',
          el('span', { class: 'muted small' }, [`(${g.n_entries})`]),
        ]),
        el('button', {
          class: 'tax-cat-group-add',
          title: `Créer une entry dans « ${g.group} »`,
          onclick: e => { e.stopPropagation(); openAddEntryPopover(g.group); },
        }, ['+']),
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
    // "New group" footer action
    wrap.appendChild(el('div', { class: 'tax-cat-newgroup' }, [
      el('button', {
        class: 'btn-secondary',
        onclick: () => openAddEntryPopover(null),
      }, ['+ Créer une entry dans un nouveau groupe']),
    ]));
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
    // Actions
    wrap.appendChild(el('div', { class: 'tax-cat-field', style: 'border-bottom:none;' }, [
      el('label', null, ['Actions']),
      el('button', {
        class: 'btn-secondary tax-cat-action',
        onclick: () => openRenamePopover(state.selectedEntry.group, e.chemin),
      }, ['Renommer chemin']),
      ' ',
      el('button', {
        class: 'btn-secondary tax-cat-action',
        onclick: () => openPriorityPopover(state.selectedEntry.group,
                                            e.chemin, e.priorite),
      }, ['Modifier priorité']),
      ' ',
      el('button', {
        class: 'btn-danger tax-cat-action',
        onclick: () => deleteSelectedEntry(),
      }, ['Supprimer entry']),
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
    // Add-keyword input — Enter validates, button also works
    const input = el('input', {
      type: 'text',
      placeholder: 'ajouter un mot-clé… puis Entrée',
    });
    const addBtn = el('button', { class: 'btn-primary' }, ['+ Ajouter']);
    const submitKeyword = () => {
      const value = input.value.trim();
      if (!value) return;
      input.value = '';
      addKeywordHandler(state.selectedEntry.group, e.chemin, value);
    };
    input.addEventListener('keydown', ev => {
      if (ev.key === 'Enter') { ev.preventDefault(); submitKeyword(); }
    });
    addBtn.addEventListener('click', submitKeyword);
    wrap.appendChild(el('div', { class: 'tax-cat-kw-add' }, [input, addBtn]));
    if (e.n_keywords === 0) {
      wrap.appendChild(el('div', {
        class: 'muted small',
        style: 'padding:20px;text-align:center;',
      }, ['⚠ Aucun mot-clé — cette entry ne classifiera rien.']));
      return;
    }
    const list = el('div', { class: 'tax-cat-kw-list' });
    for (const k of e.mots_cles) {
      const chip = el('span', { class: 'tax-cat-kw' }, [
        k, ' ',
        el('span', {
          class: 'tax-cat-kw-x',
          title: `Retirer « ${k} »`,
          onclick: ev => {
            ev.stopPropagation();
            deleteKeywordHandler(state.selectedEntry.group, e.chemin, k);
          },
        }, ['×']),
      ]);
      list.appendChild(chip);
    }
    wrap.appendChild(list);
    wrap.appendChild(el('div', {
      class: 'muted small',
      style: 'padding:10px 14px;border-top:1px solid var(--border, #30363d);',
    }, [
      '💡 Phase C ajoutera la détection des mots-clés qu\'aucun fichier de la lib ne contient.',
    ]));
  }

  // ── Write handlers ───────────────────────────────────────────────────

  async function addKeywordHandler(group, chemin, keyword) {
    await withBusy(`Ajout « ${keyword} »…`, async () => {
      try {
        const r = await postJSON('/api/categories/entry/keyword',
                                 { group, chemin, keyword });
        if (r.unchanged) {
          showToast('Mot-clé déjà présent (dedup)', 'info');
        } else {
          showToast(`✓ « ${keyword} » ajouté`, 'success');
        }
        await reloadAfterWrite();
      } catch (e) {
        showToast('✗ ' + e.message, 'error');
      }
    });
  }

  async function deleteKeywordHandler(group, chemin, keyword) {
    await withBusy(`Suppression « ${keyword} »…`, async () => {
      try {
        await deleteJSON('/api/categories/entry/keyword',
                         { group, chemin, keyword });
        showToast(`✓ « ${keyword} » retiré`, 'success');
        await reloadAfterWrite();
      } catch (e) {
        showToast('✗ ' + e.message, 'error');
      }
    });
  }

  // ── Inline mini-popover for path / priority / new-entry ──────────────
  // Reuses the dashboard's "showConfirm-like" pattern but for a single
  // text input. Simpler than spawning a full template-bound modal.

  function showPrompt({title, label, initial, validator, confirmLabel}) {
    return new Promise((resolve) => {
      const back = document.createElement('div');
      back.className = 'tax-modal-backdrop';
      back.style.display = 'flex';
      const modal = el('div', { class: 'tax-modal', style: 'width:480px;' }, [
        el('div', { class: 'tax-modal-title' }, [title]),
        el('div', { class: 'tax-modal-body', style: 'padding: 14px 16px;' }, [
          el('label', { style: 'display:block; font-size:11px; color: var(--text-muted); margin-bottom:6px;' },
                     [label || '']),
        ]),
      ]);
      const input = el('input', {
        type: 'text',
        style: 'width:100%; background: var(--bg, #0d1117); border: 1px solid var(--border, #30363d); '
             + 'border-radius:4px; padding:6px 10px; color: var(--text, #c9d1d9); font-size:13px;',
      });
      input.value = (initial != null) ? String(initial) : '';
      const err = el('div', { class: 'muted small', style: 'margin-top:6px; min-height:14px;' }, ['']);
      modal.querySelector('.tax-modal-body').appendChild(input);
      modal.querySelector('.tax-modal-body').appendChild(err);
      const cancelBtn = el('button', { class: 'btn-secondary' }, ['Annuler']);
      const okBtn = el('button', { class: 'btn-primary' }, [confirmLabel || 'OK']);
      modal.appendChild(el('div', { class: 'tax-modal-actions' }, [cancelBtn, okBtn]));
      back.appendChild(modal);
      document.body.appendChild(back);
      setTimeout(() => input.focus(), 50);

      function cleanup(val) {
        back.remove();
        document.removeEventListener('keydown', onKey);
        resolve(val);
      }
      function tryOk() {
        const value = input.value.trim();
        if (validator) {
          const msg = validator(value);
          if (msg) { err.textContent = msg; return; }
        }
        cleanup(value);
      }
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); cleanup(null); }
        if (e.key === 'Enter')  { e.preventDefault(); tryOk(); }
      }
      cancelBtn.addEventListener('click', () => cleanup(null));
      okBtn.addEventListener('click', tryOk);
      document.addEventListener('keydown', onKey);
    });
  }

  async function openRenamePopover(group, chemin) {
    const value = await showPrompt({
      title: 'Renommer le chemin de cette entry',
      label: `Nouveau chemin (group « ${group} »)`,
      initial: chemin,
      confirmLabel: 'Renommer',
      validator: v => v ? null : 'chemin vide',
    });
    if (!value || value === chemin) return;
    await withBusy('Renommage…', async () => {
      try {
        await patchJSON('/api/categories/entry',
                        { group, chemin, new_chemin: value });
        showToast(`✓ Renommé : ${value}`, 'success');
        state.selectedEntry = { group, chemin: value };
        await reloadAfterWrite();
      } catch (e) {
        showToast('✗ ' + e.message, 'error');
      }
    });
  }

  async function openPriorityPopover(group, chemin, current) {
    const value = await showPrompt({
      title: 'Modifier la priorité de cette entry',
      label: 'Priorité (1 = haute, 99 = basse)',
      initial: current,
      confirmLabel: 'Modifier',
      validator: v => {
        const n = parseInt(v, 10);
        if (isNaN(n) || n < 1 || n > 99) return 'priorité hors plage (1-99)';
        return null;
      },
    });
    if (value == null) return;
    const n = parseInt(value, 10);
    if (n === current) return;
    await withBusy('Mise à jour priorité…', async () => {
      try {
        await patchJSON('/api/categories/entry',
                        { group, chemin, new_priorite: n });
        showToast(`✓ Priorité : P${n}`, 'success');
        await reloadAfterWrite();
      } catch (e) {
        showToast('✗ ' + e.message, 'error');
      }
    });
  }

  async function deleteSelectedEntry() {
    const sel = state.selectedEntry;
    if (!sel) return;
    const e = findEntry(sel.group, sel.chemin);
    if (!e) return;
    const value = await showPrompt({
      title: `Supprimer cette entry ?`,
      label: `Tape « SUPPRIMER » pour confirmer la suppression de « ${sel.chemin} » (${e.n_keywords} mot(s)-clé(s)).`,
      initial: '',
      confirmLabel: 'Supprimer',
      validator: v => v === 'SUPPRIMER' ? null : 'tape exactement SUPPRIMER',
    });
    if (value !== 'SUPPRIMER') return;
    await withBusy('Suppression…', async () => {
      try {
        await deleteJSON('/api/categories/entry',
                         { group: sel.group, chemin: sel.chemin });
        showToast(`✓ Entry supprimée`, 'success');
        state.selectedEntry = null;
        await reloadAfterWrite();
      } catch (err) {
        showToast('✗ ' + err.message, 'error');
      }
    });
  }

  async function openAddEntryPopover(group) {
    // Phase B: a 2-field mini-form via two prompts. Phase B+ could give
    // it a richer modal. We keep it minimal but functional.
    let g = group;
    if (g == null) {
      g = await showPrompt({
        title: 'Créer une entry — choisir le groupe',
        label: 'Nom du groupe (existant ou nouveau, ex: informatique)',
        initial: '',
        confirmLabel: 'Suivant →',
        validator: v => v ? null : 'nom de groupe requis',
      });
      if (!g) return;
    }
    const chemin = await showPrompt({
      title: `Créer une entry dans « ${g} »`,
      label: 'Chemin cible (ex: 02-INFORMATIQUE/05-IA-ML/Computer-Vision)',
      initial: '',
      confirmLabel: 'Créer',
      validator: v => v ? null : 'chemin requis',
    });
    if (!chemin) return;
    await withBusy(`Création de l'entry…`, async () => {
      try {
        await postJSON('/api/categories/entry', {
          group: g, chemin, priorite: 5, mots_cles: [],
        });
        showToast(`✓ Entry créée : ${chemin}`, 'success');
        state.selectedEntry = { group: g, chemin };
        // Ensure the group is expanded so the new entry is visible
        state.collapsedGroups.delete(g);
        await reloadAfterWrite();
      } catch (err) {
        showToast('✗ ' + err.message, 'error');
      }
    });
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

  // Hijack the shared "Annuler" button at the capture phase so that when
  // the Catégories sub-tab is active it triggers categories.undo()
  // instead of theme_mapping.undo(). taxonomy.js owns the same button.
  function bindUndoInterceptor() {
    const undoBtn = document.querySelector('#tax-undo');
    if (!undoBtn) return;
    undoBtn.addEventListener('click', async (e) => {
      const active = document.querySelector('.tax-subtab.active');
      if (!active || active.dataset.view !== 'categories') return; // let main handler run
      e.stopPropagation();
      e.preventDefault();
      await withBusy('Restauration…', async () => {
        try {
          const r = await postJSON('/api/categories/undo', {});
          showToast(`↶ Restauré (${r.restored_from})`, 'success');
          await reloadAfterWrite();
        } catch (err) {
          showToast('✗ ' + err.message, 'error');
        }
      });
    }, true);  // capture phase = runs before the bubble-phase mapping handler
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
    // Wire the info banner toggle
    const infoBtn = document.querySelector('.tax-cat-info-toggle');
    const infoBody = document.querySelector('.tax-cat-info-body');
    if (infoBtn && infoBody) {
      infoBtn.addEventListener('click', () => {
        const open = infoBody.hidden;
        infoBody.hidden = !open;
        infoBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
        infoBtn.classList.toggle('open', open);
      });
    }
    // React to profile changes: drop our cache and reload if active
    sel.addEventListener('change', () => {
      state.profile = sel.value;
      state.snapshot = null;
      state.selectedEntry = null;
      state.loaded = false;
      const active = document.querySelector('.tax-subtab.active');
      if (active && active.dataset.view === 'categories') loadAndRender();
    });
    bindUndoInterceptor();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
