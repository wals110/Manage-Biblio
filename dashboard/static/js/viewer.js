// viewer.js — Visualisation double-panneau (source + destination)
// Variables _sourceProfile et _destProfile doivent être définies inline avant.

// ── Scope helpers ──────────────────────────────────────────────────────
const ViewerScope = {
    source: {
        get profile() { return document.getElementById('source-profile-select').value; },
        get list() { return document.getElementById('source-file-list'); },
        get display() { return document.getElementById('source-thumbnail-display'); },
        get info() { return document.getElementById('source-file-info'); },
        get navigator() { return document.getElementById('source-page-navigator'); },
        get pageCurrent() { return document.getElementById('source-page-current'); },
        get pageTotal() { return document.getElementById('source-page-total'); },
    },
    dest: {
        get profile() { return document.getElementById('dest-profile-select').value; },
        get list() { return document.getElementById('dest-file-list'); },
        get display() { return document.getElementById('dest-thumbnail-display'); },
        get info() { return document.getElementById('dest-file-info'); },
        get navigator() { return document.getElementById('dest-page-navigator'); },
        get pageCurrent() { return document.getElementById('dest-page-current'); },
        get pageTotal() { return document.getElementById('dest-page-total'); },
    }
};

// État du navigateur de pages par scope
const PageState = {
    source: { filename: null, current: 1, total: 0 },
    dest: { filename: null, current: 1, total: 0 },
};

// ── Sélection de fichiers à copier ─────────────────────────────────────
let _markedFiles = new Set();

function toggleMark(filename, btnEl) {
    if (_markedFiles.has(filename)) {
        _markedFiles.delete(filename);
        btnEl.classList.remove('marked');
    } else {
        _markedFiles.add(filename);
        btnEl.classList.add('marked');
    }
    updateMarkedCount();
    persistMarked();
}

function updateMarkedCount() {
    const count = _markedFiles.size;
    const counter = document.getElementById('marked-count');
    if (counter) counter.textContent = count;
    const btn = document.getElementById('btn-copy-selection');
    if (btn) {
        btn.disabled = count === 0;
        btn.textContent = `Copier sélection (${count}) → destination`;
    }
}

function persistMarked() {
    sessionStorage.setItem('viewer_marked', JSON.stringify(Array.from(_markedFiles)));
}

function restoreMarked() {
    const saved = sessionStorage.getItem('viewer_marked');
    if (!saved) return;
    try {
        const arr = JSON.parse(saved);
        _markedFiles = new Set(arr);
        _markedFiles.forEach(name => {
            const btn = document.querySelector(`.viewer-mark-btn[data-file="${CSS.escape(name)}"]`);
            if (btn) btn.classList.add('marked');
        });
        updateMarkedCount();
    } catch (e) {
        sessionStorage.removeItem('viewer_marked');
    }
}

// ── Navigation profil ───────────────────────────────────────────────────
function changeSourceProfile(name) {
    const dest = _destProfile;
    window.location.href = `/viewer?source_profile=${encodeURIComponent(name)}&dest_profile=${encodeURIComponent(dest)}`;
}

function changeDestProfile(name) {
    const source = _sourceProfile;
    window.location.href = `/viewer?source_profile=${encodeURIComponent(source)}&dest_profile=${encodeURIComponent(name)}`;
}

// ── Filtre liste ───────────────────────────────────────────────────────
function filterFiles(scope, query) {
    const q = query.toLowerCase();
    const list = ViewerScope[scope].list;
    if (!list) return;
    list.querySelectorAll('.viewer-file-item').forEach(item => {
        const name = (item.dataset.name || '').toLowerCase();
        item.style.display = (!q || name.includes(q)) ? '' : 'none';
    });
}

// ── Sélection + thumbnail (multi-pages) ───────────────────────────────
function selectFile(scope, itemEl, filename) {
    const list = ViewerScope[scope].list;
    list.querySelectorAll('.viewer-file-item.selected').forEach(el => el.classList.remove('selected'));
    itemEl.classList.add('selected');
    PageState[scope].filename = filename;
    PageState[scope].current = 1;
    loadThumbnail(scope, filename, itemEl);
}

function loadThumbnail(scope, filename, itemEl) {
    const display = ViewerScope[scope].display;
    const info = ViewerScope[scope].info;
    display.innerHTML = '<p class="text-muted">Chargement...</p>';
    info.textContent = filename;

    const profile = ViewerScope[scope].profile;
    // 1. Combien de pages en cache ?
    fetch('/api/viewer/pages?profile=' + encodeURIComponent(profile)
        + '&filename=' + encodeURIComponent(filename))
        .then(r => r.json())
        .then(data => {
            const pageCount = data.count || 0;
            if (pageCount === 0) {
                // Génération auto au clic
                display.innerHTML = '<p class="text-muted">Génération en cours...</p>';
                generateOne(scope, filename, itemEl);
                return;
            }
            PageState[scope].total = pageCount;
            PageState[scope].current = 1;
            renderPage(scope);
            updateNavigator(scope);
        })
        .catch(err => {
            display.innerHTML = '<p class="text-red">Erreur : ' + err + '</p>';
        });
}

function renderPage(scope) {
    const display = ViewerScope[scope].display;
    const profile = ViewerScope[scope].profile;
    const filename = PageState[scope].filename;
    const page = PageState[scope].current;
    if (!filename) return;
    const url = '/api/viewer/thumbnail'
              + '?profile=' + encodeURIComponent(profile)
              + '&filename=' + encodeURIComponent(filename)
              + '&page=' + page
              + '&_t=' + Date.now();
    display.innerHTML = '<img src="' + url + '" alt="' + filename + ' page ' + page + '" class="viewer-thumbnail-img">';
}

function updateNavigator(scope) {
    const nav = ViewerScope[scope].navigator;
    const total = PageState[scope].total;
    const current = PageState[scope].current;
    if (!nav) return;

    if (total > 1) {
        nav.classList.remove('hidden');
        ViewerScope[scope].pageCurrent.textContent = current;
        ViewerScope[scope].pageTotal.textContent = total;
        const buttons = nav.querySelectorAll('.page-nav-btn');
        if (buttons.length === 2) {
            buttons[0].disabled = (current <= 1);
            buttons[1].disabled = (current >= total);
        }
    } else {
        nav.classList.add('hidden');
    }
}

function prevPage(scope) {
    if (PageState[scope].current > 1) {
        PageState[scope].current--;
        renderPage(scope);
        updateNavigator(scope);
    }
}

function nextPage(scope) {
    if (PageState[scope].current < PageState[scope].total) {
        PageState[scope].current++;
        renderPage(scope);
        updateNavigator(scope);
    }
}

function generateOne(scope, filename, itemEl) {
    const profile = ViewerScope[scope].profile;
    // Si scope=dest, on récupère N depuis le sélecteur
    let nPages = 1;
    if (scope === 'dest') {
        const sel = document.getElementById('dest-pages-select');
        if (sel) nPages = parseInt(sel.value, 10) || 1;
    }
    const url = '/api/viewer/generate'
              + '?profile=' + encodeURIComponent(profile)
              + '&filename=' + encodeURIComponent(filename)
              + '&n_pages=' + nPages;
    fetch(url, { method: 'POST' }).then(r => r.json()).then(data => {
        if (data.success) {
            if (itemEl) {
                itemEl.dataset.cached = 'true';
                const dot = itemEl.querySelector('.viewer-cache-dot');
                if (dot) dot.classList.add('cached');
            }
            // Recharger pour récupérer le vrai count
            loadThumbnail(scope, filename, itemEl);
            refreshCacheButton(scope);
        } else {
            ViewerScope[scope].display.innerHTML = '<p class="text-red">Génération échouée</p>';
        }
    }).catch(err => {
        ViewerScope[scope].display.innerHTML = '<p class="text-red">Erreur : ' + err + '</p>';
    });
}

// ── Copie source → destination ─────────────────────────────────────────
function copySelection() {
    if (_markedFiles.size === 0) return;
    const source = ViewerScope.source.profile;
    const dest = ViewerScope.dest.profile;
    const filenames = Array.from(_markedFiles).join(',');

    const url = '/api/viewer/copy'
              + '?source_profile=' + encodeURIComponent(source)
              + '&dest_profile=' + encodeURIComponent(dest)
              + '&filenames=' + encodeURIComponent(filenames);

    fetch(url, { method: 'POST' }).then(r => r.json()).then(result => {
        if (result.error) {
            alert('Erreur : ' + result.error);
            return;
        }
        let msg = `${result.copied} fichier(s) copié(s)`;
        if (result.skipped > 0) msg += `, ${result.skipped} ignoré(s)`;
        if (result.errors && result.errors.length > 0) msg += `\n\nErreurs :\n` + result.errors.join('\n');
        alert(msg);
        // Vider la sélection après une copie réussie
        _markedFiles.clear();
        sessionStorage.removeItem('viewer_marked');
        window.location.reload();
    }).catch(err => alert('Erreur : ' + err));
}

// ── Vider destination ──────────────────────────────────────────────────
function clearDestination() {
    const dest = ViewerScope.dest.profile;
    if (!confirm(`Vider l'INBOX du profil ${dest} ?\n\nLes thumbnails (cache) sont préservés.`)) {
        return;
    }
    fetch('/api/viewer/clear-destination?profile=' + encodeURIComponent(dest), { method: 'POST' })
        .then(r => r.json())
        .then(result => {
            if (result.error) {
                alert('Erreur : ' + result.error);
                return;
            }
            alert(`${result.removed} fichier(s) supprimé(s), ${result.preserved_cache} thumbnail(s) préservé(s)`);
            window.location.reload();
        })
        .catch(err => alert('Erreur : ' + err));
}

// ── Génération en masse ────────────────────────────────────────────────
function generateMissing(scope) {
    startBatch(scope, false);
}

function generateAll(scope) {
    if (!confirm('Régénérer TOUS les thumbnails ? Le cache existant sera écrasé.')) {
        return;
    }
    startBatch(scope, true);
}

function startBatch(scope, force) {
    const profile = ViewerScope[scope].profile;
    let nPages = 1;
    if (scope === 'dest') {
        const sel = document.getElementById('dest-pages-select');
        if (sel) nPages = parseInt(sel.value, 10) || 1;
    }
    const url = '/api/viewer/generate-batch?profile=' + encodeURIComponent(profile)
              + '&force=' + (force ? 'true' : 'false')
              + '&n_pages=' + nPages;
    fetch(url, { method: 'POST' }).then(r => r.json()).then(data => {
        if (!data.started) {
            alert('Une génération est déjà en cours.');
            return;
        }
        startBatchProgress(scope);
    }).catch(err => alert('Erreur : ' + err));
}

function startBatchProgress(scope) {
    const panel = document.getElementById(scope + '-progress');
    panel.classList.remove('hidden');
    panel.classList.remove('panel-progress-done');

    if (window._klodoViewerSSE) {
        window._klodoViewerSSE.close();
    }
    const es = new EventSource('/api/viewer/events');
    window._klodoViewerSSE = es;

    es.onmessage = function (ev) {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'progress') {
            updateProgressBar(scope, msg.done, msg.total, msg.current, false);
        } else if (msg.type === 'batch_finished') {
            updateProgressBar(scope, msg.done, msg.total, '', true);
            es.close();
            setTimeout(() => refreshFileList(scope), 500);
            setTimeout(() => panel.classList.add('hidden'), 4000);
        }
    };

    es.onerror = function () {
        // EventSource reconnecte automatiquement
    };
}

function updateProgressBar(scope, done, total, current, done_state) {
    const panel = document.getElementById(scope + '-progress');
    if (!panel) return;
    const fill = panel.querySelector('.panel-progress-fill');
    const count = panel.querySelector('.panel-progress-count');
    const filename = panel.querySelector('.panel-progress-current');
    const percent = panel.querySelector('.panel-progress-percent');

    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    if (fill) fill.style.width = pct + '%';
    if (percent) percent.textContent = pct + '%';
    if (filename) filename.textContent = current || '';
    if (count) {
        count.textContent = done_state
            ? `${done} / ${total} ✓ Terminé`
            : `${done} / ${total}`;
    }
    if (done_state) {
        panel.classList.add('panel-progress-done');
    }
}

function refreshFileList(scope) {
    const profile = ViewerScope[scope].profile;
    fetch('/api/viewer/files?profile=' + encodeURIComponent(profile))
        .then(r => r.json())
        .then(files => {
            const list = ViewerScope[scope].list;
            files.forEach(f => {
                const item = list.querySelector('.viewer-file-item[data-name="' + f.name.replace(/"/g, '\\"') + '"]');
                if (item) {
                    item.dataset.cached = f.cached ? 'true' : 'false';
                    const dot = item.querySelector('.viewer-cache-dot');
                    if (dot) {
                        if (f.cached) dot.classList.add('cached');
                        else dot.classList.remove('cached');
                    }
                }
            });
        });
    refreshCacheButton(scope);
}

function refreshCacheButton(scope) {
    // Met à jour le compteur "Vider cache (X img, Y Mo)" du bouton après une action.
    // Seul le panneau source a ce bouton (id="source-clear-btn").
    if (scope !== 'source') return;
    const profile = ViewerScope[scope].profile;
    const btn = document.getElementById('source-clear-btn');
    if (!btn) return;
    fetch('/api/viewer/cache-stats?profile=' + encodeURIComponent(profile))
        .then(r => r.json())
        .then(stats => {
            const countEl = btn.querySelector('.cache-count');
            const moEl = btn.querySelector('.cache-mo');
            if (countEl) countEl.textContent = stats.count;
            if (moEl) moEl.textContent = stats.size_mo;
        })
        .catch(() => {});
}

function clearCache(scope) {
    const profile = ViewerScope[scope].profile;
    if (!confirm('Vider le cache des thumbnails pour le profil ' + profile + ' ?')) return;
    fetch('/api/viewer/clear-cache?profile=' + encodeURIComponent(profile), { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            alert(data.count + ' thumbnails supprimés (' + Math.round(data.freed_bytes / 1024) + ' Ko libérés)');
            window.location.reload();
        })
        .catch(err => alert('Erreur : ' + err));
}

// ── Init ────────────────────────────────────────────────────────────────
restoreMarked();
updateMarkedCount();
