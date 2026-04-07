# Dashboard Tests Fonctionnels — Design Spec

**Date :** 2026-04-07
**Scope :** App web locale pour visualiser, exécuter et monitorer les tests fonctionnels de Klodo
**Stack :** FastAPI + HTMX + Jinja2 + Chart.js

---

## Objectif

Un centre de contrôle qualité pour les développeurs de Klodo. Accessible via `./klodo.sh dashboard` → `http://localhost:8080`. Visualiser les résultats des tests, les rapports CSV, les métriques qualité, et exécuter des tests depuis l'interface.

## Architecture

```
dashboard/
├── app.py                 # FastAPI, routes, logique
├── templates/
│   ├── base.html          # Layout commun (sidebar, header)
│   ├── overview.html      # Page Overview
│   ├── tests.html         # Page Tests — exécution
│   ├── rapports.html      # Page Rapports — viewer CSV
│   ├── comparer.html      # Page Comparer — diff rapports
│   ├── metriques.html     # Page Métriques qualité
│   ├── historique.html    # Page Historique
│   └── suggestions.html   # Page Suggestions (lecture seule)
└── static/
    └── style.css          # Thème dark, CSS custom
```

Le serveur lit les données depuis :
- `tests/functional/reports/report_*.json` — rapports de runs
- `tests/functional/tests.yaml` — définition des tests
- `logs/*.csv` — rapports classify/rename/refine
- `logs/suggestions.yaml` — suggestions de dossiers

## Accès

```bash
./klodo.sh dashboard
# Ouvre http://localhost:8080
# Ctrl+C pour arrêter
```

Dépendance ajoutée : `fastapi`, `uvicorn`, `jinja2` dans `pyproject.toml`.

## Thème visuel

- Dark mode (#0f172a background, #e2e8f0 texte)
- Sidebar fixe à gauche (180px) avec navigation
- Couleurs : vert (#4ade80) PASS, rouge (#f87171) FAIL, jaune (#fbbf24) SKIP, bleu (#38bdf8) info, violet (#a78bfa) durée
- Responsive : minimum 1024px largeur

---

## Pages

### 1. Overview (`/`)

**KPIs en haut :**
- Pass / Fail / Skip / Pass Rate / Durée du dernier run
- Boutons : "Run All", "Run Failures"

**Heatmap des phases :**
- Chaque phase = une ligne
- Chaque série = un carré coloré (vert/rouge/jaune)
- Clic sur un carré → redirige vers la page Tests avec la série en focus

**Release Gate :**
- Status : READY / BLOCKED
- Liste des issues GitHub encore ouvertes
- Bouton "Fermer l'issue" sur les séries PASS (appelle `gh issue close`)

**Données :** dernier `report_*.json`

### 2. Tests (`/tests`)

**Filtres :**
- Par phase (dropdown)
- Par statut (PASS/FAIL/SKIP)
- Par priorité

**Liste des séries :**
- Regroupées par phase (accordéon)
- Chaque série affiche : ID, nom, nombre de checks pass/total, durée
- Boutons par série : "Run" (lance le test), "Details" (expand les checks), lien issue GitHub
- Bouton par phase : "Run Phase"
- Bouton global : "Run All", "Run Failures"

**Série expandée :**
- Liste des checks avec statut, commande, output, erreur
- Les FAIL sont surlignés en rouge avec le détail de l'erreur

**Actions interactives (HTMX) :**
- Clic "Run" → POST `/api/run?series=T1.1` → le serveur exécute `python run_functional.py --series T1.1` → retourne le résultat → HTMX remplace le HTML de la série
- Clic "Fermer issue" → POST `/api/issue/close?number=89` → appelle `gh issue close 89 --comment "..."` → met à jour le badge

**Données :** `tests.yaml` + dernier `report_*.json`

### 3. Rapports (`/rapports`)

**Sélecteurs :**
- Type de rapport : classify / rename / refine / process
- Fichier rapport (dropdown avec les fichiers disponibles dans logs/)
- Lien vers le test qui a produit ce rapport (via timestamps)

**Stats en haut :**
- Total fichiers, classifiés, non classifiés, confiance moyenne, coût API

**Barre de recherche :**
- Filtrer par nom de fichier
- Filtrer par statut (classifié / non_classifié / erreur)
- Filtrer par section

**Tableau interactif :**
- Colonnes triables : Fichier, Thème, Destination, Confiance, Status
- Badges colorés par statut
- Pagination

**Données :** `logs/rapport_*.csv`, `logs/refine_*.csv`, `logs/log_renommage_*.csv`

### 4. Comparer (`/comparer`)

**Sélecteur :**
- Rapport A (avant) et Rapport B (après)
- Même type de rapport uniquement
- Affichage du test associé à chaque rapport

**Diff stats :**
- Nouveaux classifiés, régressions, destination changée, delta confiance

**Filtres par type de changement :**
- Tout / Nouveaux / Régressions / Changés / Inchangés

**Tableau diff :**
- Colonnes : Fichier, Status A → Status B, Destination A → B
- Couleurs : vert (amélioration), rouge (régression), bleu (changé)

**Données :** 2 fichiers CSV comparés ligne par ligne (clé = nom de fichier)

### 5. Métriques (`/metriques`)

**Section Classification :**
- KPIs : taux de classification, confiance moyenne, coût API, suggestions en attente
- Donut chart (Chart.js) : répartition par section
- Bar chart horizontal : top thèmes détectés

**Section Renommage :**
- KPIs : taux de renommage, déjà propres, échecs, LLM Vision utilisé

**Section Fichiers problématiques :**
- Tableau des fichiers récurrents en erreur (sur plusieurs runs)
- Colonnes : Fichier, Problème, Confiance, Récurrence (N/M runs)

**Données :** dernier `rapport_classify_*.csv` + `rapport_rename_*.csv` + historique `report_*.json`

### 6. Historique (`/historique`)

**Graphique stacked bar (Chart.js) :**
- X = runs (par date)
- Y = nombre de checks pass/fail/skip empilés
- Le dernier run surligné

**Graphique line (Chart.js) :**
- Pass rate % sur le temps

**Graphique bar (Chart.js) :**
- Durée par run

**Timeline table :**
- Chaque run : date, pass, fail, skip, rate, durée, delta vs précédent
- Bouton "Voir" → redirige vers la page Tests avec ce rapport chargé

**Données :** tous les `report_*.json` dans `tests/functional/reports/`

### 7. Suggestions (`/suggestions`) — lecture seule

**Stats :**
- En attente, fichiers concernés

**Liste des suggestions :**
- Thème détecté
- Dossier proposé (chemin complet)
- Raison de la suggestion
- Liste des fichiers concernés (expandable)

**Pas de boutons d'action** — les suggestions se gèrent via CLI (`./klodo.sh suggest --apply --execute`). Le dashboard est un viewer.

**Données :** `logs/suggestions.yaml`

---

## API endpoints (FastAPI)

### Lecture (GET)
- `GET /` — page Overview
- `GET /tests` — page Tests
- `GET /rapports` — page Rapports
- `GET /comparer` — page Comparer
- `GET /metriques` — page Métriques
- `GET /historique` — page Historique
- `GET /suggestions` — page Suggestions

### API data (GET, retourne JSON pour HTMX)
- `GET /api/reports` — liste des rapports JSON disponibles
- `GET /api/reports/{filename}` — contenu d'un rapport
- `GET /api/csv/{type}` — liste des CSV par type (classify/rename/refine)
- `GET /api/csv/{type}/{filename}` — contenu d'un CSV parsé en JSON
- `GET /api/suggestions` — contenu de suggestions.yaml

### Actions (POST)
- `POST /api/run` — exécuter un test
  - Params : `series=T1.1` ou `phase=0` ou `all=true` ou `failures=true`
  - Retourne : HTML fragment (HTMX) avec le résultat
- `POST /api/issue/close` — fermer une issue GitHub
  - Params : `number=89`, `comment=PASS - test validé`
  - Retourne : HTML fragment avec le nouveau statut

---

## Interactions HTMX

Les actions interactives utilisent HTMX pour éviter le rechargement de page :

```html
<!-- Bouton Run sur une série -->
<button hx-post="/api/run?series=T1.1"
        hx-target="#series-T1-1"
        hx-swap="outerHTML"
        hx-indicator="#spinner-T1-1">
  ▶ Run
</button>

<!-- Bouton fermer issue -->
<button hx-post="/api/issue/close?number=89"
        hx-target="#issue-89"
        hx-swap="outerHTML">
  Fermer #89
</button>

<!-- Filtre par statut (rechargement partiel) -->
<select hx-get="/api/tests?status=fail"
        hx-target="#tests-list"
        hx-swap="innerHTML">
```

---

## Contraintes

- **Standalone** : le dashboard est un module du projet, pas un service séparé
- **Dépendances minimales** : FastAPI + uvicorn + jinja2 (déjà dans l'écosystème Python)
- **Chart.js via CDN** : pas de build frontend, un seul `<script>` tag
- **HTMX via CDN** : idem
- **Pas de base de données** : lit les fichiers JSON/CSV/YAML directement
- **Pas d'authentification** : app locale, accès localhost uniquement
- **Pas de modification de données** : le dashboard ne modifie pas les rapports, les CSV, ni la bibliothèque. Seules actions : exécuter des tests et fermer des issues GitHub
