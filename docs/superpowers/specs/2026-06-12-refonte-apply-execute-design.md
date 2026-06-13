# Refonte — Apply / Execute (adoption + déplacements physiques)

**Date** : 2026-06-12
**Statut** : validé (brainstorming)
**Onglet** : Taxonomie → 🤖 Refonte

## Contexte & objectif

La Phase B de l'agent Refonte produit une proposition complète (arbre cible,
mappings, projection fichier-par-fichier) que l'utilisateur revoit dans l'UI
(onglets Verdict / Arbre / Plan, PR #174). Mais rien ne permet de l'**adopter** :
aujourd'hui il faudrait éditer les YAML à la main puis déplacer les fichiers en
CLI. Ce chantier ajoute l'application de la refonte **depuis le dashboard, avec
garde-fous** (preview, confirmation, backup, journal, rollback) — plus sûr que
la CLI car le préalable de revue est intégré au flux.

## Décisions actées

| Décision | Choix |
| --- | --- |
| Périmètre | Une spec, **deux actions UI séquencées** : ① Adopter la structure (config) puis ② Exécuter les déplacements (moves physiques) |
| Fichiers déplacés | **Tout sauf « à vérifier »** (critère existant `_is_doubt` : tout ce qui n'est pas P1/P1-raffiné avec confiance ≥ 0.7 reste en place) |
| Échec en cours de batch | **Skip + continuer + rapport** (jamais d'abort global) |
| Source des moves | **Projection figée** `simulation/reclassify-projection.csv` du run (ce qui a été revu = ce qui est exécuté) + garde de fraîcheur à l'exécution |
| Rollback | Couche A : restore du snapshot config. Couche B : `undo_batch` d'un journal de moves (nouveau `lib/move_journal.py` miroir de `rename_journal.py`) |

## Architecture

### État d'application par run

```text
profiles/<p>/.cache/refonte/<run_id>/apply/
  ├── state.json    # {adopted, adopted_at, config_backup, move_batch_id,
  │                 #  executed, executed_at, n_moved, n_failed, n_skipped,
  │                 #  rolled_back_config, rolled_back_moves}
  └── status.json   # progression live (op, n_done, n_total, n_failed, error)
```

### Séquencement & gating (validé côté backend, pas seulement UI)

```text
[Run Phase B done] → ① Adopter → ② Exécuter
                       ↩ Restaurer la config      (dispo tant que ② non exécuté)
                                  ↩ Annuler les déplacements (après ②)
```

- ② impossible si ① non adopté (HTTP 409) — les fichiers ne peuvent aller que
  dans des dossiers présents dans `tree.yaml`.
- « Restaurer la config » impossible après ② tant que les moves ne sont pas
  annulés (409) — cohérence config ↔ disque.
- Un seul run « adopté » à la fois par profil : adopter un run alors qu'un
  autre run a `adopted=true` non restauré → 409 (message explicite).

### Modèle d'exécution

- **① Adopter** : synchrone (< 1 s), sous `_locks[profile]` + `_check_lock_free`.
- **② Exécuter** et **Annuler les déplacements** : **thread daemon** (patron
  `dashboard/agent_refonte.py`), progression écrite dans `apply/status.json`
  toutes les ~50 ops, **poll** frontend 1-2 s (patron existant). Pendant toute
  la durée : le **lock `.taxonomy.lock` est posé** (bloque mappings, autres
  writes) et retiré en `finally`.
- Préconditions communes (vérifiées au backend) : SSD monté (target existe),
  lock libre, run Phase B `status=done`, artefacts présents et parsables.

### Nouveau module dashboard : `dashboard/agent_refonte_apply.py`

Pont FastAPI → logique d'application, pour garder `app.py` mince (même
découpage que `agent_refonte.py` / `agent_refonte_phase_c.py`).

## Couche A — « Adopter la structure »

La Phase B persiste déjà les YAML cibles **finalisés** dans `proposed/` :
`tree-proposed.yaml`, `theme_mapping-proposed.yaml` (fusion renames/fusions/
deletions/mappings_added déjà calculée par `_apply_changes_to_mapping`) et
`categories-proposed.yaml` (cascade, optionnel). **Adopter = promouvoir ces
fichiers vers la prod**, sans refaire la logique de fusion.

Étapes (sous lock) :

1. **Snapshot config** : `agents/refonte/agent_backup.create_backup(profile,
   batch_id)` (rotation 50). **Prérequis** : étendre `PROD_FILES` de
   `("tree.yaml", "theme_mapping.yaml")` à `(..., "categories.yaml")` —
   rétro-compatible, `create_backup`/`restore_backup` skippent déjà les
   fichiers absents. Le nom retourné est stocké dans
   `state.json.config_backup`.
2. **Promotion** : copie `tree-proposed.yaml → tree.yaml`,
   `theme_mapping-proposed.yaml → theme_mapping.yaml`,
   `categories-proposed.yaml → categories.yaml` (si présent).
3. **Création physique des nouveaux dossiers** sur le SSD : `mkdir -p` pour
   chaque entrée `creations[].path` de `changes.json` (nécessaire pour ②).
4. `taxonomy.reset_cache(profile)` + écriture `state.json` (`adopted: true`).

**Rollback A — « Restaurer la config »** : `restore_backup(profile,
config_backup)` + suppression des dossiers créés en (3) **uniquement s'ils
sont vides** (jamais de suppression de contenu ; non-vides → rapportés) +
`reset_cache` + `state.json` (`adopted: false`, `rolled_back_config: true`).

**Confirmation A (modal)** : récapitulatif depuis `changes.json` — N créations,
N renames, N fusions, N suppressions, N mappings ajoutés + mention du backup.
Pattern `showConfirm` existant.

## Couche B — « Exécuter les déplacements »

### Sélection des moves

Lecture de `simulation/reclassify-projection.csv` via
`refonte_results.read_projection_rows` + `enrich_row` :

- inclus : `changed` vrai **et non-doute** (`source_class ∈ {p1_theme,
  p1_refined}` et `confidence ≥ 0.7` — réutilise `_is_doubt`) ;
- exclus : fichiers en doute (restent en place, comptés et affichés) ;
- destination de chaque move : `proposed_folder` (chemins relatifs au target
  du profil).

### Nouveau module : `lib/move_journal.py` (miroir de `rename_journal.py`)

JSONL append-only `profiles/<p>/.cache/move-journal.jsonl`. Records
`{ts, old, new, batch}`. Helpers : `append_move`, `read_journal`,
`list_batches`, `undo_record` (reverse 1 move, garde collisions/missing),
`undo_batch` (reverse du plus récent au plus ancien, undos journalisés
`undo-<batch_id>`, échecs individuels skippés + rapportés).

### Boucle d'exécution (thread, lock posé, `move_batch_id` = UUID4)

Pour chaque move `old = target/current_folder/rel_path`,
`new = target/proposed_folder/rel_path` :

1. **Fraîcheur** : `old` n'existe plus → skip `stale` (fichier bougé depuis la
   simulation).
2. **Collision** : `new` existe déjà → skip `collision`, jamais d'écrasement.
3. `os.makedirs(dirname(new), exist_ok=True)` puis `os.rename(old, new)`
   (même volume → atomique) puis `append_move` (ordre : move puis journal,
   comme `commit_rename`).
4. Exception I/O → skip `error` + continue.
5. Progression dans `status.json` toutes les ~50 ops.

**Fin de batch** : rapport CSV horodaté `logs/rapport_apply_<ts>.csv` (une
ligne par move : rel_path, old, new, statut moved/stale/collision/error,
détail) ; dossiers sources devenus vides supprimés (remontée récursive dans la
limite du target) ; `state.json` (`executed: true`, compteurs) ; lock retiré.

**Rollback B — « Annuler les déplacements »** : `undo_batch(move_batch_id)` en
thread (long), même polling. Ensuite « Restaurer la config » redevient
disponible. État : `rolled_back_moves: true`, `executed: false`.

**Confirmation B (modal)** : N à déplacer, N exclus « à vérifier », N stables,
top 10 destinations, rappel « annulable via le journal tant que les fichiers
ne sont pas re-déplacés à la main ».

## Routes API (dans `app.py`, logique dans `agent_refonte_apply.py`)

| Route | Effet | Erreurs |
| --- | --- | --- |
| `GET /api/agent/refonte/apply/{run_id}/preview?profile=` | Compteurs pour les modals : état `state.json` + N moves / N exclus / N stables / top destinations (lecture seule) | 404 run inconnu |
| `POST /api/agent/refonte/apply/adopt` `{profile, run_id}` | Couche A (synchrone) | 409 (pas done / déjà adopté / autre run adopté), 423 lock, 500 SSD absent ou artefact manquant |
| `POST /api/agent/refonte/apply/restore-config` `{profile, run_id}` | Rollback A | 409 (non adopté / exécuté non annulé), 423 |
| `POST /api/agent/refonte/apply/execute` `{profile, run_id}` | Lance le thread moves, retourne immédiatement | 409 (non adopté / déjà exécuté / op en cours), 423 |
| `POST /api/agent/refonte/apply/undo-moves` `{profile, run_id}` | Lance le thread d'annulation | 409 (non exécuté / op en cours) |
| `GET /api/agent/refonte/apply/{run_id}/status?profile=` | `state.json` + `status.json` fusionnés (poll) | 404 |

## UI — bloc « Application » (panneau Refonte)

Affiché dans la colonne rapport quand le run sélectionné est Phase B `done`.
Stepper 2 étapes, états dérivés du endpoint status/preview :

```text
┌─ Application de la refonte ────────────────────────────────┐
│ ① Adopter la structure          [Adopter…] / ✓ Adoptée le… │
│    12 créations · 8 renames · 3 fusions · 31 mappings      │
│    [↩ Restaurer la config]   (si adoptée, moves non faits) │
│ ② Exécuter les déplacements     [▶ Exécuter…] (si ① ok)    │
│    2 341 à déplacer · 187 exclus (à vérifier) · 15 902 stables │
│    [████████░░░] 1 420/2 341 · 3 échecs   (poll pendant run) │
│    [↩ Annuler les déplacements]  (si exécuté)              │
│    Rapport : logs/rapport_apply_<ts>.csv                   │
└────────────────────────────────────────────────────────────┘
```

- Boutons grisés + tooltip explicite quand la précondition manque.
- Pendant une op : barre de progression (poll), boutons désactivés.
- Échecs > 0 : lien visible vers le rapport CSV.

## Tests (aucun test ne touche le vrai SSD)

- **`tests/auto/test_move_journal.py`** : append/read, `undo_record`
  (missing/collision), `undo_batch` (ordre inverse, undos journalisés,
  échecs skippés), batchs.
- **`tests/auto/test_refonte_apply.py`** (profil fixture + target tmp) :
  - Couche A : promotion des 3 YAML (+ absence tolérée de
    categories-proposed), backup créé, mkdir des créations, gating
    (pas done → 409, déjà adopté → 409, lock → 423), restore (dossiers vides
    supprimés, non-vides préservés + rapportés).
  - Couche B : sélection (doute exclu via `_is_doubt`), moves OK, skip
    stale/collision/error sans abort, journal complet, rapport CSV écrit,
    dossiers sources vides supprimés, undo_batch, gating (non adopté → 409),
    lock posé pendant le run et retiré en cas d'exception.
  - Endpoints HTTP : preview, statuts, codes d'erreur.
- Le thread est testé en mode synchrone (fonction interne appelée directement)
  comme pour le diagnostic.

## Hors périmètre (YAGNI)

- Sélection fine des moves (filtres/coches par fichier) — la zone de doute
  reste traitée manuellement via l'UI existante.
- Staging transactionnel, reprise de batch interrompu (le re-lancement
  re-skippe naturellement les moves déjà faits via la garde de fraîcheur).
- Application partielle de la config (tout ou rien par design).
- SSE temps réel (le polling existant suffit).
