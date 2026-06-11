# Restitution Phase B — refonte UI "outil de décision" (spec)

> **Goal :** Transformer la restitution des résultats de l'agent Refonte Phase B (aujourd'hui : cards + tables brutes + markdown) en un **outil de décision** qui aide à juger si la refonte proposée est bonne AVANT de l'appliquer.

**Statut :** Design validé (combo direction 1 "Triage Confiance" + direction 2 "L'Arbre Vivant"). Implémentation à planifier via `superpowers:writing-plans`.

**Date :** 2026-06-09 · **Branche :** `feature/refonte-phaseb-results-ui`

---

## Contexte

Le rapport Phase B a aujourd'hui 3 onglets internes (`Rationale` / `Diff tree` / `Impact reclassify`) rendus à plat : un markdown brut + 4 cards + 2 tables. Le user veut une restitution beaucoup plus visuelle, **orientée décision** (validé en cadrage) : clarté, drill-down, signaux de confiance, repérage des déplacements douteux.

Issue d'un panel de design (5 concepts notés par un jury), le user a retenu la **combinaison des 2 mieux notées** :
- **Direction 1 "Triage Confiance" (4.67)** — isoler la zone de doute (~1 600 fichiers à risque), trancher fichier par fichier.
- **Direction 2 "L'Arbre Vivant" (4.67)** — voir la nouvelle arborescence, dossiers créés en surbrillance, clic → provenance.

Elles s'emboîtent comme **2 onglets** : le risque (« y a-t-il des fichiers mal rangés ? ») et la structure (« la nouvelle arbo est-elle bonne ? »).

## Architecture cible — restructuration des 3 onglets

| Onglet | Avant | Après |
|---|---|---|
| ① | `Rationale` (markdown) | **`① Verdict & Risque`** (NOUVEAU, ouvert par défaut) — direction 1 |
| ② | `Diff tree` | **`② Arbre proposé`** — direction 2 (icicle de tree-proposed) |
| ③ | `Impact reclassify` | **`③ Plan complet`** — l'ancien Rationale markdown + les 200 mappings, relégué (preuve brute consultée rarement) |

L'ancien contenu "Impact reclassify" (cards + tables) est **absorbé et enrichi** dans l'onglet ①. L'ancien "Diff tree" devient l'onglet ② "Arbre proposé". Le markdown rationale glisse en ③.

## Onglet ① — Verdict & Risque (direction 1)

Au-dessus de la ligne de flottaison, 3 blocs :

### 1.1 — Ligne de verdict (4 KPI)
`18 425 fichiers · 12 105 déplacés · N à revoir · 623 sans prédiction`. Le KPI "à revoir" (jaune) et "sans prédiction" (rouge) sont les signaux d'alerte. "N à revoir" = nombre de fichiers dans la zone de doute (cf. 1.3).

### 1.2 — Budget de risque (barre empilée par source)
Une barre horizontale segmentée par **classe de source de classement** : P1 mapping (vert) · P1 affiné (vert clair) · keyword P2 (jaune) · fallback P4 (orange) · FAILED (rouge). Donne le go/no-go d'un coup d'œil : si 91 % en P1, sain ; si beaucoup de fallback/FAILED, fragile. Chaque segment est cliquable → filtre la table (1.4) sur cette source. Implémentation : Chart.js bar horizontal empilé OU flex CSS (les deux acceptables).

### 1.3 — Matrice de risque (source × confiance)
Grille HTML/CSS (pas de lib) : lignes = classe de source (P1 theme / P1 refined / P2 keyword / P4 fallback / FAILED), colonnes = bandes de confiance vision (`0–.5`, `.5–.7`, `.7–.9`, `.9–1`). Chaque cellule affiche le **compte réel** et une couleur de risque (vert→rouge). **Raison d'être** : le champ `confidence` est quasi dégénéré (≈98 % des fichiers à 0.9–1.0), donc un histogramme de confiance seul est inutile ; la vraie variance de risque est dans `source`. La matrice croise les deux pour ne jamais cacher un fallback derrière une fausse haute confiance. Cellules colorées non-vertes cliquables → filtre la table (1.4).

La "zone de doute" (= N à revoir du KPI 1.2) = toutes les cellules non-`P1` + les cellules `P1` à confiance < 0.7.

### 1.4 — Table de drill-down (sous la ligne de flottaison)
Apparaît au clic d'un segment de budget ou d'une cellule de matrice. Colonnes : `Fichier · Source (badge coloré) · Déplacement (current → proposed) · Confiance (mini-jauge) · Alerte (🆕 dossier créé / ⇄ saut inter-discipline) · Triage (✓/✗)`. **Triée par risk-score décroissant** (les pires en tête). **Paginée et filtrée côté serveur** (cf. backend) — on ne charge jamais les 18 425 lignes côté client.

Les 16 793 fichiers "P1 sûr" restent **pliés par défaut** : on ne montre que la zone de doute tant qu'aucun filtre "sûr" n'est explicitement demandé.

### 1.5 — Risk-score (heuristique transparente)
Score composite par fichier, sert le tri par défaut de la table :
```
risk = w_src·src_weight(source) + w_conf·(1 − confidence) + w_jump·is_jump + w_new·is_new_dest
```
avec `src_weight` : FAILED=1.0, fallback=0.7, keyword=0.6, p1_refined=0.1, p1_theme=0.0 ; poids `w_src=0.5, w_conf=0.2, w_jump=0.2, w_new=0.1` (constantes nommées, ajustables). Un tooltip décompose le score sur chaque ligne (transparence). `is_jump` et `is_new_dest` : cf. backend.

## Onglet ② — Arbre proposé (direction 2)

### 2.1 — Icicle de la taxonomie proposée
Vue hiérarchique horizontale (D3 `d3.partition` + `d3.hierarchy`, **même idiome que le treemap existant de `taxonomy.js`** — D3 v7 est déjà chargé dans le contexte Taxonomie). Construite depuis `tree-proposed.yaml`. Chaque rectangle = un dossier ; sa taille (hauteur) = nombre de fichiers **entrants** (`n_incoming`, agrégé bottom-up). Les **39 dossiers créés** par la refonte sont mis en évidence (bordure + glow orange). Drill : clic sur un dossier descend dans ses enfants. Fallback acceptable : icicle en HTML/CSS flex (rectangles dimensionnés par `flex`).

### 2.2 — Panneau de provenance
Au clic d'un dossier : un panneau affiche **d'où viennent ses fichiers entrants** (top origines : `current_folder` → ce dossier, avec compte et barre). Permet de vérifier que la refonte range les fichiers depuis des sources cohérentes (ex. un nouveau dossier "Pattern-Recognition" qui se remplit depuis `_INBOX` = sain ; depuis un dossier sans rapport = suspect).

## Onglet ③ — Plan complet

L'onglet `Rationale` actuel, **inchangé** : le markdown rendu par marked.js (résumé des changements + créations + 200 mappings, repliés par groupe). Juste déplacé en 3ᵉ position. Zéro dev sauf le réordonnancement des onglets.

## Backend — agrégations et endpoints

Pattern projet : **DuckDB `read_csv_auto()`** sur les CSV (déjà le pattern des métriques, cf. `dashboard/CLAUDE.md`). Les agrégations lourdes se font en SQL sur `reclassify-projection.csv`, pas en Python ligne à ligne.

### Données existantes (réutilisées)
- `simulation-summary.json` : `n_files, n_moving, n_stable, n_no_prediction, n_by_source, top_destinations[], top_origins[]`.
- `reclassify-projection.csv` : `rel_path, current_folder, proposed_folder, changed, source, top_theme, confidence, score`.
- `changes.json` : `creations[].path` (pour `is_new_dest` + glow arbre), `mappings_added[]`.
- `tree-proposed.yaml` : `{folders: [...]}` (pour l'icicle).
- Endpoint existant : `GET /api/agent/refonte/proposition/{run_id}/simulation?profile=X` → `{summary, sample_moves, n_moves_total}`.

### Nouvelles agrégations (à ajouter)
1. **Matrice risque** : compter les fichiers par `(source_class, confidence_band)`. `source_class` = bucketisation du label `source` brut :
   - commence par `"LLM (theme→"` → `p1_refined`
   - commence par `"LLM (theme)"` → `p1_theme`
   - contient `"fallback"` → `fallback`
   - commence par `"Keyword"` → `keyword`
   - `"FAILED"` ou vide → `failed`
   (Note : le simulateur passe `llm_mapper=None`, donc P3 ne fire jamais — pas de bucket P3.)
   `confidence_band` ∈ {`[0,0.5)`, `[0.5,0.7)`, `[0.7,0.9)`, `[0.9,1.0]`}.
2. **Risk-score + flags par ligne** : pour chaque ligne, calculer `risk` (cf. 1.5), `is_jump` (1er segment de `current_folder` ≠ 1er segment de `proposed_folder`, en ignorant `_INBOX`/racine qui ne sont pas une discipline → pas un saut), `is_new_dest` (`proposed_folder ∈ creations`).
3. **Arbre + entrants par dossier** : pour chaque dossier de `tree-proposed.yaml`, `n_incoming` = nombre de lignes du CSV dont `proposed_folder` == ce dossier (ou un descendant, pour l'agrégation bottom-up). Marquer `is_creation` (∈ `changes.json:creations`).
4. **Provenance par dossier** : pour un `proposed_folder` donné, top `current_folder` d'origine (group by + count + tri desc, limite ~8).

### Endpoints (à ajouter ou étendre)
- `GET …/{run_id}/risk-matrix?profile=X` → `{matrix: [{source_class, band, count}], budget: [{source_class, count}], n_doubt: int}`.
- `GET …/{run_id}/doubt-files?profile=X&source_class=&band=&page=&page_size=50` → `{rows: [{rel_path, source, source_class, current_folder, proposed_folder, confidence, top_theme, risk, is_jump, is_new_dest}], total, page}`. Filtrage + tri risk desc + pagination **côté serveur**.
- `GET …/{run_id}/proposed-tree?profile=X` → `{tree: <hierarchie {name, children[], n_incoming, is_creation}>}`.
- `GET …/{run_id}/folder-provenance?profile=X&folder=…` → `{origins: [{folder, count}]}`.

## Décision de scope ouverte (à trancher en revue de spec)

**Triage actif ✓/✗ — persisté ou éphémère ?**
- **V1 recommandé : éphémère (client-side seul).** Les boutons ✓/✗ marquent visuellement les lignes à l'écran (aide à éplucher la zone de doute) mais ne persistent rien. Livrable plus vite, zéro store backend.
- **V2 (optionnel plus tard) : persistant.** Les verdicts sont sauvés dans `profiles/<p>/.cache/refonte/<run>/triage.json` via un endpoint `POST …/{run_id}/triage`, et rechargés à la réouverture. Permet d'arbitrer en plusieurs sessions, et de servir de base à une future application sélective.

La spec part sur **V1 éphémère** sauf indication contraire en revue.

## Hors-scope explicite

- **Application de la refonte en prod** : pas de bouton "Appliquer" ni d'écriture sur les YAML de prod. Cette UI sert à JUGER, pas à exécuter (cohérent avec le badge "YAML prod intacts").
- **Persistance des verdicts ✓/✗** (sauf si tranché en V1 lors de la revue).
- **Heatmap des flux inter-disciplines** (sous la ligne de flottaison de la direction 1, et cœur des directions 4/5 non retenues) : hors V1, backlog possible.
- **Modification du pipeline Phase A / B lui-même** : on ne touche qu'à la restitution (lecture des artefacts existants + nouvelles agrégations en lecture seule).
- **Onglet ③** : juste réordonné, pas de refonte de son contenu markdown.

## Critères de succès

1. Le rapport Phase B affiche 3 onglets `① Verdict & Risque` (défaut) · `② Arbre proposé` · `③ Plan complet`.
2. Onglet ① : KPI + budget de risque + matrice source×confiance cliquables ; le clic filtre une table de drill-down paginée côté serveur, triée par risk-score, avec badges source / flèche déplacement / jauge confiance / alertes 🆕⇄.
3. La zone de doute (non-P1 + P1 conf<0.7) est isolée ; les fichiers P1 sûrs restent pliés par défaut.
4. Onglet ② : icicle de la taxo proposée dimensionné par `n_incoming`, créations en surbrillance, clic dossier → panneau de provenance.
5. Aucune des agrégations ne charge >2 000 lignes côté client (pagination/filtrage serveur via DuckDB).
6. Tests : agrégations backend (bucketisation source, bands confiance, risk-score, flags jump/new-dest, provenance) couvertes en unitaire ; endpoints couverts en intégration HTTP ; rendu front non-régressif (les 3 onglets se montent, le lazy-load fonctionne).

## Notes de faisabilité

- D3 v7 est chargé dans le contexte Taxonomie (`taxonomy.html`) où `agent_refonte_panel.html` est inclus → l'icicle D3 est dispo sans dépendance. Chart.js v4 idem.
- Le pattern DuckDB `read_csv_auto` est déjà en place pour les métriques → réutilisable pour les agrégations sur `reclassify-projection.csv`.
- Le champ `confidence` étant quasi constant à 0.9–1.0, l'axe primaire de risque est `source`, pas la confiance (d'où la matrice plutôt qu'un histogramme).
