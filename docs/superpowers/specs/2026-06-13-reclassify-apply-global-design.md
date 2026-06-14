# Apply global — synchroniser la bibliothèque avec la config live

**Date** : 2026-06-13
**Statut** : validé (brainstorming)
**Emplacement** : modale « Voir ce qui bougerait » (toolbar Mappings) + carte de rappel dans Overview

## Contexte & objectif

Quand l'utilisateur modifie sa classification dans le dashboard (onglet **Mappings**,
**Dédupli**, **Catégories**, création de dossiers), il édite en réalité 3 fichiers de
config live par profil : `theme_mapping.yaml`, `categories.yaml`, `theme-canon.json`
(+ `tree.yaml`). **Aucune de ces éditions ne déplace les fichiers existants** : la config
dit *où ils devraient être*, mais la bibliothèque physique reste figée. Le seul moyen
de réorganiser la bibliothèque aujourd'hui passe par un run de refonte
(PR #176 — `docs/superpowers/specs/2026-06-12-refonte-apply-execute-design.md`).

Ce chantier ajoute un **Apply global** : une action qui **synchronise physiquement la
bibliothèque avec la config live**, capturant l'effet cumulé de Mappings + Dédupli +
Catégories + dossiers, **en réutilisant le moteur de déplacement de PR #176** (journal,
undo, garde-fous, rapport, lock). Le **Rename est hors périmètre** (déjà appliqué
immédiatement sur disque, journal séparé). L'apply de la refonte reste tel quel.

Conceptuellement : la config live est l'unique source de vérité du « bon » placement ;
un seul apply global la projette sur la bibliothèque. Pas besoin d'un apply par onglet.

## Décisions actées (brainstorming)

| Décision | Choix |
| --- | --- |
| Fidélité de la cascade | **Déterministe P1+P2** (pas de P3 LLM Mapper payant) — l'apply applique ce que ta config détermine, gratuit et reproductible |
| Quoi déplacer par défaut | **P1** (theme_mapping + canonicalisation) déplacé ; **P2** (mots-clés) en **case à cocher opt-in** (faux positifs connus) ; sans prédiction → reste en place |
| Dédupli | **Corriger la canonicalisation** : canonicaliser les thèmes avant le lookup theme_mapping, dans le chemin de classification partagé, pour que Dédupli affecte enfin les destinations |
| Projection | **Figée** : calculée une fois au preview, écrite en CSV, rejouée à l'identique à l'exécution |
| Architecture | **Généraliser le moteur** (un seul moteur de moves, deux sources) ; nouveau module mince `dashboard/reclassify_apply.py` ; pas de couche « Adopter » |
| État & « en attente » | État global unique par profil + **hash de config** pour le badge « modifié depuis le dernier apply » (instantané, sans scan) |
| Scope | **Mono-profil** (profil sélectionné, une op à la fois) |
| Fencing | Étendre le lock à `categories.py` ; Rename reste non-fencé (skip stale propre) |

## Architecture

### Généralisation du moteur (couche B partagée)

Un seul changement de signature dans `dashboard/agent_refonte_apply.py` rend le moteur
source-agnostique :

- `_run_moves(profile, run_id)` → `_run_moves(profile, moves: list[dict], apply_id: str)`
- `_execute_job(profile, run_id)` → `_execute_job(profile, apply_id, moves)`
- `start_execute` reste le point d'entrée refonte : il construit `moves` via
  `select_move_rows(_run_dir(...))` puis appelle le moteur.

Restent partagés **inchangés** : `_spawn`, `_prune_empty_dirs`, `_safe_target_subdir`
(pré-vol anti-évasion), `lib/move_journal.py` (journal **déjà par-profil**, undo), le
lock sentinelle. Le contrat du moteur devient : `(profile, liste de moves enrichis,
apply_id unique)`. Le code de déplacement dangereux existe **en un seul exemplaire**.

`moves` est une liste de dicts portant au minimum `rel_path` et `proposed_folder`
(mêmes champs que les lignes enrichies de `select_move_rows`).

### Nouveau module `dashboard/reclassify_apply.py`

La glue spécifique au global, calquée sur le découpage de `agent_refonte_apply.py` :

- `build_projection(profile, include_keyword)` → matérialise la projection live
  complète (voir §Projection) et l'écrit figée.
- état `read_state`/`write_state`, preview, `start_execute_global`, `start_undo_global`,
  `get_status` — réutilisent le moteur partagé et le journal partagé.
- **Pas de couche A** (`adopt_structure`) : la config est déjà live.

### Flux (couche B uniquement)

```text
Preview (scan ~20s → projection figée)  →  Confirmer  →  Exécuter (moves + journal + rapport)
                                                                      │
                                                                 ↩ Annuler (undo_batch)
```

### État (un seul par profil)

```text
profiles/<p>/.cache/reclassify/apply/
  ├── state.json        # {executed, executed_at, move_batch_id, include_keyword,
  │                     #  n_moved, n_failed, n_skipped, rolled_back_moves,
  │                     #  last_applied: {ts, config_hash}}
  ├── status.json       # progression live (op, n_done, n_total, n_failed, n_skipped, error)
  └── projection-<ts>.csv  # projection figée du preview courant
```

### Exécution & concurrence

- Thread daemon + polling `status.json` toutes les 2 s (patron `agent_refonte.py`).
- Lock sentinelle `.cache/taxonomy.lock` (contenu `reclassify-apply`) posé pendant
  les moves, retiré en `finally`. Execute **et** undo sérialisés par-profil via
  `taxonomy._locks[profile]` + détection de lock zombie (status `running` périmé
  via mtime + timeout, comme refonte).
- **Mono-profil** : l'action porte sur le profil sélectionné, une op à la fois.

## Projection live & canonicalisation

### Source : `build_reclassify_projection(profile, include_keyword)`

`reclassify_dryrun` (`dashboard/taxonomy.py:870-1078`) ne renvoie qu'un échantillon
(`sample_moves`). On ajoute une fonction sœur qui réutilise le même scan
(`os.walk` du target, 8 workers, ~18-22 s sur ~19k fichiers, 100 % read-only) +
`classify_combined`, mais **matérialise la liste complète** des moves, mêmes colonnes
que la projection refonte : `rel_path, current_folder, proposed_folder, changed,
source, top_theme, confidence`.

Sélection :

- **P1** (`source` = `LLM (theme)` / `LLM (theme→refined)`) et `changed` → inclus
  par défaut.
- **P2** (`source` = `Keyword`) → inclus **seulement si `include_keyword=True`**.
- Sans prédiction (`FAILED`) ou `proposed_folder` vide → exclu (reste en place).
- Pas de P3 : `llm_mapper=None` (déterministe).

### Projection figée

Au *Preview*, la projection est calculée une fois (avec le choix P2 courant) et écrite
dans `projection-<ts>.csv` ; son chemin et `include_keyword` sont enregistrés dans
`state.json`. L'exécution rejoue **exactement** ce fichier (preview == exécution,
reproductible, scan unique) — refaire le preview avec/sans P2 réécrit la projection
figée. Le pré-vol anti-évasion (`_safe_target_subdir` sur `rel_path` et
`proposed_folder`) s'applique à toute la projection avant le premier move.

### Correction de la canonicalisation (chemin partagé)

Aujourd'hui `reclassify_dryrun` passe les thèmes bruts du vision_cache à
`classify_combined` **sans** `canonicalize()` (contrairement à l'agrégation UI
`dashboard/taxonomy.py:228-236`). Donc Dédupli (`theme-canon.json`) **n'affecte pas
les destinations**. Correctif :

- Étendre `lib/classifier.classify_combined` (et `classify_by_theme`) avec un
  paramètre optionnel `canon_table: dict[str, str] | None = None`. Quand fourni, le
  thème est canonicalisé via `lib/theme_canon.canonicalize(theme, canon_table)`
  **avant** le lookup theme_mapping (P1).
- Les appelants qui ont un profil chargent la table via
  `theme_canon.load_canon_table(profile)` et la passent : `build_reclassify_projection`,
  `reclassify_dryrun`, le simulateur refonte (`agents/refonte/simulator.py`), et le
  chemin de classification de production (CLI) — pour que l'apply ne déplace **pas**
  vers des endroits qu'un classify ultérieur défait.
- **Smoke test 3-5 PDFs réels obligatoire** (règle projet : pipeline touché).

## UI

### Modale « Voir ce qui bougerait » étendue (toolbar Mappings)

La modale existante (`#tax-reclassify-modal`, déclenchée par `#tax-reclassify`)
affiche déjà stats + top destinations + échantillon de moves
(`renderReclassifyBody`, `dashboard/static/js/taxonomy.js:654-769`). On l'étend en
flux complet :

- **Case « inclure les matchs par mot-clé (P2) »** (décochée par défaut) → re-fetch
  preview avec `include_keyword`.
- Répartition par signal visible (N P1 vs N P2) avant action.
- Bouton **« Appliquer (N) »** → `showConfirm({variant:'danger'})` (récap : N P1,
  N P2 si cochés, top destinations, rappel annulable) → POST execute.
- **Barre de progression** pollée (2 s) pendant les moves ; bouton désactivé en vol.
- **« Annuler le dernier apply »** quand `executed` (via `undo_batch`).
- Lien vers le rapport `logs/rapport_apply_*.csv` ; affichage séparé n_failed /
  n_skipped.

Composants réutilisés sans refacto : `showConfirm`, `showImpactModal`, busy overlay
`#tax-busy` + `withBusy()`, toasts, polling `setInterval(2000)`.

### Carte de rappel dans Overview

Une carte profile-aware : **« Bibliothèque : config modifiée depuis le dernier apply ·
Synchroniser »** (état dérivé du **hash de config**, instantané) qui pointe vers la
modale. Donne l'entrée « globale » découvrable sans dupliquer l'UI. Si le hash courant
== `last_applied.config_hash` → « bibliothèque à jour ». Le compteur coûteux « N
fichiers bougeraient » n'est calculé qu'à l'ouverture de la modale (lazy, busy
overlay).

### Hash de config (détection « en attente »)

Après chaque apply réussi : `last_applied.config_hash` = hash du contenu de
`theme_mapping.yaml + categories.yaml + theme-canon.json + tree.yaml`. Comparaison
instantanée hash courant vs stocké → badge sans scan.

## Routes API (dans `app.py`, logique dans `reclassify_apply.py`)

| Route | Effet | Erreurs |
| --- | --- | --- |
| `GET /api/taxonomy/reclassify/apply/preview?profile=&keyword=` | Construit + fige la projection, retourne compteurs (n_moves, n_p1, n_p2, n_stable, top destinations, état, pending) | 400 profil sans target, 423 lock |
| `GET /api/taxonomy/reclassify/apply/pending?profile=` | Comparaison hash de config (instantané, sans scan) → `{pending: bool}` | 400 |
| `POST /api/taxonomy/reclassify/apply/execute` `{profile}` | Lance le thread de déplacements de la **projection figée** du dernier preview (le choix P2 est déjà encodé dans le CSV figé — l'exécution ne le reprend pas) | 409 (op en cours / pas de projection figée), 423 lock, 400 target |
| `POST /api/taxonomy/reclassify/apply/undo` `{profile}` | Annule le dernier batch de moves global (thread) | 409 (rien à annuler / op en cours) |
| `GET /api/taxonomy/reclassify/apply/status?profile=` | `state.json` + `status.json` fusionnés (poll) | 404 |

## Garde-fous & erreurs (hérités du moteur PR #176)

- Pré-vol anti-évasion sur toute la projection avant le premier move.
- Par fichier : garde fraîcheur (stale) → garde collision (jamais d'écrasement) →
  `os.rename` → journal (move puis journal).
- Échec individuel = skip + rapport, **jamais d'abort** ; rapport CSV horodaté.
- Rollback complet via `undo_batch` (journal partagé, par-profil).
- Trou de journal sur crash documenté (≤ 1 record).
- Rename concurrent pendant un apply → skip `stale` propre, rapporté (pas de
  corruption) — à documenter dans un guide opérateur.

## Tests (tout en tmpdir, zéro SSD réel, zéro LLM)

- **`build_reclassify_projection`** : matérialisation complète, **canonicalisation
  appliquée** (un thème variant route vers le dossier du thème canonique), split
  P1/P2 (`include_keyword`), colonnes, exclusion sans-prédiction. Fixture = profil +
  target tmp + `vision_cache.json` + `theme_mapping.yaml` + `categories.yaml` +
  `theme-canon.json`.
- **Canonicalisation `classify_combined`** : unitaire (thème variant → dossier
  canonique avec `canon_table`, comportement inchangé sans) + non-régression du
  pipeline + **smoke test 3-5 PDFs réels**.
- **Module `reclassify_apply`** : état, hash de config (détection pending), preview
  (projection figée écrite), execute (P1 seul par défaut, P2 si coché), gating, lock
  423, undo, idempotence.
- **Non-régression refonte** : les 51 tests de `test_refonte_apply.py` verts après le
  changement de signature `_run_moves`.
- **Routes HTTP** du global + test du fencing `categories.py` (423 pendant un apply).

## Hors périmètre (YAGNI)

- **P3 (LLM Mapper)** : déterministe seulement.
- **Rename** : déjà appliqué immédiatement, journal séparé — pas d'objet à orchestrer.
- **Multi-profil** : une op par profil à la fois.
- Historique multi-applies dédié (le journal des moves porte déjà l'audit).
- Fencing de `baseline_run` (CLI, hors dashboard) — noté mais non traité.

## Fichiers critiques

- `dashboard/agent_refonte_apply.py:345-492` — généralisation `_run_moves`/`_execute_job`.
- `dashboard/reclassify_apply.py` — **nouveau** module global.
- `dashboard/taxonomy.py:870-1078` — `reclassify_dryrun` + `build_reclassify_projection`.
- `lib/classifier.py` (`classify_combined`, `classify_by_theme`) — param `canon_table`.
- `lib/theme_canon.py:551,586` — `load_canon_table`, `canonicalize` (réutilisés).
- `dashboard/categories.py:374,429,492` — ajout `_check_lock_free`.
- `lib/move_journal.py` — journal/undo partagés (inchangés).
- `dashboard/static/js/taxonomy.js:511-769` + `templates/taxonomy.html` — modale étendue.
- `agents/refonte/simulator.py` — passer `canon_table` (cohérence).
