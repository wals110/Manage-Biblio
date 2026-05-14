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
    return r.json();
  }
  async function fetchFiles(path, offset, limit) {
    const url = `/api/taxonomy/folder/files?profile=${encodeURIComponent(state.profile)}` +
                `&path=${encodeURIComponent(path)}&offset=${offset}&limit=${limit}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error('files HTTP ' + r.status);
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

  // ── Tree (column 1) ──────────────────────────────────────────────────

  function renderTree() {
    const root = state.snapshot.tree;
    const container = $('#tax-tree');
    container.innerHTML = '';
    container.appendChild(renderTreeNode(root, 0));
    $('#tax-tree-sub').textContent =
      `${state.snapshot.stats.tree_nodes} dossiers · ${state.snapshot.stats.total_files} fichiers`;
  }

  function renderTreeNode(node, depth) {
    const isRoot = !node.path;
    const isExpanded = state.expanded.has(node.path) || isRoot;
    const hasChildren = node.children && node.children.length > 0;
    const hasFiles = (node.file_count || 0) > 0;
    const isExpandable = hasChildren || hasFiles;
    const isSelected = state.selection.type === 'folder' && state.selection.path === node.path;
    const touched = touchedFoldersSet();
    const ancestors = touchedAncestorsSet(touched);
    const isTouched = touched.has(node.path);
    const isOnPath = !isTouched && ancestors.has(node.path);

    let classes = 'tax-tree-row';
    if (isSelected) classes += ' selected';
    if (isTouched) classes += ' touched';
    else if (isOnPath) classes += ' on-path';
    const row = el('div', {
      class: classes,
      style: `padding-left:${depth * 14 + 6}px;`,
      ondragover: e => { e.preventDefault(); row.classList.add('drop-target'); },
      ondragleave: () => row.classList.remove('drop-target'),
      ondrop: e => { row.classList.remove('drop-target'); onDropOnFolder(e, node.path); },
    });
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
    if (isTouched || isOnPath) {
      row.appendChild(el('span', {
        class: 'tax-touched-dot' + (isOnPath ? ' tax-touched-dot-hollow' : ''),
        title: isTouched
          ? 'Modifié — annulable via le bouton Annuler'
          : 'Contient un dossier modifié',
      }));
    }
    row.appendChild(el('span', { class: 'tax-tree-count', title: 'fichiers directs' },
      [String(node.file_count)]));
    row.appendChild(el('button', {
      class: 'tax-tree-add-btn',
      title: 'Créer un sous-dossier',
      onclick: e => { e.stopPropagation(); openCreateFolderPopover(node.path, e.currentTarget); },
    }, ['+']));
    if (!isRoot) {
      row.appendChild(el('button', {
        class: 'tax-tree-add-btn tax-tree-rename-btn',
        title: 'Renommer ce dossier',
        onclick: e => {
          e.stopPropagation();
          openRenamePopover(node.path, node.name, e.currentTarget);
        },
      }, ['✎']));
    }

    const wrap = el('div', { class: 'tax-tree-node' }, [row]);

    if (isExpanded && isExpandable) {
      const childWrap = el('div', { class: 'tax-tree-children' });
      if (hasChildren) for (const c of node.children) childWrap.appendChild(renderTreeNode(c, depth + 1));
      if (hasFiles) {
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
    for (const f of data.files) {
      const filePath = path ? path + '/' + f.name : f.name;
      const isSel = state.selection.type === 'file' && state.selection.path === filePath;
      wrap.appendChild(el('div', {
        class: 'tax-tree-file' + (isSel ? ' selected' : ''),
        title: f.name,
        onclick: () => selectFile(filePath),
      }, [el('span', { class: 'tax-tree-icon' }, ['📄']),
          el('span', { class: 'tax-tree-name' }, [f.name])]));
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
    if (meta.prediction) {
      body.appendChild(el('div', null, [
        '🎯 Prédiction theme-only : ',
        el('code', null, [meta.prediction.dest]),
        ' ', el('span', { class: 'muted small' }, [`(via "${meta.prediction.used_theme}")`]),
      ]));
      body.appendChild(el('div', { class: 'muted small', style: 'margin-top:4px;' },
        [meta.prediction.label]));
    } else {
      body.appendChild(el('div', { class: 'muted' },
        ['🎯 Aucune prédiction theme-only — tous les thèmes sont orphelins.']));
    }
    // Bouton "Pipeline complet" (Phase 2 C) — recalcul avec KeywordClassifier
    const fullBtn = el('button', {
      class: 'tax-llm-full-btn',
      title: 'Calcule la prédiction avec le pipeline complet (KeywordClassifier inclus)',
      onclick: () => loadFullPipelinePrediction(fullBtn),
    }, ['Voir prédiction pipeline complet →']);
    body.appendChild(fullBtn);
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
          '⚙ Pipeline complet : ',
          el('code', null, [r.prediction.dest]),
        ]));
        wrap.appendChild(el('div', { class: 'muted small', style: 'margin-top:4px;' },
          [`source : ${r.prediction.source} · score : ${r.prediction.score.toFixed(2)}`]));
        wrap.appendChild(el('div', { class: 'muted small', style: 'margin-top:2px;' },
          [r.prediction.label]));
      } else {
        wrap.appendChild(el('div', { class: 'muted' },
          ['⚙ Pipeline complet : aucune destination trouvée (theme_mapping + KeywordClassifier ont tous deux échoué)']));
      }
      body.appendChild(wrap);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Voir prédiction pipeline complet →';
      showToast('Erreur pipeline complet : ' + e.message, 'error');
    }
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
    const path = getActiveFolder();
    if (state.selection.type === null) {
      sub.textContent = 'Sélectionne un dossier ou un fichier';
      return;
    }
    const mappings = state.snapshot.mapping_by_folder[path] || [];
    sub.textContent = `${path || 'racine'} · ${mappings.length} clé(s)`;
    if (mappings.length === 0) {
      list.appendChild(el('li', { class: 'muted small' },
        ['Aucun thème mappé. Glisse un thème LLM ici ou utilise « + Mapper ».']));
      return;
    }
    const touched = touchedThemesSet();
    for (const t of mappings) {
      const editBtn = el('button', {
        class: 'tax-mapped-action', title: 'Rediriger ce mapping vers un autre dossier',
        onclick: e => { e.stopPropagation(); openMapPopover({ theme: t, count: '?', is_edit: true }, e.currentTarget); },
      }, ['↗']);
      const delBtn = el('button', {
        class: 'tax-mapped-action tax-mapped-del', title: 'Supprimer ce mapping',
        onclick: e => { e.stopPropagation(); confirmDeleteMapping(t); },
      }, ['×']);
      const isTouched = touched.has(t);
      const dot = isTouched
        ? el('span', { class: 'tax-touched-dot', title: 'Modifié — annulable via le bouton Annuler' })
        : null;
      list.appendChild(el('li', {
        class: 'tax-mapped-item' + (isTouched ? ' touched' : ''),
        title: t,
      }, [el('span', { class: 'tax-mapped-key' }, [t]), dot, editBtn, delBtn]));
    }
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
    const MAX = 300;
    for (const t of filtered.slice(0, MAX)) {
      const mainLine = el('div', { class: 'tax-llm-item-main' }, [
        el('span', { class: 'tax-llm-name' }, [t.theme]),
        el('span', { class: 'tax-llm-count' }, [String(t.count)]),
        el('button', {
          class: 'tax-llm-map-btn',
          title: t.is_orphan ? 'Mapper ce thème (orphelin)' : 'Re-mapper ce thème',
          onclick: e => { e.stopPropagation(); openMapPopover(t, e.currentTarget); },
        }, ['+']),
      ]);
      // Ligne 2 : destination actuelle si mappé, sinon badge orphelin
      const subLine = el('div', { class: 'tax-llm-item-sub' }, [
        t.is_orphan
          ? el('span', { class: 'tax-llm-orph-tag' }, ['orphelin'])
          : el('span', { class: 'tax-llm-dest', title: t.mapped_to }, ['→ ' + t.mapped_to]),
      ]);
      const row = el('div', {
        class: 'tax-llm-item' + (t.is_orphan ? ' orphan' : ''),
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
  }

  // ── "+ Mapper" popover with folder autocomplete ──────────────────────

  function openMapPopover(theme, anchorEl) {
    state.popover = { open: true, theme, anchorEl };
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
    state.popover = { open: false, theme: null, anchorEl: null };
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
    if (!folder || !state.popover.theme) return;
    const theme = state.popover.theme.theme;
    const isEdit = state.popover.theme.is_edit === true;
    closeMapPopover();
    if (isEdit) await doUpdateMapping(theme, folder);
    else        await doAddMapping(theme, folder);
  }

  // ── Drag-drop & central mapping handler ──────────────────────────────

  async function onDropOnFolder(e, folder) {
    e.preventDefault();
    const theme = e.dataTransfer.getData('text/plain');
    if (!theme || folder == null) return;
    await doAddMapping(theme, folder);
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

  // Wrap any write op: preview → conditional modal → action.
  // Returns true if op should proceed.
  async function previewAndConfirm(action, theme, folder) {
    let preview;
    try {
      preview = await fetchPreview(action, theme, folder);
    } catch (e) {
      console.warn('preview failed, proceeding without confirm:', e);
      return true;
    }
    if (!_needsConfirm(preview)) return true;
    return await showImpactModal(action, theme, folder, preview);
  }

  async function doAddMapping(theme, folder) {
    if (!(await previewAndConfirm('add', theme, folder))) return;
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
  }
  async function doUpdateMapping(theme, folder) {
    if (!(await previewAndConfirm('update', theme, folder))) return;
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
  }
  async function confirmDeleteMapping(theme) {
    // Preview always asked for delete (high-impact action by nature)
    let preview;
    try {
      preview = await fetchPreview('delete', theme, null);
    } catch (e) { /* fallback to simple confirm */ }
    if (preview && _needsConfirm(preview)) {
      if (!(await showImpactModal('delete', theme, null, preview))) return;
    } else if (!window.confirm(`Supprimer le mapping « ${theme} » ?\nUtilise ↶ Annuler en cas d'erreur.`)) {
      return;
    }
    try {
      const r = await deleteMapping(theme);
      showToast(`✓ « ${theme} » supprimé (était → ${r.previous_folder})`, 'success');
      state.snapshot = await fetchSnapshot();
      renderAll();
    } catch (err) {
      showToast('✗ ' + err.message, 'error');
    }
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
    try {
      const r = await postCreateFolder(parent, name);
      showToast(`✓ Dossier créé : ${r.path}`, 'success');
      state.snapshot = await fetchSnapshot();
      state.expanded.add(parent);
      renderAll();
    } catch (err) {
      showToast('✗ ' + err.message, 'error');
    }
  }
  async function doRenameFolder(oldPath, newName) {
    closeCreateFolderPopover();
    try {
      const r = await patchRenameFolder(oldPath, newName);
      if (r.unchanged) {
        showToast('Nom inchangé', 'info');
        return;
      }
      showToast(
        `✓ Renommé : ${r.new_path}  ·  ${r.n_tree_entries_renamed} entrée(s) tree, ${r.n_mappings_updated} mapping(s) cascadé(s)`,
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
  }

  async function doUndo() {
    if (!window.confirm('Annuler la dernière modification du mapping ?\n(restore depuis le backup le plus récent)')) return;
    try {
      const r = await postUndo();
      showToast(`↶ Restauré depuis ${r.restored_from}`, 'success');
      state.snapshot = await fetchSnapshot();
      renderAll();
    } catch (err) {
      showToast('✗ ' + err.message, 'error');
    }
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
    renderStats(); renderTree(); renderTreemap(); renderBreadcrumb();
    renderMappedPanel(); renderLLMPanel(); renderFileSection();
  }

  // ── Init ─────────────────────────────────────────────────────────────

  async function init() {
    const sel = $('#tax-profile-select');
    state.profile = sel.value;
    sel.addEventListener('change', async () => {
      state.profile = sel.value;
      state.selection = { type: null, path: '' };
      state.expanded = new Set();
      state.filesByPath = new Map();
      await loadAndRender();
    });
    $('#tax-refresh').addEventListener('click', async () => {
      state.filesByPath = new Map();
      try { state.snapshot = await fetchSnapshot(true); renderAll(); showToast('Snapshot rechargé', 'success'); }
      catch (e) { showToast('Erreur: ' + e.message, 'error'); }
    });
    $('#tax-undo').addEventListener('click', doUndo);
    $('#tax-llm-search').addEventListener('input', e => {
      state.search = e.target.value.trim().toLowerCase(); renderLLMPanel();
    });
    $('#tax-llm-orph-only').addEventListener('change', e => {
      state.orphOnly = e.target.checked; renderLLMPanel();
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
    document.addEventListener('click', e => {
      if (state.popover.open && !e.target.closest('#tax-map-popover')
          && !e.target.classList.contains('tax-llm-map-btn')) closeMapPopover();
      const newFolderPop = $('#tax-newfolder-popover');
      if (newFolderPop.style.display === 'block'
          && !e.target.closest('#tax-newfolder-popover')
          && !e.target.classList.contains('tax-tree-add-btn')) {
        closeCreateFolderPopover();
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
