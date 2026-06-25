// Flux Apply global du reclassify, réutilisable (onboarding, explorer).
// applyReclassify({profile, keyword, onStatus, confirmFn, undoConfirmFn}) -> Promise<void>
//   onStatus(msg)      : callback d'affichage (string)
//   confirmFn(n)       : retourne true/false (confirmer le déplacement de n fichiers)
//   undoConfirmFn(n)   : retourne true/false (proposer l'annulation après n déplacés)
window.applyReclassify = async function ({ profile, keyword, onStatus, confirmFn, undoConfirmFn }) {
  const base = '/api/taxonomy/reclassify/apply';
  const enc = encodeURIComponent(profile);
  const say = onStatus || (() => {});
  async function pollDone() {
    for (;;) {
      const st = await fetch(base + '/status?profile=' + enc).then((r) => r.json());
      const p = st.progress;
      if (p && (p.status === 'done' || p.status === 'error')) return p;
      if (p) say(`Déplacement… ${p.n_done || 0}/${p.n_total || '?'}`);
      await new Promise((res) => setTimeout(res, 1500));
    }
  }
  say('Calcul de ce qui bougerait…');
  const pv = await fetch(base + '/preview?profile=' + enc + '&keyword=' + (keyword ? 'true' : 'false'))
    .then((r) => r.json());
  if (pv.error) { say('✗ ' + pv.error); return; }
  const n = pv.n_moves || 0;
  if (!n) { say('Aucun fichier à déplacer.'); return; }
  if (!(confirmFn ? confirmFn(n) : window.confirm(`Déplacer ${n} fichier(s) ?`))) { say(''); return; }
  say('Déplacement en cours…');
  await fetch(base + '/execute', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profile }) });
  const prog = await pollDone();
  if (prog.status === 'error') { say('✗ Échec : ' + (prog.error || 'erreur')); return; }
  const moved = prog.n_done || 0, failed = prog.n_failed || 0, skipped = prog.n_skipped || 0;
  say(`✓ ${moved} déplacé(s)` + (skipped ? ` · ${skipped} ignoré(s)` : '')
      + (failed ? ` · ${failed} erreur(s)` : ''));
  if (moved > 0 && (undoConfirmFn ? undoConfirmFn(moved)
      : window.confirm(`${moved} déplacé(s). Annuler ?`))) {
    say('Annulation…');
    await fetch(base + '/undo', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile }) });
    const u = await pollDone();
    say(u.status === 'error' ? ('✗ ' + (u.error || 'échec annulation')) : '↩ Déplacement annulé.');
  }
};
