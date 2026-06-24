# Explorateur « Maintenant / Après » — preview global du reclassify — Design

**Goal:** Un onglet **Explorateur** qui montre la bibliothèque sous deux modes basculables —
**Maintenant** (emplacement réel des fichiers sur le disque) et **Après** (où chaque fichier
atterrirait après un reclassify) — **sans rien déplacer**. L'apply devient une action explicite,
séparée, déclenchée depuis l'Explorateur. L'onboarding ne déplace plus automatiquement après le scan.

**Architecture:** Une projection par fichier (calculée par le **même chemin de classification que l'apply
réel** : `classify_combined` P1+P2, **sans LLM Mapper**) est construite **en tâche de fond** (~27 s sur
~18 k fichiers), **mise en cache** (cache dédié, invalidé par un hash config+Vision), puis renvoyée au
front. Le front construit les deux arbres + provenance + badges et **bascule Maintenant/Après
instantanément côté client**. L'apply réutilise le moteur `reclassify_apply` existant (preview → execute
→ undo) **avec le même flag de classification** que la projection affichée.

**Tech Stack:** FastAPI + Jinja2 + vanilla JS (`$()`), classifieur `lib/classifier.classify_combined`
(P1 `classify_by_theme` theme_mapping + P2 `KeywordClassifier`), pattern daemon-thread + status.json
(comme onboarding / `reclassify_apply`).

---

## 1. Problème

Le bouton onboarding étape 3 **« 🚀 Déplacer les fichiers classés » (`onb-move-btn`,
`dashboard/templates/onboarding.html:163`, handler `moveClassified()` l.518-562)** déclenche un
**déplacement réel sur disque** juste après le scan (via `reclassify_apply` preview → execute).
L'utilisateur ne veut **pas** d'application automatique : il veut **voir** d'abord, globalement, où
iraient les fichiers — un mode **« maintenant → après »** lecture seule — puis décider d'appliquer.

Le `dry-run` existant (`GET /api/taxonomy/reclassify/dryrun`) ne montre qu'un **agrégat + 50 fichiers
d'échantillon**, pas une exploration complète de la bibliothèque projetée.

## 2. Décisions validées (brainstorming)

1. **Forme** : un **explorateur** (arbre de dossiers + fichiers du dossier sélectionné) avec un
   **interrupteur de mode Maintenant/Après** ; en « Après », le même explorateur ré-affiche chaque
   fichier dans son **dossier final**.
2. **Emplacement** : un **nouvel onglet « Explorateur »** dédié.
3. **Annotations en mode Après** : **état final + provenance** (« ← venait de X » + badge +N/−N par
   dossier). ⚠️ **Ajusté** (cf. §8) : la **majorité** des fichiers bouge (~62 % mesuré) → l'affordance
   privilégie l'**état final lisible** avec provenance **compacte** (une ligne par fichier, pas de gros
   surlignage) ; le surlignage « ce qui a bougé » est un **toggle optionnel** (off par défaut).
4. **Apply** : un bouton **« Appliquer cette projection »** (mode Après) réutilise `reclassify_apply`
   (preview → execute → undo) **avec le même flag de classification** que la projection. **L'onboarding
   ne déplace plus** : à l'étape 3 le bouton ouvre l'Explorateur en mode Après.

## 3. Architecture & flux

```
Onglet « Explorateur »
  GET /api/explorer/projection?profile=X
     → si cache FRAIS (hash config+Vision inchangé) → renvoie la projection
     → sinon → lance un build en tâche de fond + renvoie {status:"building"}
  GET /api/explorer/status?profile=X      → {status: building|ready|error, n_done, n_total}
  POST /api/explorer/refresh?profile=X    → force un rebuild (config éditée)

  build (daemon, ~27 s / 18k fichiers) :
     explorer.build_projection(profile)
        → taxonomy._scan_and_classify(profile, include_step2=RECLASSIFY_INCLUDE_KEYWORD)
           (MÊME chemin que l'apply ; llm_mapper=None → pas de LLM Mapper)
        → mappe/enrichit chaque fichier (cf. §4.1) → met en cache {hash, files, summary}

  Front (1 charge, bascule client) :
     • Arbre MAINTENANT = fichiers groupés par `current_folder` (état disque réel)
     • Arbre APRÈS      = fichiers groupés par `final_folder`
                          (= `predicted_folder` s'il ≠ current, sinon `current_folder`)
     • Badge dossier +N/−N (entrants/sortants) · compteur global « N fichiers bougeraient »
     • Provenance compacte « ← venait de X » sur les fichiers déplacés (mode Après)
  Toggle [ Maintenant | Après ] = re-rendu instantané, AUCUN appel réseau.
  Bouton « Appliquer cette projection » → /reclassify/apply/preview?keyword=<MÊME flag> → execute → undo.
```

**Flag de classification unifié (verrou anti-dérive).** Il n'existe **aujourd'hui aucune source unique**
pour le flag P2 : la route `api_reclassify_apply_preview` (`dashboard/app.py:2096`) défaut à
`keyword=False`, l'onboarding passe `keyword=true` (`onboarding.html:524`), `build_reclassify_projection`
n'a pas de défaut. Mesuré : **829 fichiers** (profil `default`) bougent différemment selon ce flag. On
introduit donc **`taxonomy.RECLASSIFY_INCLUDE_KEYWORD = True`** (constante partagée), consommée par
`build_projection`. Le bouton **« Appliquer »** de l'Explorateur appelle la route preview en passant
**`keyword=RECLASSIFY_INCLUDE_KEYWORD`** — donc l'apply lancé depuis l'Explorateur utilise **exactement
le même flag** que la projection affichée. Invariant garanti : **« Après » de l'Explorateur == apply
déclenché depuis l'Explorateur.** (On ne touche pas au défaut de la route ni à la modale dry-run
Taxonomie, hors périmètre — mais on documente que l'Explorateur reflète l'apply **P1+P2**.)

## 4. Composants

### 4.1 Backend — `dashboard/explorer.py` (nouveau module)

```python
def build_projection(profile: str) -> dict:
    """Projection par fichier pour l'Explorateur. Lecture seule, aucun déplacement.

    Réutilise taxonomy._scan_and_classify(profile, include_step2=RECLASSIFY_INCLUDE_KEYWORD)
    (MÊME classifieur P1+P2 que l'apply, llm_mapper=None). Coûteux (~27 s / 18k fichiers)
    → appelé en tâche de fond, résultat mis en cache (cf. cache dédié ci-dessous).

    Mapping des champs (la sortie brute de _scan_and_classify diffère — voir NB) :
      confidence  ← top_confidence
      signal      ← 'p2' si source.startswith('Keyword') sinon 'p1' ; None si predicted_folder est None
      analyzed    ← nouveau flag (cf. extension _scan_and_classify ci-dessous)

    Retour:
      { "ok": bool,
        "files": [ {rel_path, current_folder, predicted_folder, source,
                    signal, confidence, top_theme, analyzed} ],   # TOUS les fichiers
        "summary": {n_total, n_moving, n_stable, n_no_prediction, n_unanalyzed},
        "flag_keyword": RECLASSIFY_INCLUDE_KEYWORD }   # réémis tel quel par le bouton Appliquer
    """
```

**NB — schéma de champs (3 conventions existent, à unifier ici).** `_scan_and_classify`
(`dashboard/taxonomy.py:989-992`) émet `{rel_path, current_folder, predicted_folder, source, score,
top_theme, top_confidence}` — **pas** de `signal`, et `top_confidence` (pas `confidence`).
`build_reclassify_projection` émet `proposed_folder`/`confidence`/`signal` (dérivé `source`, l.1148) mais
**ne garde que les fichiers qui bougent**. L'Explorateur a besoin de **TOUS** les fichiers → on part de
`_scan_and_classify` (qui les renvoie tous) et `build_projection` fait le mapping ci-dessus explicitement.

**Extension `_scan_and_classify` (additive, sûre).** Ajouter au dict par fichier un booléen
`analyzed` = `vision_cache.lookup(...) is not None` (calculé avant le fallback `result = result or {}`,
`taxonomy.py:985-988`). Additif → `build_reclassify_projection`/`reclassify_dryrun` l'ignorent. Permet
de distinguer `n_unanalyzed` (pas de cache Vision) de `n_no_prediction` (analysé mais sans prédiction).

**Compteurs `summary`** : `n_moving` = `predicted_folder` non null **et** ≠ `current_folder` ;
`n_stable` = `predicted_folder == current_folder` ; `n_no_prediction` = `analyzed` ET `predicted_folder`
null ; `n_unanalyzed` = `not analyzed`. ⚠️ Un fichier **non analysé peut quand même avoir un
`predicted_folder`** (P2 classe sur le **nom de fichier**, `lib/classifier.py:454-474`) → il **bouge**
malgré l'absence de Vision ; ce n'est donc pas un cas « rien ne bouge ».

**Cache dédié** (PAS celui des index Mappings, qui stocke une structure différente sans la classification
par fichier) : `_projection_cache[profile] = {"fresh_hash": str, "data": {...}}`.
- `fresh_hash` = hash de la config (réutiliser `reclassify_apply._config_hash` : theme_mapping +
  categories + tree + theme-canon) **+ signature du vision_cache** (mtime ou hash du
  `.cache/vision_cache.json`).
- À chaque `GET /api/explorer/projection`, comparer `fresh_hash` courant au cache : **frais** → servir ;
  **périmé ou absent** → déclencher un build de fond et renvoyer `{status:"building"}`. Cela résout la
  **péremption silencieuse** (éditer `categories.yaml` change le `_config_hash` → recalcul auto), sans
  dépendre d'une invalidation manuelle (vérifié : `categories.reset_cache` n'appelle pas
  `taxonomy.reset_cache`, `dashboard/categories.py:288-296`).
- Build = daemon thread écrivant la progression dans un `status.json`
  (`.cache/explorer/status.json`), comme `reclassify_apply`.

### 4.2 Backend — routes `dashboard/app.py`

```
GET  /api/explorer/projection?profile=X   → cache frais ? data : (lance build) {status:"building"}
GET  /api/explorer/status?profile=X        → {status: building|ready|error, n_done, n_total, error}
POST /api/explorer/refresh?profile=X       → invalide + relance le build
```

L'**apply réutilise les routes existantes** (vérifiées `dashboard/app.py:2095-2139`) — aucun nouveau
code moteur : `/api/taxonomy/reclassify/apply/{preview,execute,status,undo,pending}`. Le bouton Appliquer
passe `keyword=RECLASSIFY_INCLUDE_KEYWORD`.

### 4.3 Frontend — `dashboard/templates/explorer.html` + `dashboard/static/js/explorer.js`

- Entrée **« Explorateur »** dans la sidebar (`dashboard/templates/base.html:15-25`, même motif que les
  autres onglets).
- **État de chargement** : à l'ouverture, `GET projection` ; si `building` → afficher une **progression**
  (spinner + `n_done/n_total` via `GET status`, poll 1 s) pendant les ~27 s ; quand `ready` → fetch data
  + rendre. Idem après « Rafraîchir ».
- `explorer.js` : construit deux maps (`byCurrent`, `byFinal`) + un arbre imbriqué depuis les chemins →
  toggle Maintenant/Après (re-rendu instantané) → panneau fichiers (provenance compacte + signal +
  confiance en Après) → compteur global → bouton Appliquer (mode Après) → bouton Rafraîchir.
- **Virtualisation OBLIGATOIRE** (pas optionnelle) : ~18 k fichiers / payload ~5,7 Mo ; un dossier comme
  `_INBOX` peut contenir des milliers de fichiers → rendu fenêtré / « charger plus », jamais tout d'un
  coup. La bascule « instantanée » ne vaut **qu'après** la charge initiale (qui, elle, prend les ~27 s).
- **Affordance majorité-bouge** (~62 %) : en Après, état final lisible ; provenance « ← venait de X »
  compacte sur la ligne ; badges +N par dossier ; surlignage « déplacé » = **toggle off par défaut**.
- Affiche les dossiers ayant **≥ 1 fichier** dans le mode courant (pas les dossiers de taxonomie vides).

### 4.4 Frontend partagé — extraire le flux apply (nouvelle tâche)

Le flux JS de `onb-move-btn` (`moveClassified()` / `_rcaPollDone()` / `_moveShow()`,
`onboarding.html:502-562`) est **couplé au DOM onboarding** (globale `profileName`, const `_rcaBase`,
élément `onb-move-status`) → **pas réutilisable tel quel**. **Extraire** preview→execute→poll→undo dans
**`dashboard/static/js/reclassify_apply.js`** : `applyReclassify({profile, keyword, onStatus, onConfirm,
onDone})`. Le réutiliser depuis **onboarding** (régression : onboarding garde le même comportement
qu'avant, juste via le module) **et** `explorer.js`.

### 4.5 Branchement onboarding — `dashboard/templates/onboarding.html`

- **Remplacer** `onb-move-btn` (« 🚀 Déplacer les fichiers classés ») par **« 🔍 Voir dans
  l'Explorateur »** → navigue vers `/explorer?profile=<draft>&mode=after`. L'onboarding **ne déclenche
  plus** de déplacement. « Continuer dans Mappings » / « Finaliser » inchangés.
- L'Explorateur lit `tree.yaml`/`theme_mapping.yaml`/`categories.yaml` + `vision_cache.json` du profil
  brouillon → handoff naturel (un profil brouillon n'a pas de particularité bloquante pour la projection).

## 5. Cas limites

| Cas | Comportement |
|---|---|
| Pas de cache Vision (profil non scanné) | Les fichiers restent **classables par P2 sur le nom de fichier** → certains bougent quand même. Bandeau « analyse Vision absente : classement basé uniquement sur les noms de fichiers » (et **pas** « rien ne bougera »). |
| Orphelin (`analyzed`, sans prédiction) | Reste dans `current_folder`, `signal=null`, ne bouge pas. |
| Config éditée (mapping **ou** categories) | `fresh_hash` change → **recalcul auto** au prochain GET (pas de bandeau périmé silencieux). « Rafraîchir » force aussi. |
| `.cache/taxonomy.lock` actif (apply/écriture en cours) | L'apply renvoie **423** (via `taxonomy._check_lock_free`, déjà câblé dans `reclassify_apply`) → bouton Appliquer désactivé + état « occupé ». Aucun code lock neuf côté Explorateur. |
| Profil sans `target` / dossier illisible | `{ok:false, error}` ; l'UI affiche l'erreur. |
| Rien ne bouge (`n_moving == 0`) | Après == Maintenant ; message « rien ne bougerait au prochain reclassify ». |

## 6. Tests (Vision/LLM mockés, jamais réels)

- `tests/auto/test_explorer.py` (nouveau) :
  - `build_projection` : profil-fixture avec petit `vision_cache` → vérifie `current_folder` /
    `predicted_folder` / `signal` / `analyzed` par fichier + `summary`
    (n_moving/n_stable/n_no_prediction/n_unanalyzed). Réutilise les fixtures de `_scan_and_classify` /
    `reclassify_dryrun`.
  - **Verrou anti-dérive** : l'ensemble des fichiers `n_moving` de `build_projection(profile)` ==
    l'ensemble de `build_reclassify_projection(profile, include_keyword=RECLASSIFY_INCLUDE_KEYWORD)`
    (même constante des deux côtés — pas un `True` câblé arbitraire).
  - **`analyzed`** : fichier avec cache Vision → `analyzed=True` ; sans cache → `analyzed=False` **mais**
    peut avoir `predicted_folder` non null si le nom matche un mot-clé (n_unanalyzed ≠ « ne bouge pas »).
  - **Cache & péremption** : 2ᵉ GET servi par le cache ; après changement de `_config_hash` (édition
    categories) ou du vision_cache → `fresh_hash` diffère → rebuild déclenché.
- Routes : `GET /api/explorer/projection` (data ou `building`), `GET /api/explorer/status`,
  `POST /api/explorer/refresh` → formes correctes.
- Rendu page : `GET /explorer` contient le toggle + conteneurs arbre + entrée nav ; `GET /onboarding`
  **ne contient plus** `onb-move-btn`/`moveClassified` mais contient le handoff Explorateur.
- **Extraction JS** : un test de rendu vérifie qu'`onboarding.html` et `explorer.html` chargent
  `static/js/reclassify_apply.js` ; le comportement onboarding reste identique (non-régression du flux).
- **Apply** : moteur `reclassify_apply` déjà couvert par `test_reclassify_apply` ; un test vérifie
  seulement que le bouton Explorateur appelle `/reclassify/apply/preview` avec `keyword` =
  `RECLASSIFY_INCLUDE_KEYWORD` (le flag de la projection), pas le défaut `false` de la route.

## 7. Hors périmètre (YAGNI)

- **Projection reclassify uniquement** (un « Après » pour le **rename** = extension future).
- **Apply tout-ou-rien** (pas d'apply partiel par dossier).
- **Miniature/aperçu au clic** et **recherche/filtre** = bonus optionnels (réutiliseraient
  thumbnail/viewer), pas le cœur.
- On **ne modifie pas** le défaut `keyword=False` de la route apply preview ni la modale dry-run de
  Taxonomie : l'Explorateur passe toujours le flag explicitement.
- Optimisation du build (~27 s) — ex. cache de hash FS pour éviter le re-hash de 18 k fichiers — = piste
  future ; on s'appuie d'abord sur le cache de projection + le build de fond.

## 8. Risques / vigilance

- **Dérive preview ⇄ apply** : éliminée par la constante `RECLASSIFY_INCLUDE_KEYWORD` partagée (build +
  bouton Appliquer) + le test anti-dérive. Sans cela, « Après » mentirait (829 fichiers d'écart mesurés).
- **Coût du build (~27 s / 18 k fichiers, ~5,7 Mo)** : `_scan_and_classify` n'est **pas** caché et le
  coût n'est **pas** amorti par les index Mappings (erreur initiale corrigée). Mitigé par : build de fond
  + progression, cache de projection invalidé par hash, virtualisation de la liste. La bascule
  Maintenant/Après est instantanée **après** cette charge.
- **Majorité de fichiers bouge (~62 %)** : l'affordance « léger surlignage minoritaire » est recalibrée
  (état final lisible + provenance compacte + surlignage en toggle off). À valider visuellement au build.
- **Péremption** : résolue en stockant `fresh_hash` (config + vision) dans l'entrée de cache et en
  comparant à chaque GET (recalcul auto) — ne dépend pas d'une invalidation cross-module absente
  aujourd'hui.
- **« Après » = apply P1+P2 du dashboard** (sans LLM Mapper) : un run CLI avec Mapper pourrait différer ;
  on documente que l'Explorateur reflète l'apply du dashboard, pas un run CLI.
