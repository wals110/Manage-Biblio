# Toggle Maintenant/Après dans Mappings — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Faire vivre l'aperçu Maintenant/Après comme un toggle DANS le sous-onglet Mappings (au lieu d'un sous-onglet « Aperçu » séparé), où « Après » re-range les fichiers de l'arbre Mappings existant dans leur dossier cible.

**Architecture:** Overlay côté client. « Maintenant » reste strictement l'actuel (snapshot disque + lazy-load disque). « Après » charge la projection existante (`/api/explorer/projection`, `build_projection`), bucketise les fichiers par dossier cible côté client, et le rendu de l'arbre (compteurs, expandabilité, liste de fichiers) lit cette source en mode Après. Aucun nouveau backend. Le sous-onglet Aperçu autonome (`taxonomy_explorer.js`) est supprimé ; son backend de projection est conservé et réutilisé.

**Tech Stack:** FastAPI + Jinja2 (templates) ; JavaScript vanilla sur `window` (pas de bundler) ; tests Python `unittest` + `fastapi.testclient.TestClient` ; `uv` pour lancer.

## Global Constraints

- **Python 3.13 via `uv`** ; lancer les tests avec `uv run python -m unittest ...`.
- **JS vanilla, pas de bundler** : modules chargés par `<script>` dans `dashboard/templates/taxonomy.html`, état sur un objet `state` interne à l'IIFE de `taxonomy.js`.
- **Pas de harnais de test JS** : la logique JS est validée par **smoke manuel** ; le filet automatique porte sur le **template** (assertions `TestClient.get("/taxonomy").text`) et le **backend** (déjà couvert).
- **Ne jamais** lancer un reclassify réel en test ; ne pas toucher au backend de projection (`dashboard/explorer.py`) ni à ses routes.
- **Ruff** doit rester vert : `uv run ruff check tests/auto/`.
- **Classe CSS du toggle = `tax-preview-btn`** (PAS `tax-subtab`) : le binding générique `activateSubtab` lie TOUS les `.tax-subtab` ; réutiliser cette classe ferait collisionner le toggle avec le routage des sous-onglets.
- **Commits** : messages en anglais, terminés par `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Profil de smoke** : `the-big-one` (déjà régénéré). Hard-reload (Cmd+Shift+R) après chaque changement JS/template.

---

## File Structure

- `dashboard/templates/taxonomy.html` — retire le sous-onglet Aperçu (bouton + vue + `<script>`) ; ajoute la barre d'outils toggle dans la vue Mappings + son CSS.
- `dashboard/static/js/taxonomy_explorer.js` — **supprimé**.
- `dashboard/static/js/taxonomy.js` — ajoute l'état preview, le chargement/bucketing de la projection, le toggle, et rend l'arbre + les fichiers mode-aware.
- `dashboard/static/js/reclassify_apply.js` — **inchangé** (réutilisé par le bouton Appliquer).
- `dashboard/explorer.py` + routes `/api/explorer/*` — **inchangés** (alimentent le mode Après).
- `tests/auto/test_explorer.py` — la classe `TestExplorerSubtab` devient `TestMappingsPreviewToggle` (le sous-onglet a disparu ; Mappings a le toggle). Les tests `TestExplorerRoutes` / `TestBuildProjection` restent inchangés.

---

## Task 1: Supprimer le sous-onglet Aperçu autonome (backend projection conservé)

**Files:**
- Modify: `dashboard/templates/taxonomy.html` (bouton l.22-23, vue l.420-451, script l.653)
- Delete: `dashboard/static/js/taxonomy_explorer.js`
- Test: `tests/auto/test_explorer.py` (classe `TestExplorerSubtab` → `TestMappingsPreviewToggle`)

**Interfaces:**
- Consumes: rien.
- Produces: la page `/taxonomy` ne contient plus `data-view="explorer"` ni `taxonomy_explorer.js` ; `reclassify_apply.js` reste chargé.

- [ ] **Step 1: Réécrire le test pour exiger la SUPPRESSION du sous-onglet**

Dans `tests/auto/test_explorer.py`, remplacer la classe `TestExplorerSubtab` (et sa méthode `test_taxonomy_has_explorer_subtab`) par :

```python
class TestMappingsPreviewToggle(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from dashboard.app import app
        self.client = TestClient(app)

    def test_explorer_subtab_removed(self):
        body = self.client.get("/taxonomy").text
        self.assertNotIn('data-view="explorer"', body)   # sous-onglet supprimé
        self.assertNotIn("taxonomy_explorer.js", body)    # script supprimé
        self.assertIn("reclassify_apply.js", body)        # apply conservé (réutilisé)
```

- [ ] **Step 2: Lancer le test → il échoue (le sous-onglet existe encore)**

Run: `uv run python -m unittest tests.auto.test_explorer.TestMappingsPreviewToggle -v`
Expected: FAIL sur `assertNotIn('data-view="explorer"', body)` (la chaîne est encore présente).

- [ ] **Step 3: Retirer le bouton sous-onglet Aperçu du template**

Dans `dashboard/templates/taxonomy.html`, supprimer ces lignes (≈22-23) :

```html
            <button class="tax-subtab" data-view="explorer"
                    title="Aperçu Maintenant / Après — où iraient les fichiers au reclassify, sans rien déplacer">&#128270; Aperçu</button>
```

- [ ] **Step 4: Retirer la vue Aperçu du template**

Supprimer tout le bloc (≈420-451), du commentaire d'ouverture au commentaire de fermeture inclus :

```html
<!-- ────────────────────── View: Aperçu (Explorateur) ─────────────────────── -->
<div class="tax-view tax-view-explorer" data-view="explorer" style="display:none;">
  ... (tout le contenu : <style>, toggle expl-mode, expl-tree, expl-files) ...
</div>
<!-- ────────────────────── End view: Aperçu ───────────────────────────────── -->
```

- [ ] **Step 5: Retirer le `<script>` de l'explorer**

Supprimer la ligne (≈653) :

```html
<script src="/static/js/taxonomy_explorer.js"></script>
```

- [ ] **Step 6: Supprimer le fichier JS de l'explorer**

```bash
git rm dashboard/static/js/taxonomy_explorer.js
```

- [ ] **Step 7: Lancer le test → il passe**

Run: `uv run python -m unittest tests.auto.test_explorer.TestMappingsPreviewToggle -v`
Expected: PASS.

- [ ] **Step 8: Vérifier que le backend projection n'est PAS cassé**

Run: `uv run python -m unittest tests.auto.test_explorer -v`
Expected: PASS (TestBuildProjection, TestProjectionCache, TestExplorerRoutes intacts — on n'a touché ni `explorer.py` ni les routes).

- [ ] **Step 9: Commit**

```bash
git add dashboard/templates/taxonomy.html tests/auto/test_explorer.py
git rm dashboard/static/js/taxonomy_explorer.js
git commit -m "refactor(taxonomy): remove standalone Aperçu sub-tab (projection backend kept)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Barre d'outils Maintenant/Après dans la vue Mappings (template + CSS)

**Files:**
- Modify: `dashboard/templates/taxonomy.html` (vue `.tax-view-mappings`, ≈81-86)
- Test: `tests/auto/test_explorer.py` (`TestMappingsPreviewToggle`)

**Interfaces:**
- Consumes: rien.
- Produces: dans le DOM de Mappings — `#tax-preview-toggle` contenant deux boutons `.tax-preview-btn` (`data-preview="now"` actif, `data-preview="after"`), `#tax-preview-counter`, `#tax-preview-loading`, `#tax-preview-apply` (masqué), `#tax-preview-apply-status`. Ces ids/classes sont câblés en Task 3.

- [ ] **Step 1: Écrire le test qui exige le toggle dans Mappings**

Ajouter cette méthode à `TestMappingsPreviewToggle` dans `tests/auto/test_explorer.py` :

```python
    def test_mappings_has_preview_toggle(self):
        body = self.client.get("/taxonomy").text
        self.assertIn('id="tax-preview-toggle"', body)
        self.assertIn('data-preview="after"', body)
        self.assertIn('id="tax-preview-apply"', body)
```

- [ ] **Step 2: Lancer le test → il échoue**

Run: `uv run python -m unittest tests.auto.test_explorer.TestMappingsPreviewToggle.test_mappings_has_preview_toggle -v`
Expected: FAIL (`id="tax-preview-toggle"` absent).

- [ ] **Step 3: Ajouter la barre d'outils + CSS dans la vue Mappings**

Dans `dashboard/templates/taxonomy.html`, juste APRÈS le bloc `<div id="tax-onboarding-banner" ...>...</div>` (≈85) et AVANT `<div class="tax-grid" id="tax-grid">` (≈86), insérer :

```html
<style>
  .tax-preview-bar { display:flex; gap:12px; align-items:center; margin:6px 0 10px; flex-wrap:wrap; }
  .tax-preview-seg { display:inline-flex; border:1px solid var(--border,#30363d); border-radius:6px; overflow:hidden; }
  .tax-preview-btn { background:transparent; border:none; color:var(--text-muted,#8b949e); padding:5px 16px; cursor:pointer; font-size:12px; font-weight:500; }
  .tax-preview-btn:hover { background:rgba(255,255,255,0.04); color:var(--text,#c9d1d9); }
  .tax-preview-btn.active[data-preview="now"] { background:var(--blue,#58a6ff); color:#fff; }
  .tax-preview-btn.active[data-preview="after"] { background:var(--green,#3fb950); color:#06210f; }
  .tax-tree-file.moved-after { outline:1px solid rgba(63,185,80,.45); background:rgba(63,185,80,.07); }
  .tax-tree-file .efrom-badge { font-size:10px; color:var(--green,#3fb950); background:rgba(63,185,80,.12); border-radius:9px; padding:1px 7px; margin-left:6px; white-space:nowrap; }
</style>
<div class="tax-preview-bar">
  <div class="tax-preview-seg" id="tax-preview-toggle">
    <button class="tax-preview-btn active" data-preview="now" type="button">Maintenant</button>
    <button class="tax-preview-btn" data-preview="after" type="button">Après</button>
  </div>
  <span id="tax-preview-counter" class="muted small"></span>
  <span id="tax-preview-loading" class="muted small" style="display:none;"></span>
  <button id="tax-preview-apply" class="btn-primary btn-sm" type="button" style="display:none;">Appliquer cette projection</button>
  <span id="tax-preview-apply-status" class="muted small"></span>
</div>
```

- [ ] **Step 4: Lancer le test → il passe**

Run: `uv run python -m unittest tests.auto.test_explorer.TestMappingsPreviewToggle -v`
Expected: PASS (les 2 méthodes).

- [ ] **Step 5: Commit**

```bash
git add dashboard/templates/taxonomy.html tests/auto/test_explorer.py
git commit -m "feat(taxonomy): add Maintenant/Après toggle toolbar to Mappings view

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: État preview + chargement/bucketing de la projection + câblage (taxonomy.js)

**Files:**
- Modify: `dashboard/static/js/taxonomy.js` (état ≈30-104 ; nouvelles fonctions près de `fetchFiles` ≈203 ; câblage dans `init` ≈4108 et ≈4137)

**Interfaces:**
- Consumes: la barre d'outils de Task 2 (`#tax-preview-toggle`, `#tax-preview-counter`, `#tax-preview-loading`, `#tax-preview-apply`, `#tax-preview-apply-status`) ; `window.applyReclassify({profile, keyword, onStatus})` (reclassify_apply.js) ; routes `/api/explorer/projection` et `/api/explorer/status`.
- Produces (pour Task 4) : `state.previewMode` (`'now'`|`'after'`), `state.afterCounts` (`{folderPath: number}`), `state.afterFiles` (`{folderPath: [{name, rel_path, movedFrom}]}`), et la fonction `finalFolder(f)`.

**Note (pas de test JS) :** ce dépôt n'a pas de harnais JS ; la vérification est un **smoke manuel** (Step final). Aucun test unitaire JS n'est ajouté ; le filet auto reste au niveau template (Task 2).

- [ ] **Step 1: Ajouter les champs d'état preview**

Dans `dashboard/static/js/taxonomy.js`, dans l'objet `const state = { ... }`, juste après `folderBreakdown: new Map(),` (≈103) et avant le `};` de fermeture (≈104), ajouter :

```js
    // ── Aperçu Maintenant/Après (toggle DANS Mappings) ──────────────────
    // 'now'  = placement disque (snapshot, comportement historique).
    // 'after'= placement cible (projection) : fichiers rangés là où le
    // reclassify les mettrait. Voir setPreviewMode / bucketAfter.
    previewMode: 'now',
    projection: null,    // payload /api/explorer/projection quand status==='ready'
    afterCounts: {},     // { folderPath: nb de fichiers en mode Après }
    afterFiles: {},      // { folderPath: [{ name, rel_path, movedFrom|null }] }
```

- [ ] **Step 2: Ajouter `finalFolder`, `bucketAfter`, `loadProjection`, `setPreviewMode`**

Toujours dans `taxonomy.js`, juste après la fonction `fetchFiles` (qui se termine ≈203, ligne `}` après `return r.json();`), insérer ce bloc :

```js
  // Dossier d'un fichier en mode Après : sa cible si déplacé, sinon sa place
  // actuelle (les non-analysés / sans prédiction restent en place).
  function finalFolder(f) {
    return (f.predicted_folder && f.predicted_folder !== f.current_folder)
      ? f.predicted_folder : (f.predicted_folder || f.current_folder);
  }

  // Bucketise projection.files par dossier final → state.afterCounts + afterFiles.
  function bucketAfter(proj) {
    const counts = {};
    const files = {};
    for (const f of proj.files) {
      const folder = finalFolder(f);
      counts[folder] = (counts[folder] || 0) + 1;
      (files[folder] = files[folder] || []).push({
        name: f.rel_path.split('/').pop(),
        rel_path: f.rel_path,
        movedFrom: (f.current_folder !== folder) ? (f.current_folder || '(racine)') : null,
      });
    }
    state.afterCounts = counts;
    state.afterFiles = files;
  }

  // Charge la projection (build async + cache). Affiche un overlay tant que
  // status==='building', puis renvoie le payload ready. Lève en cas d'erreur.
  async function loadProjection() {
    const loading = $('#tax-preview-loading');
    const purl = `/api/explorer/projection?profile=${encodeURIComponent(state.profile)}`;
    let d = await (await fetch(purl)).json();
    while (d.status === 'building') {
      if (loading) {
        loading.style.display = '';
        loading.textContent = `Calcul de la projection… ${d.n_done || 0}/${d.n_total || '?'} fichiers`;
      }
      await new Promise((r) => setTimeout(r, 1000));
      const s = await (await fetch(`/api/explorer/status?profile=${encodeURIComponent(state.profile)}`)).json();
      if (s.status === 'error') { if (loading) loading.style.display = 'none'; throw new Error(s.error || 'erreur'); }
      d = (s.status === 'ready') ? await (await fetch(purl)).json()
                                 : { status: 'building', n_done: s.n_done, n_total: s.n_total };
    }
    if (loading) loading.style.display = 'none';
    if (d.status !== 'ready') throw new Error(d.error || 'projection indisponible');
    return d;
  }

  // Bascule le mode d'aperçu. Au 1er passage en 'after', charge + bucketise la
  // projection (reste en 'now' si échec). Re-render l'arbre dans tous les cas.
  async function setPreviewMode(mode) {
    if (mode === 'after' && !state.projection) {
      try { state.projection = await loadProjection(); bucketAfter(state.projection); }
      catch (e) { showToast('Projection : ' + e.message, 'error'); return; }
    }
    state.previewMode = mode;
    document.querySelectorAll('#tax-preview-toggle .tax-preview-btn')
      .forEach((b) => b.classList.toggle('active', b.dataset.preview === mode));
    const counter = $('#tax-preview-counter');
    const apply = $('#tax-preview-apply');
    if (mode === 'after' && state.projection) {
      const n = state.projection.summary.n_moving;
      if (counter) counter.textContent = `${n} fichier(s) bougeraient au reclassify`;
      if (apply) apply.style.display = n > 0 ? '' : 'none';
    } else {
      if (counter) counter.textContent = '';
      if (apply) apply.style.display = 'none';
    }
    state.filesByPath = new Map();   // la source des fichiers change → invalide le cache disque
    renderTree();
  }
```

- [ ] **Step 3: Câbler les boutons du toggle + le bouton Appliquer dans `init`**

Dans `init` (`taxonomy.js` ≈4105), juste après la ligne `$('#tax-reclassify').addEventListener('click', openReclassifyModal);` (≈4137), insérer :

```js
    // Toggle Maintenant/Après (aperçu DANS Mappings)
    document.querySelectorAll('#tax-preview-toggle .tax-preview-btn').forEach((b) => {
      b.addEventListener('click', () => setPreviewMode(b.dataset.preview));
    });
    const previewApply = $('#tax-preview-apply');
    if (previewApply) previewApply.addEventListener('click', () => {
      if (!state.projection) return;
      const status = $('#tax-preview-apply-status');
      window.applyReclassify({
        profile: state.profile,
        keyword: !!state.projection.flag_keyword,
        onStatus: (m) => { if (status) status.textContent = m; },
      }).then(() => withBusy('Rechargement…', async () => {
        state.projection = null;          // invalide après application
        await setPreviewMode('now');       // repasse en Maintenant
        state.filesByPath = new Map();
        state.snapshot = await fetchSnapshot(true);
        renderAll();
      })).catch((e) => { if (status) status.textContent = '✗ ' + (e.message || e); });
    });
```

- [ ] **Step 4: Réinitialiser le mode au changement de profil**

Dans le handler `sel.addEventListener('change', ...)` de `init` (≈4108-4118), juste après `state.filesByPath = new Map();` (≈4112), ajouter :

```js
      state.previewMode = 'now';          // l'aperçu repart sur Maintenant
      state.projection = null;
      state.afterCounts = {};
      state.afterFiles = {};
      document.querySelectorAll('#tax-preview-toggle .tax-preview-btn')
        .forEach((b) => b.classList.toggle('active', b.dataset.preview === 'now'));
      const pc = $('#tax-preview-counter'); if (pc) pc.textContent = '';
      const pa = $('#tax-preview-apply'); if (pa) pa.style.display = 'none';
```

- [ ] **Step 5: Smoke manuel (état + chargement)**

```bash
./klodo.sh dashboard
```
Profil `the-big-one`, Taxonomie ▸ Mappings, hard-reload (Cmd+Shift+R). Cliquer **Après** : un overlay « Calcul de la projection… » peut apparaître (1er passage), puis le compteur « N fichier(s) bougeraient » s'affiche et le bouton **Appliquer cette projection** apparaît. (L'arbre ne re-range pas encore les fichiers — c'est la Task 4 ; ici on valide juste le chargement, le compteur, le bouton, et que cliquer **Maintenant** vide le compteur.) Vérifier la console : aucune erreur JS.

- [ ] **Step 6: Vérifier les tests template + ruff (non-régression)**

Run: `uv run python -m unittest tests.auto.test_explorer -v && uv run ruff check tests/auto/`
Expected: PASS, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add dashboard/static/js/taxonomy.js
git commit -m "feat(taxonomy): preview state + projection load/bucketing + toggle wiring

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Rendu de l'arbre mode-aware (compteurs, expandabilité, liste de fichiers)

**Files:**
- Modify: `dashboard/static/js/taxonomy.js` (`renderTreeNode` ≈1360-1361 et ≈1494-1495 ; `lazyLoadFiles` ≈1585 ; nouvelle fonction `renderAfterFiles`)

**Interfaces:**
- Consumes (de Task 3) : `state.previewMode`, `state.afterCounts`, `state.afterFiles`, et `selectFile(relPath)` (existant, ≈2412), `FILE_PAGE` (=50, ≈27), `el(...)` (≈132).
- Produces : en mode Après, l'arbre affiche les compteurs cibles, rend les dossiers qui GAGNENT des fichiers expandables, et liste les fichiers cibles avec un badge « ← origine ».

**Note (pas de test JS) :** vérification par smoke manuel (Step final).

- [ ] **Step 1: Compteur + expandabilité mode-aware dans `renderTreeNode`**

Dans `taxonomy.js`, `renderTreeNode` (≈1359-1361), remplacer :

```js
    const hasChildren = node.children && node.children.length > 0;
    const hasFiles = (node.file_count || 0) > 0;
    const isExpandable = hasChildren || hasFiles;
```

par :

```js
    const hasChildren = node.children && node.children.length > 0;
    // Compte « direct » selon le mode : disque (snapshot) en Maintenant,
    // projection (afterCounts) en Après — sinon un dossier qui GAGNE des
    // fichiers en Après ne serait ni expandable ni rempli (file_count disque = 0).
    const directCount = (state.previewMode === 'after')
      ? (state.afterCounts[node.path] || 0) : (node.file_count || 0);
    const hasFiles = directCount > 0;
    const isExpandable = hasChildren || hasFiles;
```

- [ ] **Step 2: Badge de compte mode-aware**

Dans le même `renderTreeNode` (≈1494-1495), remplacer :

```js
    row.appendChild(el('span', { class: 'tax-tree-count', title: 'fichiers directs' },
      [String(node.file_count)]));
```

par :

```js
    row.appendChild(el('span', { class: 'tax-tree-count', title: 'fichiers directs' },
      [String(directCount)]));
```

- [ ] **Step 3: Brancher `lazyLoadFiles` vers le rendu Après**

Dans `taxonomy.js`, au tout début de `async function lazyLoadFiles(path, wrap, offset)` (≈1585), insérer en première instruction du corps :

```js
    if (state.previewMode === 'after') { renderAfterFiles(path, wrap, offset || 0); return; }
```

(Le reste de `lazyLoadFiles`, le chemin disque, est inchangé et n'est atteint qu'en mode Maintenant.)

- [ ] **Step 4: Ajouter `renderAfterFiles`**

Juste AVANT `async function lazyLoadFiles` (≈1585), insérer :

```js
  // Liste de fichiers d'un dossier en mode Après : depuis state.afterFiles
  // (projection), synchrone, paginée comme le chemin disque. Un fichier dont
  // le dossier d'origine diffère porte un badge « ← origine » + surbrillance.
  function renderAfterFiles(path, wrap, offset) {
    offset = offset || 0;
    if (offset === 0) wrap.innerHTML = '';
    const list = state.afterFiles[path] || [];
    if (!list.length) {
      wrap.appendChild(el('div', { class: 'muted small' }, ['Aucun fichier ici en mode Après.']));
      return;
    }
    const slice = list.slice(offset, offset + FILE_PAGE);
    for (const f of slice) {
      const isSel = state.selection.type === 'file' && state.selection.path === f.rel_path;
      let cls = 'tax-tree-file';
      if (isSel) cls += ' selected';
      if (f.movedFrom) cls += ' moved-after';
      const children = [
        el('span', { class: 'tax-tree-icon' }, ['📄']),
        el('span', { class: 'tax-tree-name' }, [f.name]),
      ];
      if (f.movedFrom) {
        children.push(el('span', { class: 'efrom-badge', title: 'Vient de ' + f.movedFrom }, ['← ' + f.movedFrom]));
      }
      wrap.appendChild(el('div', {
        class: cls, title: f.name, onclick: () => selectFile(f.rel_path),
      }, children));
    }
    const shown = offset + slice.length;
    if (shown < list.length) {
      wrap.appendChild(el('div', {
        class: 'tax-tree-more',
        onclick: (e) => { e.stopPropagation(); e.currentTarget.remove(); renderAfterFiles(path, wrap, shown); },
      }, [`⋯ Afficher ${Math.min(FILE_PAGE, list.length - shown)} fichiers de plus (${shown}/${list.length})`]));
    }
  }
```

- [ ] **Step 5: Smoke manuel (re-rangement)**

Dashboard, profil `the-big-one`, Mappings, hard-reload. Toggle **Après** :
- les compteurs des dossiers changent (un dossier cible gagne des fichiers, un dossier source en perd) ;
- déplier un dossier qui gagne des fichiers (compte > 0 alors qu'il était à 0 sur disque) → ses fichiers cibles s'affichent, ceux déplacés portent « ← origine » + surbrillance verte ;
- cliquer un fichier (mode Après) → le **Viewer** (colonne centrale) affiche sa couverture + carte LLM (le `rel_path` vient de la projection) ;
- toggle **Maintenant** → l'arbre revient exactement à l'état disque (compteurs et fichiers d'origine), aucun badge « ← ».
Console : aucune erreur JS.

- [ ] **Step 6: Commit**

```bash
git add dashboard/static/js/taxonomy.js
git commit -m "feat(taxonomy): mode-aware tree rendering (after-mode counts, expand, file list)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Vérification bout-en-bout + non-régression

**Files:** aucun changement de code — validation seule.

- [ ] **Step 1: Suite de tests complète**

Run: `uv run python -m unittest tests.auto.test_explorer tests.auto.test_taxonomy tests.auto.test_thumbnail -v`
Expected: PASS (template à jour, snapshot/files/viewer inchangés).

- [ ] **Step 2: Ruff**

Run: `uv run ruff check tests/auto/`
Expected: All checks passed.

- [ ] **Step 3: Smoke bout-en-bout (dashboard, profil `the-big-one`)**

1. Mappings ▸ **Maintenant** = arbre disque actuel (compteurs et fichiers historiques) — strictement inchangé.
2. Toggle **Après** → mêmes dossiers (squelette identique), compteurs re-calculés, fichiers déplacés dans leur dossier cible (badge ← origine + surbrillance).
3. Clic fichier dans les **deux** modes → Viewer (couverture + carte LLM).
4. **Appliquer cette projection** (mode Après) → l'apply global s'exécute (overlay statut), puis l'arbre recharge en Maintenant et reflète les déplacements réels.
5. Le sous-onglet « Aperçu » n'existe plus ; les autres sous-onglets (Catégories, Rename, Refonte, Dédupli) fonctionnent toujours.

- [ ] **Step 4: Revue finale**

Dispatcher un code reviewer (superpowers:requesting-code-review) sur l'ensemble des commits de la branche depuis le point de départ, puis finaliser via superpowers:finishing-a-development-branch (merge dans la branche d'intégration `feature/agents-onboarding`).

---

## Self-Review (auteur du plan)

**Spec coverage :**
- Suppression sous-onglet Aperçu (backend gardé) → Task 1. ✓
- Toggle dans la barre d'outils Mappings + compteur + Appliquer → Task 2 + 3. ✓
- « Maintenant » strictement inchangé → Tasks 3-4 ajoutent une branche `previewMode==='after'`, le chemin `now` est intact. ✓
- « Après » = même squelette, fichiers par dossier cible (counts + expandabilité + liste + badge origine) → Task 4. ✓
- Réutilisation viewer (clic fichier via `selectFile`) → Task 4 Step 4-5. ✓
- Réutilisation `applyReclassify` → Task 3 Step 3. ✓
- Cas non-analysé/sans-prédiction restent en place → `finalFolder` (Task 3 Step 2). ✓
- Build async + overlay → `loadProjection` (Task 3 Step 2). ✓
- Tests : template (Tasks 1-2), non-régression backend/snapshot/viewer (Tasks 1, 5). ✓

**Placeholder scan :** aucun TBD ; chaque step modifiant du code montre le code complet.

**Type/nom consistency :** `state.previewMode`/`afterCounts`/`afterFiles`, `finalFolder`, `bucketAfter`, `loadProjection`, `setPreviewMode`, `renderAfterFiles` cohérents entre Tasks 3 et 4 ; ids DOM (`tax-preview-toggle`, `tax-preview-counter`, `tax-preview-loading`, `tax-preview-apply`, `tax-preview-apply-status`) identiques entre Task 2 (markup) et Task 3 (câblage) ; classe `tax-preview-btn` + `data-preview` cohérentes ; `directCount` défini en Task 4 Step 1 et réutilisé Step 2.
