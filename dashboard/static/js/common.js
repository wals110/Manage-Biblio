// common.js — Helpers partagés utilisés sur toutes les pages

function copyCmd(btn, text) {
    navigator.clipboard.writeText(text).then(function () {
        btn.textContent = '✓';
        btn.classList.add('copied');
        setTimeout(function () {
            btn.textContent = '📋';
            btn.classList.remove('copied');
        }, 1500);
    });
}
