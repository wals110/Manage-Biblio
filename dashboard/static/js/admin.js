// admin.js — Gestion des clés API, nettoyage et suppression de runs

function saveApiKey(keyName) {
    const input = document.getElementById('input-' + keyName);
    const value = input.value.trim();
    if (!value) { alert('Veuillez entrer une clé API'); return; }
    fetch('/api/admin/save-api-key?key_name=' + keyName + '&key_value=' + encodeURIComponent(value), { method: 'POST' })
        .then(r => r.json()).then(data => {
            alert(data.message);
            window.location.reload();
        });
}

function deleteApiKey(keyName) {
    if (!confirm('Supprimer la clé ' + keyName + ' ?')) return;
    fetch('/api/admin/delete-api-key?key_name=' + keyName, { method: 'POST' })
        .then(r => r.json()).then(data => {
            alert(data.message);
            window.location.reload();
        });
}

function adminAction(url, confirmMsg) {
    if (!confirm(confirmMsg)) return;
    fetch(url, { method: 'POST' }).then(r => r.json()).then(data => {
        alert(data.message || 'Done');
        window.location.reload();
    });
}

function deleteRun(runId) {
    if (!confirm('Supprimer le run ' + runId + ' ?')) return;
    fetch('/api/admin/delete-run?id=' + runId, { method: 'POST' }).then(r => r.json()).then(() => {
        document.getElementById('run-row-' + runId)?.remove();
    });
}

function toggleRunCheckboxes() {
    const boxes = document.querySelectorAll('.run-checkbox');
    const allChecked = Array.from(boxes).every(b => b.checked);
    boxes.forEach(b => { b.checked = !allChecked; });
}

function deleteSelectedRuns() {
    const checked = Array.from(document.querySelectorAll('.run-checkbox:checked')).map(c => c.value);
    if (checked.length === 0) return;
    if (!confirm('Supprimer ' + checked.length + ' run(s) ?')) return;
    Promise.all(checked.map(id =>
        fetch('/api/admin/delete-run?id=' + id, { method: 'POST' })
    )).then(() => window.location.reload());
}
