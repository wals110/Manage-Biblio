// tests.js — Gestion de l'exécution des tests fonctionnels
// Variables _profileNames et _currentProfile doivent être définies inline avant de charger ce fichier.

function updateSelectionCount() {
    const checked = document.querySelectorAll('.series-checkbox:checked');
    const count = checked.length;
    document.getElementById('selection-count').textContent = count;
    const btn = document.getElementById('btn-run-selection');
    btn.disabled = count === 0;
    btn.style.opacity = count === 0 ? '0.4' : '1';
}

function togglePhaseCheckboxes(phaseCheckbox) {
    const phase = phaseCheckbox.closest('.phase-accordion');
    const boxes = phase.querySelectorAll('.series-checkbox');
    boxes.forEach(b => { b.checked = phaseCheckbox.checked; });
    updateSelectionCount();
}

function checkAll() {
    document.querySelectorAll('.series-checkbox, .phase-checkbox').forEach(b => { b.checked = true; });
    updateSelectionCount();
}

function toggleAllCheckboxes() {
    const boxes = document.querySelectorAll('.series-checkbox');
    const allChecked = Array.from(boxes).every(b => b.checked);
    const newState = !allChecked;
    boxes.forEach(b => { b.checked = newState; });
    document.querySelectorAll('.phase-checkbox').forEach(b => { b.checked = newState; });
    const btn = document.querySelector('.btn-toggle-all');
    if (btn) btn.textContent = newState ? 'Tout désélectionner' : 'Tout sélectionner';
    updateSelectionCount();
}

function runSelection(btn) {
    const checked = document.querySelectorAll('.series-checkbox:checked');
    if (checked.length === 0) return;
    const series = Array.from(checked).map(c => c.value).join(',');
    const url = buildRunUrl('/api/run?series=' + series);
    runAll(btn, url);
}

function updateProfileInCommands(newProfile) {
    document.querySelectorAll('.cmd-text').forEach(el => {
        el.textContent = el.textContent.replace(
            '--profile ' + _currentProfile,
            '--profile ' + newProfile
        );
    });
    document.querySelectorAll('.btn-copy').forEach(btn => {
        const onclick = btn.getAttribute('onclick');
        if (onclick && onclick.includes('--profile ' + _currentProfile)) {
            btn.setAttribute('onclick', onclick.replace(
                '--profile ' + _currentProfile,
                '--profile ' + newProfile
            ));
        }
    });
    _currentProfile = newProfile;
}

function buildRunUrl(baseUrl) {
    let url = baseUrl;
    const historyChecked = document.getElementById('history-toggle').checked;
    if (!historyChecked) url += '&no_history=true';
    const profile = document.getElementById('profile-select').value;
    if (profile) url += '&profile=' + encodeURIComponent(profile);
    return url;
}

function saveCheckboxes() {
    const checked = Array.from(document.querySelectorAll('.series-checkbox:checked')).map(c => c.value);
    sessionStorage.setItem('checkedSeries', JSON.stringify(checked));
}

function startProgressStream() {
    if (window._klodoSSE) {
        window._klodoSSE.close();
    }
    const es = new EventSource('/api/events');
    window._klodoSSE = es;

    es.onmessage = function (ev) {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'idle') return;
        if (msg.type === 'series_started') {
            updateSeriesStatus(msg.id, null, null);
        } else if (msg.type === 'series_completed') {
            updateSeriesStatus(null, { [msg.id]: msg.status }, null);
        } else if (msg.type === 'check_completed') {
            updateSeriesStatus(null, null, { [msg.id]: msg.status });
        } else if (msg.type === 'run_finished') {
            es.close();
            window.location.reload();
        }
    };

    es.onerror = function () {
        // EventSource reconnecte automatiquement
    };
}

function runAll(btn, url) {
    const isPhase = url.includes('phase=');
    const scope = isPhase ? btn.closest('.phase-accordion') : document.getElementById('test-phases');
    if (!scope) return;

    // Reset dots to grey, highlight first one as running
    scope.querySelectorAll('.status-dot').forEach(d => {
        d.className = 'status-dot dot-not_run';
    });
    const firstDot = scope.querySelector('.series-summary > .status-dot');
    if (firstDot) firstDot.className = 'status-dot dot-running';

    // Démarrer le flux SSE pour le suivi temps réel
    startProgressStream();

    scope.querySelectorAll('.series-stats span').forEach(s => {
        s.textContent = '...';
        s.className = 'text-muted';
    });
    scope.querySelectorAll('.phase-pass-count').forEach(s => {
        s.textContent = '...';
        s.className = 'phase-pass-count text-muted';
    });

    document.querySelectorAll('.btn-run, .btn-primary, #btn-run-failures').forEach(b => {
        b.style.pointerEvents = 'none';
        b.style.opacity = '0.4';
    });
    btn.innerHTML = 'Exécution...';
    const stopBtn = document.getElementById('btn-stop');
    stopBtn.disabled = false;
    stopBtn.className = 'btn btn-stop';

    if (isPhase) {
        const phaseStop = btn.closest('.phase-header-right').querySelector('.btn-phase-stop');
        if (phaseStop) {
            phaseStop.disabled = false;
            phaseStop.className = 'btn btn-stop btn-sm btn-phase-stop';
        }
    } else {
        document.querySelectorAll('.btn-phase-stop').forEach(b => {
            b.disabled = false;
            b.className = 'btn btn-stop btn-sm btn-phase-stop';
        });
    }

    fetch(url, { method: 'POST' }).then(() => {
        saveCheckboxes();
        window.location.reload();
    }).catch(() => {
        saveCheckboxes();
        window.location.reload();
    });
}

function stopRun() {
    const stopBtn = document.getElementById('btn-stop');
    stopBtn.innerHTML = 'Arrêt...';
    stopBtn.style.pointerEvents = 'none';
    fetch('/api/stop', { method: 'POST' }).then(() => {
        setTimeout(() => window.location.reload(), 1000);
    }).catch(() => {
        window.location.reload();
    });
}

// ── Expand state persistence ──
function toggleExpand(el) {
    el.classList.toggle('expanded');
    saveExpandState();
}

function saveExpandState() {
    const expanded = Array.from(document.querySelectorAll('.series-row.expanded'))
        .map(el => el.id);
    sessionStorage.setItem('expandedSeries', JSON.stringify(expanded));
}

function updateSeriesStatus(seriesId, completed, checks) {
    if (completed) {
        for (const [sid, status] of Object.entries(completed)) {
            const safeId = sid.replace('.', '-');
            const row = document.getElementById('series-' + safeId);
            if (row) {
                const dot = row.querySelector('.series-summary > .status-dot');
                if (dot) dot.className = 'status-dot dot-' + status;
            }
        }
    }
    if (checks) {
        for (const [cid, status] of Object.entries(checks)) {
            document.querySelectorAll('.check-id').forEach(el => {
                if (el.textContent.trim() === cid) {
                    const checkDot = el.parentElement.querySelector('.status-dot');
                    if (checkDot) checkDot.className = 'status-dot dot-' + status;
                }
            });
        }
    }
    document.querySelectorAll('.dot-running').forEach(d => {
        d.className = 'status-dot dot-not_run';
    });
    if (seriesId) {
        const safeId = seriesId.replace('.', '-');
        const row = document.getElementById('series-' + safeId);
        if (row) {
            const dot = row.querySelector('.series-summary > .status-dot');
            if (dot) dot.className = 'status-dot dot-running';
        }
    }
}
