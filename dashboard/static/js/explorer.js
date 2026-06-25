(function () {
  const $ = (id) => document.getElementById(id);
  const profile = new URLSearchParams(location.search).get('profile') || 'default';
  let mode = (new URLSearchParams(location.search).get('mode') === 'after') ? 'after' : 'now';
  let projection = null;
  let selected = null;

  function finalFolder(f) {
    return (f.predicted_folder && f.predicted_folder !== f.current_folder)
      ? f.predicted_folder : (f.predicted_folder || f.current_folder);
  }
  function folderOf(f) { return mode === 'now' ? f.current_folder : finalFolder(f); }

  function load() {
    $('expl-loading').style.display = '';
    $('expl-loading').textContent = 'Calcul de la projection…';
    fetch('/api/explorer/projection?profile=' + encodeURIComponent(profile))
      .then((r) => r.json()).then((d) => {
        if (d.status === 'ready') { projection = d; $('expl-loading').style.display = 'none'; render(); }
        else if (d.status === 'building') { poll(); }
        else { $('expl-loading').textContent = '✗ ' + (d.error || 'erreur'); }
      });
  }
  function poll() {
    fetch('/api/explorer/status?profile=' + encodeURIComponent(profile))
      .then((r) => r.json()).then((s) => {
        if (s.status === 'ready') { load(); }
        else if (s.status === 'error') { $('expl-loading').textContent = '✗ ' + (s.error || 'erreur'); }
        else {
          $('expl-loading').textContent = `Calcul… ${s.n_done || 0}/${s.n_total || '?'} fichiers`;
          setTimeout(poll, 1000);
        }
      });
  }

  function render() {
    if (!projection) return;
    const s = projection.summary;
    $('expl-counter').textContent = `${s.n_moving} bougeraient · ${s.n_total} fichiers`
      + (s.n_unanalyzed ? ` · ${s.n_unanalyzed} non analysés (Vision)` : '');
    $('expl-apply').style.display = (mode === 'after' && s.n_moving > 0) ? '' : 'none';
    const byFolder = {}, incoming = {}, outgoing = {};
    for (const f of projection.files) {
      (byFolder[folderOf(f)] ||= []).push(f);
      if (f.predicted_folder && f.predicted_folder !== f.current_folder) {
        incoming[f.predicted_folder] = (incoming[f.predicted_folder] || 0) + 1;
        outgoing[f.current_folder] = (outgoing[f.current_folder] || 0) + 1;
      }
    }
    const folders = Object.keys(byFolder).sort();
    $('expl-tree').innerHTML = folders.map((p) => {
      const badge = mode === 'after' && incoming[p] ? ` <span class="muted">+${incoming[p]}</span>` : '';
      const out = mode === 'now' && outgoing[p] ? ` <span class="muted">−${outgoing[p]}</span>` : '';
      const sel = p === selected ? ' style="font-weight:600"' : '';
      return `<div class="expl-folder" data-folder="${encodeURIComponent(p)}"${sel}>`
        + `${p || '(racine)'} <span class="muted small">(${byFolder[p].length})</span>${badge}${out}</div>`;
    }).join('');
    $('expl-tree').querySelectorAll('.expl-folder').forEach((el) => {
      el.onclick = () => { selected = decodeURIComponent(el.dataset.folder); render(); renderFiles(byFolder); };
    });
    renderFiles(byFolder);
  }

  const PAGE = 200;
  function renderFiles(byFolder) {
    const list = (selected != null && byFolder[selected]) || [];
    const slice = list.slice(0, PAGE);
    $('expl-files').innerHTML = `<div class="muted small">${selected || ''} — ${list.length} fichier(s)</div>`
      + slice.map((f) => {
        const moved = f.predicted_folder && f.predicted_folder !== f.current_folder;
        const prov = (mode === 'after' && moved) ? ` <span class="muted small">← ${f.current_folder || '(racine)'}</span>` : '';
        const sig = f.signal ? ` <span class="muted small">[${f.signal}]</span>` : '';
        return `<div class="expl-file">${f.rel_path.split('/').pop()}${prov}${sig}</div>`;
      }).join('')
      + (list.length > PAGE ? `<div class="muted small">+${list.length - PAGE} autres…</div>` : '');
  }

  $('onb-expl-toggle').querySelectorAll('button').forEach((b) => {
    b.onclick = () => {
      mode = b.dataset.mode;
      $('onb-expl-toggle').querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === b));
      render();
    };
    b.classList.toggle('active', b.dataset.mode === mode);
  });
  $('expl-refresh').onclick = () => {
    fetch('/api/explorer/refresh?profile=' + encodeURIComponent(profile), { method: 'POST' })
      .then(() => { projection = null; load(); });
  };
  $('expl-apply').onclick = () => {
    if (!projection) return;
    window.applyReclassify({
      profile, keyword: !!projection.flag_keyword,
      onStatus: (m) => { $('expl-apply-status').textContent = m; },
    }).then(() => { projection = null; load(); });
  };

  load();
})();
