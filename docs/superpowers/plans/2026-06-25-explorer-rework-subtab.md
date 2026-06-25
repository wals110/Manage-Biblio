# Explorateur — refonte : sous-onglet Taxonomie + arbre imbriqué — Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Déplacer l'aperçu Maintenant/Après dans un **sous-onglet de Taxonomie** avec un **vrai arbre imbriqué repliable** (squelette constant = tree.yaml ∪ dossiers réels, seuls les fichiers bougent), supprimer la page autonome `/explorer` + l'onglet sidebar, et retirer le bouton onboarding (garder « Continuer dans Mappings »).

**Architecture:** Le **backend reste** (`explorer.build_projection`, cache + build de fond, routes `/api/explorer/{projection,status,refresh}`, invariant anti-dérive). On ajoute `tree_folders` à la projection. Le **frontend** est un nouveau module `taxonomy_explorer.js` (sous-onglet) qui construit l'arbre imbriqué côté client et bascule Maintenant/Après. On supprime la page autonome.

**Tech Stack:** FastAPI + Jinja2 + vanilla JS. Réutilise `taxonomy._load_tree`, les classes CSS `.tax-tree-*`, le switch générique `activateSubtab` (taxonomy_categories.js), le module partagé `reclassify_apply.js`.

**Spec:** `docs/superpowers/specs/2026-06-25-explorer-preview-design.md` (voir « Révision 2026-06-25 »).
**Branche:** `feature/explorer-preview`.

---

## Contexte (vérifié dans le code)

- `dashboard/templates/taxonomy.html` : sous-onglets l.11-22 (`<button class="tax-subtab" data-view="…">`) ; vues `<div class="tax-view" data-view="…">` ; scripts chargés l.614-617 (`taxonomy.js`, `taxonomy_categories.js`, `taxonomy_rename.js`) avant `{% endblock %}`.
- `dashboard/static/js/taxonomy_categories.js:1136-1137` : binding **générique** `document.querySelectorAll('.tax-subtab').forEach(b => b.addEventListener('click', () => activateSubtab(b.dataset.view)))` → un nouveau `data-view="explorer"` est géré tout seul (show/hide). `activateSubtab` (l.150) bascule `.active` sur les boutons + `.tax-view`.
- Classes CSS réutilisables : `.tax-tree-row`, `.tax-tree-chevron` (+ `.expanded`, `-empty`), `.tax-tree-icon`, `.tax-tree-name`, `.tax-tree-count`, `.tax-tree-node`, `.tax-tree-children`, `.tax-subtab`, `.tax-view`.
- `dashboard/taxonomy.py:_load_tree(profile) -> list[str]` : liste plate triée des dossiers de tree.yaml.
- `dashboard/explorer.py:build_projection(profile)` renvoie `{ok, files:[{rel_path,current_folder,predicted_folder,source,signal,confidence,top_theme,analyzed}], summary, flag_keyword}`.
- Profil actif côté JS : `document.getElementById('tax-profile-select').value`.
- Le standalone à supprimer : `dashboard/templates/explorer.html`, route `/explorer` dans `app.py`, l'entrée nav `Explorateur` dans `base.html`, `dashboard/static/js/explorer.js`, et les tests `TestExplorerPage`.

**Conventions :** Python 3.13, `uv`, ruff-clean, vanilla JS, UI français, commits anglais, trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## Task R1 : backend — `tree_folders` dans `build_projection`

**Files:** Modify `dashboard/explorer.py` ; Test `tests/auto/test_explorer.py`.

- [ ] **Step 1 — Test (échoue).** Dans `TestBuildProjection`, ajouter :
```python
    def test_build_projection_includes_tree_folders(self):
        from dashboard import explorer
        with mock.patch("dashboard.taxonomy._scan_and_classify", return_value=self._rows()), \
             mock.patch("dashboard.taxonomy._load_tree", return_value=["01-Info", "01-Info/ML", "02-Maths"]):
            out = explorer.build_projection("p")
        self.assertEqual(out["tree_folders"], ["01-Info", "01-Info/ML", "02-Maths"])
```
- [ ] **Step 2 — Échec.** `uv run python -m unittest tests.auto.test_explorer.TestBuildProjection -v` → KeyError/AssertionError.
- [ ] **Step 3 — Implémenter.** Dans `build_projection` (return), ajouter la clé :
```python
    return {"ok": True, "files": files,
            "summary": {...},
            "flag_keyword": taxonomy.RECLASSIFY_INCLUDE_KEYWORD,
            "tree_folders": taxonomy._load_tree(profile)}
```
- [ ] **Step 4 — Succès.** `uv run python -m unittest tests.auto.test_explorer 2>&1 | tail -3` → vert ; `uv run ruff check dashboard/explorer.py` → clean.
- [ ] **Step 5 — Commit.**
```bash
git add dashboard/explorer.py tests/auto/test_explorer.py
git commit -m "feat(explorer): exposer tree_folders dans la projection (squelette arbre)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task R2 : sous-onglet Taxonomie + `taxonomy_explorer.js` (arbre imbriqué)

**Files:** Modify `dashboard/templates/taxonomy.html` ; Create `dashboard/static/js/taxonomy_explorer.js` ; Test `tests/auto/test_explorer.py`.

- [ ] **Step 1 — Test (échoue).** Ajouter une classe :
```python
class TestExplorerSubtab(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from dashboard.app import app
        self.client = TestClient(app)

    def test_taxonomy_has_explorer_subtab(self):
        body = self.client.get("/taxonomy").text
        self.assertIn('data-view="explorer"', body)          # sous-onglet + vue
        self.assertIn("taxonomy_explorer.js", body)
        self.assertIn("reclassify_apply.js", body)           # module apply partagé
        self.assertIn("expl-mode-toggle", body)              # interrupteur Maintenant/Après
```
- [ ] **Step 2 — Échec.** `uv run python -m unittest tests.auto.test_explorer.TestExplorerSubtab -v` → FAIL.

- [ ] **Step 3 — Implémenter.**

(a) **Bouton sous-onglet** — dans `taxonomy.html`, après le bouton `data-view="dedupli"` (l.21) :
```html
            <button class="tax-subtab" data-view="explorer"
                    title="Aperçu Maintenant / Après — où iraient les fichiers au reclassify, sans rien déplacer">&#128270; Aperçu</button>
```

(b) **Vue** — après la dernière `<div class="tax-view" …>` existante (la vue `dedupli`, vers l.413-416), ajouter :
```html
<!-- ────────────────────── View: Aperçu (Explorateur) ─────────────────────── -->
<div class="tax-view tax-view-explorer" data-view="explorer" style="display:none;">
  <div style="display:flex;gap:12px;align-items:center;margin:8px 0;flex-wrap:wrap;">
    <div id="expl-mode-toggle" class="tax-subtabs">
      <button class="tax-subtab active" data-mode="now" type="button">Maintenant</button>
      <button class="tax-subtab" data-mode="after" type="button">Après</button>
    </div>
    <span id="expl-counter" class="muted small"></span>
    <button id="expl-refresh" class="btn-secondary" type="button">&#8635; Rafraîchir</button>
    <button id="expl-apply" class="btn-primary" type="button" style="display:none;">Appliquer cette projection</button>
    <span id="expl-apply-status" class="muted small"></span>
  </div>
  <div id="expl-loading" class="onb-note" style="display:none;"></div>
  <div style="display:flex;gap:16px;">
    <div id="expl-tree" class="tax-tree-scroll" style="flex:0 0 360px;max-height:68vh;overflow:auto;"></div>
    <div id="expl-files" style="flex:1;max-height:68vh;overflow:auto;"></div>
  </div>
</div>
<!-- ────────────────────── End view: Aperçu ───────────────────────────────── -->
```

(c) **Scripts** — à l.616-617 (avant `{% endblock %}`), ajouter :
```html
<script src="/static/js/reclassify_apply.js"></script>
<script src="/static/js/taxonomy_explorer.js"></script>
```

(d) **Créer `dashboard/static/js/taxonomy_explorer.js`** :
```javascript
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

  // liste plate de chemins -> arbre {name, path, count, children}
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
    // bascule Maintenant/Après
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
        .then(() => { st.proj = null; load(); });
    };
    // lazy-load à la 1ʳᵉ ouverture du sous-onglet (activateSubtab gère le show/hide)
    const tabBtn = document.querySelector('.tax-subtab[data-view="explorer"]');
    if (tabBtn) tabBtn.addEventListener('click', () => { if (!st.loaded) { st.loaded = true; load(); } });
  });
})();
```

- [ ] **Step 4 — Succès.**
`uv run python -m unittest tests.auto.test_explorer.TestExplorerSubtab -v` → PASS.
`uv run python -c "import dashboard.app"` → OK ; si `node` dispo : `node --check dashboard/static/js/taxonomy_explorer.js`.

- [ ] **Step 5 — Commit.**
```bash
git add dashboard/templates/taxonomy.html dashboard/static/js/taxonomy_explorer.js tests/auto/test_explorer.py
git commit -m "feat(explorer): sous-onglet Aperçu dans Taxonomie + arbre imbriqué Maintenant/Après

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task R3 : supprimer la page autonome `/explorer`

**Files:** Delete `dashboard/templates/explorer.html`, `dashboard/static/js/explorer.js` ; Modify `dashboard/app.py` (route `/explorer`), `dashboard/templates/base.html` (nav) ; Test `tests/auto/test_explorer.py` (retirer `TestExplorerPage`).

- [ ] **Step 1 — Modifier les tests d'abord.** Dans `tests/auto/test_explorer.py`, **supprimer entièrement la classe `TestExplorerPage`** (elle testait `/explorer` + l'entrée nav, qui disparaissent). Ne PAS toucher `TestExplorerRoutes` (les routes API restent).

- [ ] **Step 2 — Supprimer/retirer.**
```bash
git rm dashboard/templates/explorer.html dashboard/static/js/explorer.js
```
- Dans `dashboard/app.py` : **retirer** la route `@app.get("/explorer")` / `def explorer_page(...)`.
- Dans `dashboard/templates/base.html` : **retirer** la ligne nav `<li><a href="/explorer" …>Explorateur</a></li>`.

- [ ] **Step 3 — Vérifier.**
`uv run python -c "import dashboard.app"` → OK.
`uv run python -m unittest tests.auto.test_explorer 2>&1 | tail -3` → vert (TestExplorerRoutes + TestExplorerSubtab + build/cache restent).
`grep -rn "/explorer\"" dashboard/templates/base.html` → vide (plus de nav).
`uv run ruff check dashboard/app.py` → clean.

- [ ] **Step 4 — Commit.**
```bash
git add -A
git commit -m "refactor(explorer): supprimer la page autonome /explorer (remplacée par le sous-onglet)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task R4 : onboarding — retirer le bouton, garder le raffinage

**Files:** Modify `dashboard/templates/onboarding.html` ; Test `tests/auto/test_explorer.py`.

- [ ] **Step 1 — Test (échoue).** Remplacer le corps de `TestOnboardingHandoff.test_onboarding_no_longer_auto_moves` par :
```python
    def test_onboarding_step3_has_no_move_button(self):
        body = self.client.get("/onboarding").text
        self.assertNotIn("moveClassified", body)          # ancien flux absent
        self.assertNotIn("onb-move-btn", body)            # bouton retiré
        self.assertNotIn("/explorer", body)               # plus de handoff Explorateur
        self.assertIn("onb-continue-btn", body)           # raffinage conservé
```
(Renomme la méthode si tu veux ; garde la classe `TestOnboardingHandoff`.)

- [ ] **Step 2 — Échec.** `uv run python -m unittest tests.auto.test_explorer.TestOnboardingHandoff -v` → FAIL (`onb-move-btn`/`/explorer` encore présents).

- [ ] **Step 3 — Implémenter dans `dashboard/templates/onboarding.html`.**
- **Supprimer** le `<button … id="onb-move-btn" …>` (étape 3) **et** son handler JS
  `$('onb-move-btn').addEventListener('click', …)` (le bloc « Voir dans l'Explorateur »).
- **Garder** `onb-continue-btn` (« Continuer dans Mappings pour raffiner → ») et son handler.
- L'élément `onb-move-status` n'est plus utile → le retirer s'il n'est référencé nulle part ailleurs (sinon le laisser inerte).
- Mettre à jour la note de l'étape 3 si elle parle encore de l'aperçu/Explorateur — la garder factuelle (ex. « Affine le classement dans Mappings ; l'aperçu Maintenant/Après est disponible dans Taxonomie ▸ Aperçu. »).

- [ ] **Step 4 — Succès + non-régression.**
`uv run python -m unittest tests.auto.test_explorer.TestOnboardingHandoff -v` → PASS.
`uv run python -m unittest tests.auto.test_agent_onboarding 2>&1 | tail -3` → OK.
`uv run python -c "import dashboard.app"` → OK.

- [ ] **Step 5 — Commit.**
```bash
git add dashboard/templates/onboarding.html tests/auto/test_explorer.py
git commit -m "feat(onboarding): retirer le bouton aperçu (l'aperçu vit dans Taxonomie ▸ Aperçu)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task R5 : suite complète + lint + smoke

- [ ] `uv run python -m unittest discover -s tests/auto 2>&1 | tail -3` → OK.
- [ ] `uv run ruff check dashboard/ tests/auto/test_explorer.py` → clean.
- [ ] `node --check dashboard/static/js/taxonomy_explorer.js dashboard/static/js/reclassify_apply.js` (si node dispo).
- [ ] **Smoke (opérateur)** : redémarrer le dashboard, ouvrir **Taxonomie ▸ 🔎 Aperçu** sur un profil scanné. Attendu : progression du build, puis **arbre imbriqué repliable** (comme l'onglet Taxonomie). Basculer **Maintenant/Après** : la **forme de l'arbre ne change pas**, seuls les compteurs et le contenu des dossiers changent (les fichiers « remontent » dans les bons dossiers en Après). Cliquer un dossier → ses fichiers à droite (provenance « ← venait de X » en Après). **Appliquer** → preview/execute/undo. **Rafraîchir** après édition d'un mapping → rebuild.

---

## Self-review

- Squelette constant (tree.yaml ∪ current ∪ predicted) → `skeleton()` + `buildTree()` (R2). ✓
- Arbre imbriqué réutilisant `.tax-tree-*` → `renderNode` (R2). ✓
- Toggle ne change que les fichiers (skeleton recalculé identique, counts/contenu par mode) → `render()`/`folderOf` (R2). ✓
- Sous-onglet Taxonomie (pas top-level) + suppression page autonome → R2 + R3. ✓
- Onboarding sans bouton, raffinage conservé → R4. ✓
- Backend réutilisé (build de fond, cache, flag unifié, anti-dérive) ; ajout `tree_folders` → R1. ✓
- Tests : `tree_folders` (R1), sous-onglet présent (R2), page autonome retirée (R3), onboarding (R4), suite (R5). `TestExplorerRoutes` (API) conservé. ✓
- Noms cohérents : `expl-mode-toggle`/`expl-tree`/`expl-files`/`expl-counter`/`expl-refresh`/`expl-apply`/`expl-loading`, `taxonomy_explorer.js`, `tree_folders`, `data-view="explorer"`.
