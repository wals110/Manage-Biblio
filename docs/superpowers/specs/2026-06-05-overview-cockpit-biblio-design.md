# Overview — Cockpit Bibliothèque

> Brainstormé le 2026-06-05. Cible : refondre l'onglet Overview du dashboard
> Klodo pour qu'il devienne un cockpit Biblio profile-aware, après le refactor
> UX qui a déjà déplacé les KPIs tests vers le hub `/tests`.

## Context

L'onglet **Overview** actuel ([dashboard/templates/overview.html](../../../dashboard/templates/overview.html))
était dimensionné autour des tests fonctionnels : KPIs Pass/Fail/Skip,
heatmap par phase, release gate, bandeau LLM config statique. Avec le
refactor `feature/dashboard-ux-refactor` (PR #166), les tests sont
maintenant centralisés dans un hub `/tests` dédié — l'Overview se retrouve
redondant et ne parle plus de la bibliothèque elle-même.

**Objectif** : transformer Overview en cockpit Biblio avec :

1. **Moitié haute** profile-aware (sélecteur dropdown) — surface ce qui se
   passe sur le profil sélectionné : volume, classification, coût LLM, état
   de santé, charts, activité récente.
2. **Moitié basse** info générales (statique tous-profils) — modèles LLM
   par profil, statut API keys, liste des profils, répartition des coûts.

Pas de tests fonctionnels sur Overview — Tests reste sur `/tests`. Pas de
JS fetch ni HTMX swap : 100% server-render cohérent avec le refactor récent.

## Décisions actées (réponses utilisateur)

| Question | Choix |
|---|---|
| Scope global | **Cockpit Biblio pur** — tests sortent complètement |
| Layout moitié profil | **B — Hiérarchique** (4 KPI vedettes + bloc détails mixé aux charts) |
| Cartes profil (12) | Fichiers · % classifiés · Folders · Coût LLM · Vision cache · Inbox · Health (orphans+locks) · Top 10 thèmes · Top 10 folders · Activité récente · Runs baseline · Sessions agents |
| Cartes générales (4) | Modèles LLM · API keys · Profils existants · Coût cumulé tous profils |
| Dropdown profil | **Reload page avec `?profile=X`** (cohérent avec `?view=X`) |
| Refresh | **Snapshot statique au load + bouton 🔄** (`?refresh=1` force cache miss) |
| Empty cards | **Afficher `—` + tooltip** explicatif (layout stable entre profils) |

## Architecture

**Route unique** : `GET /?profile=<name>&refresh=<bool>`

- Default : `?profile=default`, ou 1er profil disponible si `default` absent
- Profile inconnu (`?profile=foo`) → fallback transparent (pas de 404)
- `?refresh=1` force le recalcul, sinon hit cache mémoire 30s

**Cache mémoire serveur** :

```python
# dashboard/overview.py
_CACHE_TTL_SECONDS = 30
_overview_cache: dict[str, tuple[float, dict]] = {}  # profile → (ts, snapshot)
```

Les opérations coûteuses (scan FS `os.walk(target)` sur 19k fichiers ~1-3s ;
agrégation `top_themes` sur `vision_cache.json` ~0.5s ; somme `global_cost`
sur tous les profils ~1s) hit le cache. Les opérations gratuites
(lecture `tree.yaml`, glob de petits dossiers) restent recalculées à chaque
appel pour rester à jour — c'est l'agrégation totale par profil qui est
cachée 30s.

**Module dédié** : nouveau `dashboard/overview.py` qui centralise les
helpers cockpit. Décharge `data.py` (déjà 1500+ LOC), cohérent avec le
pattern `taxonomy.py` / `categories.py` / `baseline.py`.

**Header (toujours visible)** :

```text
[📚 Overview]   Profil : [▾ default]   [🔄]   Coût total tous profils : $8.42
```

Le bandeau `Coût total` est promu en header parce qu'il agrège tous les
profils — indépendant du dropdown. C'est aussi la métrique qui justifie
le plus rapidement le projet Klodo (visibilité immédiate).

## Layout cible (Layout B — Hiérarchique)

```text
┌─ Header ─────────────────────────────────────────────────────────┐
│  📚 Overview     Profil : [▾ default]  🔄    Coût total : $8.42 │
└──────────────────────────────────────────────────────────────────┘

┌─ MOITIÉ HAUTE : Profil "default" ────────────────────────────────┐
│                                                                  │
│  ┌──────┬──────┬──────┬──────┐                                   │
│  │ 📁   │ 📊   │ 💰   │ ⚠    │  ← Rangée 1 : 4 KPI vedettes      │
│  │19250 │ 96 % │ $6.42│  32  │                                   │
│  │fich. │class.│ LLM  │orph. │                                   │
│  └──────┴──────┴──────┴──────┘                                   │
│                                                                  │
│  ┌──────────────────┬──────────────────────────────────────┐     │
│  │ Détails  (1fr)   │ 🔥 Top 10 thèmes LLM  (1.3fr)        │     │
│  │ 🗂  178 folders  │ ▰▰▰▰▰▰▰▰▰▰  IA / ML        450      │     │
│  │ 👁  14 580 vis.  │ ▰▰▰▰▰▰▰▰    Python         320      │     │
│  │ 📥  42 inbox     │ ▰▰▰▰▰▰      Linux          240      │     │
│  │ 🎯  3 baseline   │ ▰▰▰▰▰       Math           185      │     │
│  │ 🤖  5 sessions   │ ...                                  │     │
│  └──────────────────┴──────────────────────────────────────┘     │
│                                                                  │
│  ┌──────────────────────────────────────┬──────────────────┐     │
│  │ 📈 Top 10 folders  (1.3fr)           │ 🕒 Activité (1fr)│     │
│  │ ▰▰▰▰▰▰▰▰▰▰  02-INFO/AI       1240   │  il y a 2h       │     │
│  │ ▰▰▰▰▰▰▰▰    01-SCIENCES       980   │  rename folder   │     │
│  │ ▰▰▰▰▰▰      04-LITT          720   │  il y a 5h       │     │
│  │ ▰▰▰▰        09-OS            580   │  + mapping       │     │
│  │ ...                                  │  hier · backup   │     │
│  └──────────────────────────────────────┴──────────────────┘     │
└──────────────────────────────────────────────────────────────────┘

┌─ MOITIÉ BASSE : Infos générales ─────────────────────────────────┐
│  ┌───────────────┬───────────────┬───────────────┬─────────────┐ │
│  │ 🧠 LLM models │ 🔑 API keys   │ 👥 Profils    │ 💵 Coûts    │ │
│  │ default:      │ ● SiliconFlow │ ● default     │ pie chart   │ │
│  │   Qwen3-VL-32 │   configurée  │ ● test        │ default 76% │ │
│  │ test:         │ ○ Anthropic   │ ● test-local  │ test    18% │ │
│  │   Qwen3-VL-32 │   non config. │ ● template    │ local    6% │ │
│  │ test-local:   │               │               │             │ │
│  │   Ollama      │               │               │             │ │
│  └───────────────┴───────────────┴───────────────┴─────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

**Décisions de groupement** :

- Le bloc **Détails** absorbe 5 KPIs secondaires (folders, vision, inbox,
  baseline, agents) → économise 5 rangées, reste lisible
- **Health** (orphans+locks) reste KPI vedette parce qu'il signale un
  état actionnable (rouge si > 0)
- **Coût LLM** reste KPI vedette (info business clé)

**Responsive** :

- ≥ 1280px : 4 colonnes KPI vedettes, grilles 1fr/1.3fr et 1.3fr/1fr
- 900–1280px : KPI restent en 4 colonnes, charts/détails empilent en colonne unique
- < 900px : tout en colonne unique

## Backend — `dashboard/overview.py`

**Signature publique** :

```python
# Cache
_CACHE_TTL_SECONDS = 30
_overview_cache: dict[str, tuple[float, dict]] = {}

# Agrégateurs principaux
def build_profile_snapshot(profile: str, force: bool = False) -> dict
def build_general_snapshot() -> dict

# Cartes profil (1 fonction par carte)
def card_files_count(profile: str)      -> dict   # {total, size_gb, by_ext: {pdf, epub}, error?: str}
def card_classified_rate(profile: str)  -> dict   # {classified, unclassified, rate, fallback_count}
def card_folders_count(profile: str)    -> dict   # {total, max_depth, recently_modified: list}
def card_llm_cost(profile: str)         -> dict   # {cost_usd, n_calls, cost_per_call}
def card_health(profile: str)           -> dict   # {n_orphans_mappings, n_orphans_categories, n_orphans_total, locks_active: list}
def card_vision_cache(profile: str)     -> dict   # {n_entries, n_successful, size_kb, last_modified}
def card_inbox(profile: str)            -> dict   # {n_files, size_mb, oldest_iso, exists}
def card_baseline_runs(profile: str)    -> dict   # {n_runs, last_run_iso, last_validated_pct}
def card_agent_sessions(profile: str)   -> dict   # {refonte: {n_batches, status}, dedupli: {n_batches, status}}
                                                   # status ∈ {"idle", "running", "error", "missing"}
                                                   # lu depuis .cache/<agent>/status.json (clé "status").
                                                   # "missing" si status.json absent (= jamais lancé).
def card_top_themes(profile: str, limit: int = 10) -> list[dict]   # [{theme, count, pct}]
def card_top_folders(profile: str, limit: int = 10) -> list[dict]  # [{path, n_files, pct}]
def card_recent_activity(profile: str, limit: int = 5) -> list[dict]  # [{ts, relative_time, action, target}]

# Cartes générales (statiques tous-profils)
def card_llm_models()    -> list[dict]   # [{profile, provider, model, endpoint}]
def card_api_keys()      -> list[dict]   # [{name, configured}]
def card_profiles_list() -> list[dict]   # [{name, target_exists, target_path}]
def card_global_cost()   -> dict         # {total_usd, by_profile: [{profile, cost_usd, pct}]}
```

**Sources par carte** :

| Carte | Source | Coût |
|---|---|---|
| `files_count`, `top_folders` | `os.walk(target)` filtré `.pdf`/`.epub` | Moyen (1–3 s) |
| `classified_rate` | dérivé de `files_count` (fichiers en racine/`_A-TRIER`) | Gratuit |
| `folders_count` | `taxonomy._load_tree(profile)` | Cheap |
| `llm_cost`, `vision_cache` | `vision_cache.json` + `profile.yaml:cost_per_call` | Cheap |
| `health` | `theme_mapping` vs `tree`, `categories` orphans, glob `.lock` | Cheap |
| `inbox` | glob du dossier `inbox:` de `profile.yaml` | Cheap |
| `baseline_runs` | `baseline.list_runs(profile)` (existe déjà) | Cheap |
| `agent_sessions` | glob `.cache/refonte/batches/`, `.cache/dedupli/batches/` | Cheap |
| `top_themes` | `taxonomy._iter_themes` sur `vision_cache.json` + canonisation | Moyen |
| `recent_activity` | mtime des 3 derniers `taxonomy-backups`, `categories-backups`, `rename-journal` | Cheap |
| `llm_models`, `api_keys`, `profiles_list` | réutilise `data.get_llm_config`, `data.get_available_profiles`, `_get_api_keys_status` | Cheap |
| `global_cost` | somme des `card_llm_cost` par profil | Moyen (boucle) |

Helpers existants réutilisés : [dashboard/data.py](../../../dashboard/data.py)
(`get_available_profiles`, `get_llm_config`, `get_project_root`),
[dashboard/taxonomy.py](../../../dashboard/taxonomy.py)
(`_load_tree`, `_iter_themes`, `_load_mapping`),
[dashboard/categories.py](../../../dashboard/categories.py)
(`build_snapshot` pour `n_orphans`),
[dashboard/baseline.py](../../../dashboard/baseline.py) (`list_runs`).

**Endpoint refactor** dans [dashboard/app.py](../../../dashboard/app.py:57) :

```python
@app.get("/")
async def overview(
    request: Request,
    profile: str = "default",
    refresh: bool = False,
):
    from dashboard import overview as ov
    available = data.get_available_profiles()
    names = {p["name"] for p in available}
    if profile not in names:
        profile = available[0]["name"] if available else "default"
    return templates.TemplateResponse(request, "overview.html", {
        "active": "overview",
        "current_profile": profile,
        "available_profiles": available,
        "profile_snapshot": ov.build_profile_snapshot(profile, force=refresh),
        "general_snapshot": ov.build_general_snapshot(),
    })
```

L'ancien contexte de `overview()` (`report`, `tests`, `issues`, `api_key_set`,
`llm_config`) disparaît — l'Overview ne consomme plus les helpers tests.

## Frontend — templates + macros

**`dashboard/templates/overview.html`** (réécriture complète) :

```jinja
{% extends "base.html" %}
{% from "macros/widgets.html" import kpi_card_big, mini_bar, activity_timeline %}
{% set active = "overview" %}
{% block title %}Overview — {{ current_profile }}{% endblock %}
{% block content %}

<div class="dash-overview-header">
  <h1 class="page-title">📚 Overview</h1>
  <div class="dash-overview-toolbar">
    <label>Profil</label>
    <select class="filter-select"
            onchange="window.location.href='/?profile=' + encodeURIComponent(this.value)">
      {% for p in available_profiles %}
      <option value="{{ p.name }}" {% if p.name == current_profile %}selected{% endif %}>
        {{ p.name }}
      </option>
      {% endfor %}
    </select>
    <a href="/?profile={{ current_profile }}&refresh=1"
       class="dash-refresh-btn" title="Recalculer (force cache miss)">🔄</a>
    <div class="dash-global-cost">
      Coût total : <strong>${{ general_snapshot.global_cost.total_usd }}</strong>
    </div>
  </div>
</div>

{% set s = profile_snapshot %}
{% include "partials/overview_profile.html" %}
{% include "partials/overview_general.html" %}

{% endblock %}
```

**Partials** :

- `dashboard/templates/partials/overview_profile.html` — implémente le Layout B
  (rangée 1 = 4 KPI vedettes ; rangée 2 = Détails 1fr + Top thèmes 1.3fr ;
  rangée 3 = Top folders 1.3fr + Activité 1fr)
- `dashboard/templates/partials/overview_general.html` — grille 4×1
  (LLM models / API keys / Profils / pie coûts)

**Macros à ajouter dans `dashboard/templates/macros/widgets.html`** :

```jinja
{% macro kpi_card_big(label, value, subtitle="", variant="default") %}
<div class="dash-kpi-big dash-kpi-big-{{ variant }}">
  <div class="dash-kpi-label">{{ label }}</div>
  <div class="dash-kpi-value">{{ value }}</div>
  {% if subtitle %}<div class="dash-kpi-sub">{{ subtitle }}</div>{% endif %}
</div>
{% endmacro %}

{% macro mini_bar(items, value_key='count', label_key='label', empty="") %}
{% if items %}
  {% set vmax = (items | map(attribute=value_key) | max) or 1 %}
  <ul class="dash-mini-bars">
    {% for it in items %}
    <li class="dash-mini-bar-row" title="{{ it[label_key] }} · {{ it[value_key] }}">
      <span class="dash-mini-bar-label">{{ it[label_key] }}</span>
      <span class="dash-mini-bar-track">
        <span class="dash-mini-bar-fill" style="width: {{ (it[value_key] / vmax * 100) | round(1) }}%"></span>
      </span>
      <span class="dash-mini-bar-value">{{ it[value_key] }}</span>
    </li>
    {% endfor %}
  </ul>
{% else %}<p class="muted small">{{ empty }}</p>{% endif %}
{% endmacro %}

{% macro activity_timeline(events, empty="") %}
{% if events %}
  <ul class="dash-activity">
    {% for e in events %}
    <li>
      <span class="dash-activity-time">{{ e.relative_time }}</span>
      <span class="dash-activity-action">{{ e.action }}</span>
      <span class="dash-activity-target">{{ e.target }}</span>
    </li>
    {% endfor %}
  </ul>
{% else %}<p class="muted small">{{ empty }}</p>{% endif %}
{% endmacro %}
```

**CSS** : ajouts dans [dashboard/static/style.css](../../../dashboard/static/style.css) — classes
`.dash-overview-header`, `.dash-overview-toolbar`, `.dash-refresh-btn`,
`.dash-global-cost`, `.dash-kpi-big`, `.dash-kpi-big-warn`, `.dash-kpi-big-ok`,
`.dash-grid-2-bias-right`, `.dash-grid-2-bias-left`, `.dash-card`,
`.dash-details-list`, `.dash-mini-bars`, `.dash-mini-bar-row`,
`.dash-mini-bar-label`, `.dash-mini-bar-track`, `.dash-mini-bar-fill`,
`.dash-mini-bar-value`, `.dash-activity`. Tout en fin de fichier, pas
de modification de l'existant.

**JS** : zéro fichier dédié. Le `<select onchange>` inline et le `<a href>`
du refresh button suffisent.

## Empty states + edge cases

**Cartes vides** (rappel : `—` + tooltip, layout stable) :

| Carte | Condition vide | Affichage |
|---|---|---|
| `files_count` | `target` introuvable sur disque | `—` valeur, sub-titre `target manquant` rouge, tooltip cite le path |
| `classified_rate` | `files_count` vide | `—%`, sub-titre `pas de fichier à classer` |
| `llm_cost` | `vision_cache.json` absent ou vide | `$0.00`, sub-titre `0 appel` |
| `health` | tout propre | `0` en vert (`variant=ok`), tooltip `tout propre` |
| `folders_count` | `tree.yaml` absent | `—`, tooltip `tree.yaml introuvable — profil incomplet` |
| `vision_cache` | absent | `—`, tooltip cite le path attendu `.cache/vision_cache.json` |
| `inbox` | `inbox:` non défini OU dossier absent | `—`, tooltip distingue les deux cas |
| `baseline_runs` | aucun run | `0`, tooltip rappelle la commande CLI |
| `agent_sessions` | aucune session | `0 refonte · 0 dédupli` |
| `top_themes` / `top_folders` | liste vide | macro `mini_bar` affiche son `empty` |
| `recent_activity` | aucune mutation | macro `activity_timeline` affiche son `empty` |

**Cas pathologiques** :

1. **Profile inexistant** dans l'URL → fallback sur 1er disponible
2. **Aucun profil du tout** → `<select>` affiche `(aucun profil)`, toutes les
   cartes affichent leur empty state, `global_cost = $0.00`. Lien CLI vers
   `./klodo.sh init-profile` en sub-titre du sélecteur
3. **`target` SSD non monté** (cas concret : SSD externe débranché) →
   `card_files_count` vérifie `Path(target).exists()` AVANT `os.walk`,
   sinon timeout/erreur disque. Tooltip explicite `SSD non monté : <path>`
4. **`vision_cache.json` corrompu** → `try/except json.JSONDecodeError` →
   traité comme vide. Pas de 500
5. **Calcul `top_themes` lent** (>2 s) → couvert par le cache 30 s. Premier
   hit après `?refresh=1` peut être lent, c'est attendu
6. **Profile.yaml invalide** → le profil disparaît de `available_profiles`
   (déjà géré par `data.get_available_profiles`)
7. **Cumul `global_cost` profils hétérogènes** (Ollama gratuit vs SiliconFlow
   payant) → multiplie par `cost_per_call` du profil (0 pour Ollama bien
   configuré). La pie chart cache les segments < 1 %

**Locks actifs détectés** (`card_health`) :

- Glob `profiles/<name>/.cache/.*.lock` (taxonomy, dedupli, agent_refonte)
- Pour chaque lock : nom + âge (mtime). Si âge > 1 h → badge `⚠ stale ?`
  (pattern observé précédemment : zombie 442 s)

## Testing

**Fichier principal** : `tests/auto/test_overview.py` (nouveau)

**Niveau 1 — Unitaires par carte** (18 tests) :

`test_card_files_count_happy`, `test_card_files_count_target_missing`,
`test_card_classified_rate`, `test_card_folders_count_no_tree`,
`test_card_llm_cost_with_cache`, `test_card_llm_cost_corrupt_json`,
`test_card_health_clean`, `test_card_health_with_orphans`,
`test_card_health_stale_lock`, `test_card_vision_cache_missing`,
`test_card_inbox_unconfigured`, `test_card_top_themes_aggregation`,
`test_card_top_themes_empty_cache`, `test_card_top_folders`,
`test_card_recent_activity_merge_sources`, `test_card_baseline_runs`,
`test_card_agent_sessions_counts_batches`,
`test_card_global_cost_pie_segments`

**Niveau 2 — Snapshot builder + cache** (7 tests) :

`test_build_profile_snapshot_returns_all_keys`,
`test_cache_hit_within_30s`, `test_cache_miss_after_ttl`,
`test_force_invalidates_cache`, `test_cache_per_profile_isolation`,
`test_build_general_snapshot_no_profile_arg`,
`test_global_cost_sums_per_profile_llm_cost`

**Niveau 3 — Routing & rendering** (dans `tests/auto/test_dashboard.py`, 7 tests) :

`test_overview_default_profile`, `test_overview_explicit_profile`,
`test_overview_unknown_profile_falls_back`,
`test_overview_no_profiles_renders_safely`,
`test_overview_refresh_param_invalidates_cache`,
`test_overview_contains_global_cost_banner`,
`test_overview_contains_kpi_grid`

**Niveau 4 — Macros** (5 tests dans `test_overview.py`) :

`test_macro_kpi_card_big_renders_value`,
`test_macro_kpi_card_big_warn_variant`,
`test_macro_mini_bar_with_items`,
`test_macro_mini_bar_empty_uses_fallback`,
`test_macro_activity_timeline_empty`

**Fixtures** : nouvelle classe `OverviewTestBase` calquée sur
`TaxonomyTestBase` / `CategoriesTestBase` — pose un profil minimal cohérent
(`profile.yaml` + `tree.yaml` + `theme_mapping.yaml` + dossier `target/` +
quelques PDFs binaires factices).

**Couverture totale** : ~37 nouveaux tests. Smoke total post-implémentation
visé : ~1280 tests verts.

**Smoke test manuel** :

1. `uv run python -m dashboard 8080` — ouvrir `http://localhost:8080`
2. Overview = `default` par défaut, tous les chiffres remplis
3. Switch dropdown vers `test` → URL devient `?profile=test`, données
   différentes
4. Click 🔄 → URL gagne `&refresh=1`, log serveur montre le recompute
5. Test profile inexistant `?profile=foo` → rebascule sur `default`
6. Débrancher SSD externe (si possible) → `target_missing` affiché
   correctement

## Hors-scope explicite

- **Pas de retour des KPIs tests** sur Overview (Tests vit sur `/tests`)
- **Pas d'auto-refresh** — uniquement statique au load + bouton manuel
- **Pas de fetch JS ni HTMX swap** — 100% server-render avec reload
- **Pas de modification des routes ou endpoints existants** sauf l'endpoint
  `/` (route Overview). Les autres pages (`/tests`, `/baseline`, `/taxonomy`,
  `/logs`, `/viewer`, `/admin`) restent intactes
- **Pas de migration CSS existante** — uniquement ajouts de classes `.dash-*`
  nouvelles à la fin de `style.css`
- **Pas de re-styling de l'onglet Taxonomie** (qui a son propre header de
  sub-tabs `.tax-subtabs` — découpé de notre `.dash-subtabs`)
- **Pas de logique d'auto-discovery de nouveaux profils** — relit
  `profiles/*/` à chaque appel via `data.get_available_profiles`
- **Pas de feature flag** — déploiement direct

## Critical files

**Création** :

- `dashboard/overview.py` — module backend cockpit (~400 LOC : cache + 16
  fonctions de carte + 2 builders)
- `dashboard/templates/partials/overview_profile.html` — layout B moitié haute
- `dashboard/templates/partials/overview_general.html` — grille 4×1
- `tests/auto/test_overview.py` — 30+ tests unitaires + macros

**Modification** :

- [dashboard/app.py](../../../dashboard/app.py:57) — réécriture endpoint
  `overview()` (passe à `?profile=` + `?refresh=` + dispatch
  `ov.build_*_snapshot`)
- [dashboard/templates/overview.html](../../../dashboard/templates/overview.html)
  — réécriture complète en shell minimal qui inclut les 2 partials
- [dashboard/templates/macros/widgets.html](../../../dashboard/templates/macros/widgets.html)
  — ajout 3 macros : `kpi_card_big`, `mini_bar`, `activity_timeline`
- [dashboard/static/style.css](../../../dashboard/static/style.css) — ajout
  classes `.dash-*` (cockpit, KPI big, mini-bar, activity) à la fin
- [tests/auto/test_dashboard.py](../../../tests/auto/test_dashboard.py)
  — 7 nouveaux tests pour le routing Overview

## Verification

```bash
# Lint
uv run ruff check dashboard/overview.py dashboard/app.py tests/auto/test_overview.py

# Tests
uv run python -m unittest tests.auto.test_overview -v
uv run python -m unittest tests.auto.test_dashboard.TestDashboardRoutes -v
uv run python -m unittest discover -s tests/auto -t .   # full suite

# Smoke test manuel — cf. section Testing
uv run python -m dashboard 8080
```

## Branche & livraison

- **Branche** : `feature/overview-cockpit` depuis `develop` (Gitflow)
- **Taille** : medium. ~400 LOC backend nouveau, ~300 LOC template/partial,
  ~80 LOC macros + CSS, ~37 tests. Risque principal : la régression du
  layout existant (l'ancien `overview.html` partage des classes avec
  d'autres pages — mais on garde toutes les anciennes classes et on ne
  fait qu'AJOUTER des `.dash-*`)
- **PR** : 1 PR vers `develop`, label `ux` + `dashboard`
