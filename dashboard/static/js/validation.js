// validation.js — Widgets de validation manuelle des checks

function validateCheck(runId, checkId, status) {
    fetch('/api/validate-check?run_id=' + runId + '&check_id=' + checkId + '&status=' + status, { method: 'POST' })
        .then(function () { window.location.reload(); });
}

function reviseWidget(runId, checkId) {
    return '<button class="btn btn-sm btn-pass" onclick="validateCheck(\'' + runId + '\', \'' + checkId + '\', \'pass\')">&#10003; Oui</button>'
         + '<button class="btn btn-sm btn-fail-action" onclick="validateCheck(\'' + runId + '\', \'' + checkId + '\', \'fail\')">&#10007; Non</button>';
}

function activateStops(btn) {
    // Activer le bouton stop de la phase et le bouton stop global
    var phase = btn.closest('.phase-accordion');
    if (phase) {
        var phaseStop = phase.querySelector('.btn-phase-stop');
        if (phaseStop) {
            phaseStop.disabled = false;
            phaseStop.className = 'btn btn-stop btn-sm btn-phase-stop';
        }
    }
    var globalStop = document.getElementById('btn-stop');
    if (globalStop) {
        globalStop.disabled = false;
        globalStop.className = 'btn btn-stop';
    }
}
