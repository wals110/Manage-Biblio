# Toggle Maintenant/Après dans Mappings — Design

**Date** : 2026-06-26
**Branche** : `feature/explorer-preview`
**Statut** : design validé (maquette confirmée), prêt pour le plan d'implémentation
**Remplace** : `2026-06-26-explorer-cover-viewer-design.md` (caduque — le viewer vit
déjà dans Mappings) et la notion de sous-onglet « Aperçu » autonome.

## Objectif

Faire vivre l'aperçu **Maintenant / Après** **dans le sous-onglet Mappings** (pas un
sous-onglet séparé), sous forme d'un simple **toggle**. « Maintenant » = l'arbre
Mappings actuel (fichiers dans leur dossier disque). « Après » = **exactement le même
arbre**, mais chaque fichier est rangé dans son **dossier cible** (là où le reclassify
le mettrait). **Seul le placement des fichiers change** : même style d'arbre, même
viewer, mêmes colonnes Thèmes.

Le sous-onglet « Aperçu » autonome (`taxonomy_explorer.js`) est **supprimé** ; son
backend de projection est **conservé et réutilisé** par le toggle.

## Décisions validées

1. **Placement** : toggle dans la barre d'outils de la vue Mappings (pas de sous-onglet
   séparé). Maquette HTML validée par l'utilisateur.
2. **« Maintenant » = strictement l'actuel** (snapshot disque + lazy-load disque,
   aucun changement de comportement).
3. **« Après » = même squelette d'arbre + même viewer**, fichiers bucketés par
   **dossier final** (`predicted_folder` si déplacé, sinon `current_folder`).
4. **Approche** : overlay côté client réutilisant le backend de projection existant
   (`build_projection`). Aucun nouveau backend.

## Contexte existant (cartographie)

### Arbre Mappings (« Maintenant », inchangé)
- Snapshot : `GET /api/taxonomy/snapshot` (`app.py:2025`) → `get_snapshot`
  (`taxonomy.py:521`). Arbre via `_tree_hierarchy` (`taxonomy.py:133`). Chaque nœud :
  `{name, path, file_count, in_config, on_disk, children}`. `file_count` = fichiers
  **directs** du dossier, issus du scan disque `_scan_disk` (`taxonomy.py:472`).
- Rendu JS : `fetchSnapshot` (`taxonomy.js:183`), `renderTree` (`taxonomy.js:1216`),
  `renderTreeNode` (`taxonomy.js:1351`, badge compte ~1494).
- Fichiers d'un dossier (lazy) : `GET /api/taxonomy/folder/files` (`app.py:2035`) →
  `list_files_in_folder` (`taxonomy.py:636`, scandir disque). JS : `fetchFiles`
  (`taxonomy.js:197`), `lazyLoadFiles` (`taxonomy.js:1585`). Chaque ligne fichier
  ne porte que `{name}`.

### Projection (alimente « Après », réutilisée telle quelle)
- `build_projection(profile)` (`explorer.py:24`) → pour CHAQUE fichier :
  `rel_path, current_folder, predicted_folder, source, signal, confidence, top_theme,
  analyzed`. Exposé par `GET /api/explorer/projection` (`app.py:2143`, build async +
  cache + `/status` + `/refresh`). Renvoie `{status, files, summary, flag_keyword,
  tree_folders}`.
- Pas d'agrégation backend par dossier cible : le bucketing se fait côté client
  (déjà le cas dans `taxonomy_explorer.js:66`, qu'on porte dans `taxonomy.js`).

### Sous-onglet Aperçu (supprimé)
- `taxonomy.html` : bouton `data-view="explorer"` (l.22), vue (l.421–450), `<script
  src=".../taxonomy_explorer.js">` (l.653).
- `dashboard/static/js/taxonomy_explorer.js` (IIFE complète) → **supprimé**.
- Binding `explorer` dans `activateSubtab` (`taxonomy_categories.js`) → retiré.

## Architecture (approche retenue : overlay client)

### Suppression (le sous-onglet Aperçu disparaît)
- Retirer le bouton sous-onglet `data-view="explorer"` (`taxonomy.html:22`), la vue
  `data-view="explorer"` (`taxonomy.html:421–450`), le `<script>` `taxonomy_explorer.js`
  (`taxonomy.html:653`).
- Supprimer le fichier `dashboard/static/js/taxonomy_explorer.js`.
- Retirer la clause `explorer` du binding `activateSubtab` (`taxonomy_categories.js`).

### Conservation & réutilisation
- `dashboard/explorer.py` (`build_projection`, cache, status) **inchangé**.
- Routes `/api/explorer/projection|status|refresh` **inchangées** (alimentent le mode
  Après). NB : le nom « explorer » devient un détail interne ; pas de renommage
  (hors périmètre, éviter le churn).
- `dashboard/static/js/reclassify_apply.js` (`window.applyReclassify`) **réutilisé**
  pour le bouton Appliquer.

### Ajouts dans Mappings

**Template (`taxonomy.html`, vue `.tax-view-mappings`)** : une barre d'outils en haut
de la vue, au-dessus des 3 colonnes :
- Segmented **Maintenant / Après** (classe dédiée, ex. `tax-preview-btn` — PAS
  `tax-subtab`, pour ne pas collisionner avec le binding générique des sous-onglets).
- Un compteur (`#tax-preview-counter`).
- Un bouton **Appliquer cette projection** (`#tax-preview-apply`), **masqué hors
  mode Après**.

**`taxonomy.js`** :
- État : `state.previewMode = 'now' | 'after'` (défaut `'now'`), `state.projection = null`,
  `state.afterCounts = {}`, `state.afterFiles = {}`.
- `finalFolder(f)` (porté depuis `taxonomy_explorer.js:13`) :
  `predicted && predicted !== current ? predicted : (predicted || current)`.
- Toggle → `setPreviewMode(mode)` :
  - `'after'` : si `state.projection` absent, `GET /api/explorer/projection`
    (+ polling `/status`, overlay « Calcul de la projection… » sur la colonne arbre si
    `status==='building'`). À réception : construire `afterCounts[folder]` et
    `afterFiles[folder]` (liste d'objets `{name, rel_path, movedFrom}`) en bucketant
    `projection.files` par `finalFolder`. Puis `renderAll()`.
  - `'now'` : `renderAll()` (source disque, comportement actuel).
  - Met à jour la classe active des boutons, le compteur (`summary.n_moving`), la
    visibilité du bouton Appliquer.
- `renderTreeNode` : en mode `'after'`, le badge de compte lit
  `state.afterCounts[node.path] || 0` au lieu de `node.file_count`. Squelette d'arbre
  **identique** (mêmes nœuds : les `predicted_folder` sont des dossiers de `tree.yaml`,
  déjà dans le snapshot).
- Liste de fichiers d'un dossier :
  - `'now'` : `lazyLoadFiles` disque **inchangé**.
  - `'after'` : rendu **synchronique** depuis `state.afterFiles[path]` (pas d'appel
    disque). Un fichier dont `current !== final` porte un badge **← `current_folder`**
    + surbrillance « déplacé » (cf. maquette).
- Le **viewer** (`renderFileSection`, clic fichier) marche dans les deux modes : le
  `rel_path` est connu (lignes disque en Maintenant, projection en Après).
- Bouton Appliquer → `window.applyReclassify({profile, keyword: !!projection.flag_keyword,
  onStatus})` puis invalide la projection et recharge (motif de `taxonomy_explorer.js:130`).

## Flux de données

```text
Maintenant : snapshot disque (counts) + /folder/files (lazy) → arbre actuel
toggle → Après :
  1er passage  → GET /api/explorer/projection (+poll si building)
              → bucket par finalFolder → afterCounts / afterFiles
  renderTree   → badge compte = afterCounts[path]
  expand dossier → liste = afterFiles[path] (badge ← origine si déplacé)
  clic fichier → viewer Mappings (rel_path de la projection)
  Appliquer    → window.applyReclassify
toggle → Maintenant : re-render source disque (inchangé)
```

## Cas limites

- **Fichier non analysé / sans prédiction** (`analyzed=false` ou `predicted_folder`
  vide) → `finalFolder` = `current_folder` → reste à sa place en Après.
- **Squelette identique** : aucun dossier nouveau en Après (les cibles sont des
  dossiers `tree.yaml` déjà présents dans le snapshot). Un dossier peut se vider
  (compte 0) ou se remplir — même arbre, compteurs différents.
- **1ᵉʳ passage en Après non caché** : build ~quelques secondes (overlay), ensuite
  instantané (cache projection). `/refresh` invalide le cache.
- **Projection en erreur** (`status` erreur) → message dans l'overlay, le toggle
  retombe sur Maintenant.

## Tests

Pas de harnais JS dans le dépôt → la logique JS est validée par smoke manuel ; le
filet automatique porte sur le backend (réutilisé, déjà couvert) et le template.

- **Backend** : aucun endpoint neuf. `build_projection` + `/api/explorer/*` déjà
  couverts par `test_explorer.py` → restent verts.
- **Template / route** (`test_taxonomy.py` / `test_dashboard.py` style) :
  - le sous-onglet Aperçu a **disparu** : la page n'a plus de bouton
    `data-view="explorer"` ni de `<script taxonomy_explorer.js>` → MAJ/retrait des
    assertions `TestExplorerSubtab`.
  - la vue Mappings contient le toggle (`#tax-preview-counter` / boutons
    `tax-preview-btn`) et le bouton `#tax-preview-apply`.
- **Non-régression** : `test_taxonomy.py` (snapshot + `/folder/files` inchangés) et
  `test_thumbnail.py` (viewer) restent verts.
- **Smoke manuel** (profil `the-big-one`, hard-reload) :
  1. Mappings → toggle **Maintenant** = arbre disque actuel (inchangé).
  2. Toggle **Après** → mêmes dossiers, compteurs re-calculés, fichiers déplacés
     apparaissent dans leur dossier cible (badge ← origine).
  3. Clic fichier (les 2 modes) → viewer (couverture + carte LLM).
  4. **Appliquer cette projection** (mode Après) → apply global puis recharge.
  5. Le sous-onglet « Aperçu » n'existe plus.

## Découpage (pour writing-plans)

1. **Supprimer le sous-onglet Aperçu** : bouton + vue + `<script>` (template), fichier
   `taxonomy_explorer.js`, clause `explorer` du binding `activateSubtab` ; MAJ tests
   `TestExplorerSubtab`. (Backend projection conservé.)
2. **Barre d'outils Mappings** : toggle Maintenant/Après + compteur + bouton Appliquer
   (template + CSS `tax-preview-*`) ; test template.
3. **`taxonomy.js` — état + chargement projection** : `previewMode`, `projection`,
   `finalFolder`, `setPreviewMode` (fetch + poll + overlay + bucketing
   `afterCounts`/`afterFiles`).
4. **`taxonomy.js` — rendu mode-aware** : `renderTreeNode` (compte Après), liste de
   fichiers Après (depuis `afterFiles`, badge ← origine), bouton Appliquer câblé.
5. **Smoke bout-en-bout** (toggle, viewer dans les 2 modes, Appliquer, non-régression
   Maintenant + sous-onglets restants).

## Hors périmètre (YAGNI)

- Pas de nouveau backend ni d'agrégation serveur (bucketing client).
- Pas de renommage des routes `/api/explorer/*` (churn inutile).
- Pas de modification des colonnes Viewer / Thèmes (inchangées par le toggle).
- Pas d'action depuis l'arbre Après autre que voir/Appliquer (lecture seule).
