// Sous-onglet « Aperçu » de Taxonomie : arbre imbriqué Maintenant/Après.
// Squelette CONSTANT = tree_folders ∪ dossiers réels (current ∪ predicted) ;
// seuls les fichiers se déplacent selon le mode. Aucun déplacement disque.
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const st = { loaded: false, mode: 'now', proj: null, expanded: new Set(), selected: null, profile: null };

  function profileName() {
    const sel = $('tax-profile-select');
    return (sel && sel.value) || new URLSearchParams(location.search).get('profile') || 'default';
  }
  function finalFolder(f) {
    return (f.predicted_folder && f.predicted_folder !== f.current_folder)
      ? f.predicted_folder : (f.predicted_folder || f.current_folder);
  }
  function folderOf(f) { return st.mode === 'now' ? f.current_folder : finalFolder(f); }

  function buildTree(folderSet, countByFolder) {
    const folders = Array.from(folderSet);
    function childrenOf(prefix) {
      const out = [];
      if (!prefix) { for (const f of folders) if (f && f.indexOf('/') === -1) out.push(f); }
      else { const pref = prefix + '/';
        for (const f of folders) if (f.startsWith(pref) && f.slice(pref.length).indexOf('/') === -1) out.push(f); }
      return out.sort();
    }
    function node(path) {
      return { name: path ? path.split('/').pop() : 'racine', path,
               count: countByFolder[path] || 0, children: childrenOf(path).map(node) };
    }
    return node('');
  }
  function skeleton(p) {
    const s = new Set(p.tree_folders || []);
    for (const f of p.files) { s.add(f.current_folder); if (f.predicted_folder) s.add(f.predicted_folder); }
    return s;
  }

  function load() {
    st.profile = profileName();
    $('expl-loading').style.display = '';
    $('expl-loading').textContent = 'Calcul de la projection…';
    fetch('/api/explorer/projection?profile=' + encodeURIComponent(st.profile))
      .then((r) => r.json()).then((d) => {
        if (d.status === 'ready') { st.proj = d; $('expl-loading').style.display = 'none'; render(); }
        else if (d.status === 'building') { poll(); }
        else { $('expl-loading').textContent = '✗ ' + (d.error || 'erreur'); }
      }).catch((e) => { $('expl-loading').textContent = '✗ Erreur réseau : ' + (e.message || e); });
  }
  function poll() {
    fetch('/api/explorer/status?profile=' + encodeURIComponent(st.profile))
      .then((r) => r.json()).then((s) => {
        if (s.status === 'ready') { load(); }
        else if (s.status === 'error') { $('expl-loading').textContent = '✗ ' + (s.error || 'erreur'); }
        else { $('expl-loading').textContent = `Calcul… ${s.n_done || 0}/${s.n_total || '?'} fichiers`; setTimeout(poll, 1000); }
      }).catch((e) => { $('expl-loading').textContent = '✗ Erreur réseau : ' + (e.message || e); });
  }

  function render() {
    const p = st.proj; if (!p) return;
    const s = p.summary;
    $('expl-counter').textContent = `${s.n_moving} bougeraient · ${s.n_total} fichiers`
      + (s.n_unanalyzed ? ` · ${s.n_unanalyzed} non analysés (Vision)` : '');
    $('expl-apply').style.display = (st.mode === 'after' && s.n_moving > 0) ? '' : 'none';
    const countByFolder = {};
    for (const f of p.files) { const k = folderOf(f); countByFolder[k] = (countByFolder[k] || 0) + 1; }
    const root = buildTree(skeleton(p), countByFolder);
    const tree = $('expl-tree'); tree.innerHTML = ''; tree.appendChild(renderNode(root, 0));
    renderFiles();
  }

  function renderNode(node, depth) {
    const isRoot = !node.path;
    const hasKids = node.children && node.children.length > 0;
    const isExp = isRoot || st.expanded.has(node.path);
    const row = document.createElement('div');
    row.className = 'tax-tree-row' + (node.path === st.selected ? ' selected' : '');
    row.style.paddingLeft = (depth * 14 + 6) + 'px';
    const chev = document.createElement('span');
    if (hasKids) {
      chev.className = 'tax-tree-chevron' + (isExp ? ' expanded' : '');
      chev.textContent = isExp ? '▼' : '▶';
      chev.onclick = (e) => { e.stopPropagation(); toggle(node.path); };
    } else { chev.className = 'tax-tree-chevron-empty'; }
    row.appendChild(chev);
    const icon = document.createElement('span'); icon.className = 'tax-tree-icon'; icon.textContent = '📁'; row.appendChild(icon);
    const name = document.createElement('span'); name.className = 'tax-tree-name'; name.textContent = node.name; row.appendChild(name);
    const cnt = document.createElement('span'); cnt.className = 'tax-tree-count'; cnt.textContent = String(node.count); row.appendChild(cnt);
    row.onclick = () => { st.selected = node.path; render(); };
    const wrap = document.createElement('div'); wrap.className = 'tax-tree-node'; wrap.appendChild(row);
    if (isExp && hasKids) {
      const kids = document.createElement('div'); kids.className = 'tax-tree-children';
      for (const c of node.children) kids.appendChild(renderNode(c, depth + 1));
      wrap.appendChild(kids);
    }
    return wrap;
  }
  function toggle(path) { st.expanded.has(path) ? st.expanded.delete(path) : st.expanded.add(path); render(); }

  const PAGE = 200;
  function renderFiles() {
    const p = st.proj; const panel = $('expl-files');
    if (st.selected == null) { panel.innerHTML = '<div class="muted small">Sélectionne un dossier dans l\'arbre.</div>'; return; }
    const list = p.files.filter((f) => folderOf(f) === st.selected);
    const slice = list.slice(0, PAGE);
    panel.innerHTML = `<div class="muted small">${st.selected || '(racine)'} — ${list.length} fichier(s)</div>`
      + slice.map((f) => {
        const moved = f.predicted_folder && f.predicted_folder !== f.current_folder;
        const prov = (st.mode === 'after' && moved) ? ` <span class="muted small">← ${f.current_folder || '(racine)'}</span>` : '';
        const sig = f.signal ? ` <span class="muted small">[${f.signal}]</span>` : '';
        return `<div class="expl-file" style="padding:2px 4px;">${f.rel_path.split('/').pop()}${prov}${sig}</div>`;
      }).join('')
      + (list.length > PAGE ? `<div class="muted small">+${list.length - PAGE} autres…</div>` : '');
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('#expl-mode-toggle button').forEach((b) => {
      b.onclick = () => {
        st.mode = b.dataset.mode;
        document.querySelectorAll('#expl-mode-toggle button').forEach((x) => x.classList.toggle('active', x === b));
        render();
      };
    });
    const rf = $('expl-refresh');
    if (rf) rf.onclick = () => fetch('/api/explorer/refresh?profile=' + encodeURIComponent(profileName()), { method: 'POST' })
      .then(() => { st.proj = null; load(); }).catch(() => {});
    const ap = $('expl-apply');
    if (ap) ap.onclick = () => {
      if (!st.proj) return;
      window.applyReclassify({ profile: st.profile, keyword: !!st.proj.flag_keyword,
        onStatus: (m) => { $('expl-apply-status').textContent = m; } })
        .then(() => { st.proj = null; load(); })
        .catch((e) => { $('expl-apply-status').textContent = '✗ ' + (e.message || e); });
    };
    const tabBtn = document.querySelector('.tax-subtab[data-view="explorer"]');
    if (tabBtn) tabBtn.addEventListener('click', () => { if (!st.loaded) { st.loaded = true; load(); } });
  });
})();
