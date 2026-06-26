# Viewer de couverture dans l'Aperçu — Design

**Date** : 2026-06-26
**Branche** : `feature/explorer-preview`
**Statut** : design validé, prêt pour le plan d'implémentation

## Objectif

Ajouter, dans le sous-onglet **Taxonomie ▸ Aperçu** (l'Explorateur Maintenant/Après),
un **viewer de couverture** identique à celui du sous-onglet **Mappings** : couverture
PDF/ePub + navigation multi-pages + carte d'infos (titre/auteur/langue/confiance +
thèmes→dossier mappé), enrichi de la **projection du fichier** (Maintenant → Après).

Déclencheur : **cliquer un fichier** dans la liste de l'Aperçu.
Placement : **3ᵉ colonne à droite** (arbre · liste fichiers · viewer), reproduisant
la disposition de Mappings.

## Décisions validées (brainstorming)

1. **Placement** : 3ᵉ colonne dédiée à droite (`#expl-viewer`), toujours visible,
   comme la disposition arbre · viewer · infos de Mappings.
2. **Contenu de la carte** : identique à Mappings **+** une ligne de projection
   « Maintenant: X → Après: Y » avec badge source (`p1`/`p2`).
3. **Architecture** : **module partagé** `cover_viewer.js` consommé par Mappings ET
   l'Aperçu (DRY, rendu garanti identique). Mappings est refactoré pour l'utiliser.

## Contexte existant (réutilisé tel quel)

- **Endpoints backend** (génériques `profile` + `path`, aucun neuf à créer) :
  - `GET /api/taxonomy/file/metadata?profile=&path=` — `app.py:2459`,
    handler `taxonomy_file_metadata_api` (`taxonomy.py:1257`). Retourne
    `{ok, file:{rel_path, current_folder, page_count_estimate, …}, vision:{title,
    author, language, confidence, themes:[{theme, confidence, mapped_to}]}|null,
    prediction:{dest, used_theme, label}|null}`.
  - `GET /api/taxonomy/file/thumbnail?profile=&path=&page=` — `app.py:2467`,
    handler `taxonomy_file_thumbnail_api` (`taxonomy.py:1504`). Retourne le JPEG
    (cache content-key via `lib/thumbnail.py`, cap 5 pages), 404 sinon.
- **Viewer Mappings** (`taxonomy.js`) : `renderViewer` (1862-1903, cover + pager
  ◀/▶ + bande de vignettes), `renderLLMCard` (1913+, titre/auteur/langue/confiance
  + thèmes→`mapped_to`), `renderFileSection` (1821, orchestration + fetch metadata),
  `thumbnailURL(path, page)`, état `state.viewerCurrentPage`. CSS `.tax-viewer*`
  (`#tax-viewer`, `.tax-viewer-img`, `.tax-viewer-pager`, `.tax-viewer-empty`) dans
  `style.css`.
- **Aperçu** (`taxonomy_explorer.js`, IIFE, état `st = {loaded, mode, proj,
  expanded, selected, profile}`) : `render` (60), `renderFiles` (102-116). Chaque
  fichier de `st.proj.files` porte déjà `{rel_path, current_folder,
  predicted_folder, source, signal, confidence, top_theme, analyzed}` →
  **la projection Maintenant→Après est en mémoire**, aucun appel réseau pour elle.
- **Template** `dashboard/templates/taxonomy.html` : vue `data-view="explorer"`
  (~421-450), conteneur flex `#expl-tree` (360px) + `#expl-files` (flex:1). Viewer
  Mappings `#tax-viewer` (~120-132).

## Architecture

### Nouveau module `dashboard/static/js/cover_viewer.js`

Vanilla JS, exposé sur `window.CoverViewer` (pattern du dashboard : pas de bundler,
modules sur `window`). Fonctions **pures de rendu** (chaque appelant garde son propre
état de page/orchestration) :

- `CoverViewer.thumbnailURL(profile, path, page)` → string URL
  (`/api/taxonomy/file/thumbnail?...`). Déplacé depuis `taxonomy.js`.
- `CoverViewer.fetchMetadata(profile, path)` → `Promise<metadata>`
  (wrap `GET /api/taxonomy/file/metadata`).
- `CoverViewer.renderCover(container, {profile, path, page, pageCount, onPage})` —
  vide `container`, y construit l'`<img class="tax-viewer-img" src=thumbnailURL(...)>`
  (`onerror` → `.tax-viewer-empty` « Aperçu indisponible ») + le pager
  (`.tax-viewer-pager` : ◀/▶ + bande de vignettes + « pg N/M », bornes
  `[1, min(pageCount, 5)]`). Les boutons appellent `onPage(newPage)` ; l'appelant
  met à jour son état de page et rappelle `renderCover`. `pageCount` vient de
  `metadata.file.page_count_estimate`.
- `CoverViewer.renderCard(container, metadata, projection)` — vide `container`, y
  construit la carte : titre (ou « (sans titre) »), auteur, badge langue, badge
  confiance, liste thèmes (`theme` gras → `mapped_to` ou « (orphelin) »). Gère
  `metadata.vision === null` → « (non analysé) ».
  Si `projection` est fourni (objet `{currentFolder, predictedFolder, signal}`),
  **préfixe** un bloc projection : « Maintenant: `currentFolder` → Après:
  `predictedFolder ou (pas de prédiction)` » + badge `signal` (`p1`/`p2`) si présent.
  `projection` omis (Mappings) → pas de bloc projection.

### Refactor `taxonomy.js` (Mappings) — comportement inchangé

- `thumbnailURL` → délègue à / remplacé par `CoverViewer.thumbnailURL`.
- `renderViewer` → appelle `CoverViewer.renderCover(viewerEl, {profile, path,
  page: state.viewerCurrentPage, pageCount, onPage: (p) => { state.viewerCurrentPage
  = p; renderViewer(...); }})`. La logique de pager vit désormais dans le module.
- `renderLLMCard` → appelle `CoverViewer.renderCard(cardEl, metadata)` **sans**
  argument `projection` → aucune ligne Maintenant→Après (préserve l'actuel).
- Aucun changement de markup `#tax-viewer` ni de CSS.

### `taxonomy_explorer.js` (Aperçu)

- **État** : ajouter `selectedFile: null` (l'objet fichier) et `viewerPage: 1` à `st`.
- **3ᵉ colonne** : le template ajoute `#expl-viewer` au conteneur flex.
- **Sélection de fichier** : dans `renderFiles` (108-113), chaque `.expl-file` reçoit
  un `data-rel` et un `onclick` (ou délégation) → `selectFile(f)` :
  `st.selectedFile = f; st.viewerPage = 1;` + surbrillance de la ligne (classe
  `.selected`) + `renderViewerPanel()`.
- **`renderViewerPanel()`** :
  - `st.selectedFile == null` → `#expl-viewer` montre l'état vide
    « Sélectionne un fichier pour l'aperçu. ».
  - sinon : `CoverViewer.fetchMetadata(st.profile, f.rel_path)` →
    `CoverViewer.renderCover(coverEl, {profile: st.profile, path: f.rel_path,
    page: st.viewerPage, pageCount: meta.file.page_count_estimate,
    onPage: (p) => { st.viewerPage = p; renderViewerPanel(); }})`
    **+** `CoverViewer.renderCard(cardEl, meta, {currentFolder: f.current_folder,
    predictedFolder: f.predicted_folder, signal: f.signal})`.
    En cas d'échec du fetch → message «  Aperçu indisponible » dans `#expl-viewer`.
- **Reset** : un clic dossier (`renderNode` 90 → `st.selected = …`) et un changement
  de mode (toggle Maintenant/Après) **remettent `st.selectedFile = null`** →
  viewer en état vide. Le viewer est indépendant du mode (la carte montre toujours
  Maintenant ET Après).

### Layout (template + CSS)

- Conteneur flex de l'Aperçu :
  `#expl-tree` (flex:0 0 360px) · `#expl-files` (flex:1) · `#expl-viewer`
  (flex:0 0 340px, `max-height:68vh; overflow:auto`).
- Réutilise les classes `.tax-viewer*` existantes (rendu identique). Une classe
  conteneur légère (`.expl-viewer`) pour le cadre/scroll de la 3ᵉ colonne.
- `cover_viewer.js` chargé **avant** `taxonomy.js` et `taxonomy_explorer.js` dans
  `taxonomy.html` (dépendance `window.CoverViewer`).

## Flux de données

```text
clic dossier ──► renderFiles (liste) + reset selectedFile ──► viewer vide
clic fichier ──► st.selectedFile = f ──► renderViewerPanel
                    │
                    ├─ fetchMetadata(profile, f.rel_path)  ─► GET /file/metadata
                    ├─ renderCover  ─► <img src=GET /file/thumbnail?page=N>
                    └─ renderCard(meta, projection f)  (projection déjà en mémoire)
pager ◀/▶ ─► st.viewerPage = p ─► renderViewerPanel (re-render image)
```

## Cas limites

- **Fichier non analysé** (`f.analyzed === false`, `meta.vision === null`) : carte
  « (non analysé) » ; couverture = 1ʳᵉ page si dispo, sinon placeholder
  « Aperçu indisponible ». La ligne projection s'affiche quand même (Maintenant +
  Après, qui peut être « (pas de prédiction) »).
- **`predicted_folder` null** → « Après: (pas de prédiction) ».
- **Thumbnail 404** → `onerror` → `.tax-viewer-empty` (existant).
- **Metadata fetch KO** → message d'erreur dans `#expl-viewer`, pas de crash.
- **Fichier sélectionné puis filtré hors-vue** (changement de dossier/mode) :
  `selectedFile` est remis à null (voir Reset) → pas d'état incohérent.

## Tests

Le dépôt **n'a pas de harnais de test JS** ; la logique JS est validée par smoke
manuel. Le filet automatique porte sur le backend (déjà couvert) et le template.

- **Backend** : aucun endpoint neuf. `/file/metadata` et `/file/thumbnail` sont déjà
  testés (`test_taxonomy.py`, `test_thumbnail.py`).
- **Template / route** (style `test_dashboard.py` / `test_taxonomy.py`) :
  - la page Taxonomie charge `cover_viewer.js` (référence `<script>` présente) ;
  - la vue `explorer` contient `#expl-viewer`.
- **Non-régression Mappings** : `test_thumbnail.py` (31) et les tests viewer de
  `test_dashboard.py` restent **verts** après le refactor de `taxonomy.js`
  (aucun changement de contrat des endpoints/markup).
- **Smoke manuel** (profil `the-big-one`, hard-reload) :
  1. Aperçu → clic dossier → liste fichiers ; viewer en état vide.
  2. Clic fichier → couverture + pager + carte (titre/auteur/langue/confiance +
     thèmes→dossier) + ligne « Maintenant → Après » + badge source.
  3. Navigation pages ◀/▶ ; fichier non analysé → « (non analysé) » sans crash.
  4. Sub-tab **Mappings** : viewer toujours identique (non-régression).

## Découpage (pour writing-plans)

1. Créer `cover_viewer.js` (helpers + `renderCover` + `renderCard` avec bloc
   projection optionnel) en extrayant la logique de `taxonomy.js`.
2. Refactorer `taxonomy.js` (Mappings) pour consommer `CoverViewer.*` sans
   `projection` — vérifier non-régression (tests thumbnail/viewer + smoke Mappings).
3. Template : 3ᵉ colonne `#expl-viewer` + chargement de `cover_viewer.js` + CSS
   `.expl-viewer` ; test template (`cover_viewer.js` chargé, `#expl-viewer` présent).
4. `taxonomy_explorer.js` : état `selectedFile`/`viewerPage`, sélection de fichier
   (clic + surbrillance), `renderViewerPanel`, reset sur dossier/mode.
5. Smoke manuel bout-en-bout (Aperçu + non-régression Mappings).

## Hors périmètre (YAGNI)

- Pas de nouvel endpoint ni de changement backend.
- Pas de prefetch/cache supplémentaire des couvertures (le cache content-key suffit).
- Pas d'action depuis le viewer (mapper, déplacer) — lecture seule, comme l'Aperçu.
