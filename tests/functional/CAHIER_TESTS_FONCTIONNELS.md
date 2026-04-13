# Cahier de tests fonctionnels — Klodo v1.0.0-dev

> **Fichier généré automatiquement** depuis `tests/functional/tests.yaml`.
> Ne pas modifier manuellement — utiliser le YAML comme source de vérité.
>
> Généré le : 2026-04-06T14:00:00+02:00

---

## Prérequis

**Environnement de test :**
- Copie de la bibliothèque sur SSD externe (`/Volumes/ExtSSD/BIBLIO-TEST-FUNC`)
- Structure identique à la production : 9 sections + `_A-TRIER` + `_INBOX`
- Profil de test (`test`) pointant vers cette copie
- Clé API SiliconFlow configurée dans `SILICONFLOW_API_KEY`
- Python 3.13 via `uv`, `poppler` installé (`brew install poppler`)

**Script de remise à zéro :**
```bash
# Dry-run : voir ce qui serait déplacé
./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST-FUNC

# Déplacer tout vers _INBOX
echo oui | ./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST-FUNC --execute

# Variante : ne remettre que 500 fichiers (tests rapides)
echo oui | ./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST-FUNC --max 500 --execute
```

**Variables :**

| Variable | Valeur |
|----------|--------|
| `BIBLIO_TEST` | `/Volumes/ExtSSD/BIBLIO-TEST-FUNC` |
| `INBOX` | `${BIBLIO_TEST}/_INBOX` |
| `A_TRIER` | `${BIBLIO_TEST}/_A-TRIER` |
| `PROF` | `test` |
| `KLODO` | `./klodo.sh` |

---

## Grille récapitulative

| Phase | Nom | Séries | Checks | Priorité | Durée estimée |
|-------|-----|--------|--------|----------|---------------|
| 0 | Prérequis et smoke tests | 3 | 14 | Obligatoire | 5 min |
| 1 | Renommage | 5 | 13 | Obligatoire | 15 min |
| 2 | Classification LLM | 6 | 18 | Obligatoire | 22 min |
| 3 | Raffinement | 4 | 8 | Haute | 10 min |
| 4 | Pipeline complet (process) | 4 | 10 | Obligatoire | 20 min |
| 5 | Commandes utilitaires | 3 | 10 | Moyenne | 5 min |
| 6 | Sécurité et cas limites | 6 | 13 | Haute | 5 min |
| 7 | Performance et stabilité | 3 | 7 | Basse | 30 min |
| 8 | Qualité de classification | 4 | 13 | Haute | 15 min |
| 9 | Reclassification et enrichissement | 3 | 8 | Moyenne | 15 min |
| 10 | Benchmark et stabilité | 4 | 14 | Basse | 45 min |
| **Total** | | **46** | **129** | | **~187 min** |

**Automatisation :** 111 checks auto (86%) + 18 checks manuels (14%)

---

## Checklist rapide (~30 min)

Pour une validation rapide, lancer ces commandes dans l'ordre :

```bash
# 1. Vérifier l'environnement
test -d /Volumes/ExtSSD/BIBLIO-TEST-FUNC && echo "SSD OK"

# 2. Remettre à zéro
echo oui | ./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST-FUNC --execute

# 3. Smoke test
./klodo.sh --version
./klodo.sh profiles

# 4. Rename rapide (20 fichiers)
./klodo.sh rename /Volumes/ExtSSD/BIBLIO-TEST-FUNC/_INBOX --profile test --max 20 --execute --verbose

# 5. Classify rapide (30 fichiers)
echo oui | ./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST-FUNC --execute
./klodo.sh classify -y /Volumes/ExtSSD/BIBLIO-TEST-FUNC/_INBOX --profile test --max 30 --workers 5 --execute --verbose

# 6. Pipeline complet (50 fichiers)
echo oui | ./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST-FUNC --execute
./klodo.sh process -y /Volumes/ExtSSD/BIBLIO-TEST-FUNC/_INBOX --profile test --max 50 --workers 5 --execute --verbose

# 7. Sécurité
./klodo.sh classify -y /Volumes/ExtSSD/BIBLIO-TEST-FUNC --profile test 2>&1 | grep SÉCURITÉ
```

---

## Phase 0 — Prérequis et smoke tests

### T0.1 — Environnement de test

Vérifie que le SSD de test est monté, le profil configuré, et les dépendances installées.

| # | Vérification | Attendu |
|---|-------------|---------|
| T0.1a | Le volume SSD de test est monté | `OK` affiché |
| T0.1b | Le dossier `_INBOX` existe | `OK` affiché |
| T0.1c | Le profil `test` est listé par `profiles` | Exactement 1 occurrence trouvée |
| T0.1d | La variable `SILICONFLOW_API_KEY` est définie | `OK` affiché |
| T0.1e | Python 3.13 est disponible via `uv` | Version 3.13 détectée |
| T0.1f | `pdftoppm` (poppler) est installé | `OK` affiché |

### T0.2 — Smoke test CLI

Vérifie que le CLI se lance et répond aux commandes de base.

```bash
./klodo.sh --version
./klodo.sh
./klodo.sh profiles
./klodo.sh classify --profile inexistant /Volumes/ExtSSD/BIBLIO-TEST-FUNC/_INBOX
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T0.2a | `--version` affiche la version | Contient `klodo` et `1.0.0` |
| T0.2b | Sans argument, affiche toutes les sous-commandes | Contient `process`, `classify`, `rename`, `refine`, `clean`, `profiles` |
| T0.2c | `profiles` liste au moins le profil `default` | Contient `default` |
| T0.2d | Profil inexistant échoue proprement | Contient `introuvable` |

### T0.3 — Script de remise à zéro

Après remise à zéro complète, vérifie que tous les PDFs sont dans `_INBOX` et l'arborescence est intacte.

```bash
echo oui | ./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST-FUNC --execute
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T0.3a | Aucun PDF hors `_INBOX` après flatten | Compteur = 0 |
| T0.3b | `_INBOX` contient des PDFs | Compteur > 0 |
| T0.3c | L'arborescence `01-SCIENCES` est préservée (vide mais intacte) | Contient `MATHEMATIQUES`, `PHYSIQUE` |
| T0.3d | Flatten en dry-run ne déplace rien | Affiche `Relancer avec --execute` |

---

## Phase 1 — Renommage (`rename`)

### T1.1 — Dry-run sur un petit lot

```bash
./klodo.sh rename /Volumes/ExtSSD/BIBLIO-TEST-FUNC/_INBOX --profile test --max 20 --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T1.1a | Un rapport CSV rename est généré dans `logs/` | Fichier `rapport_rename_*.csv` créé |
| T1.1b | Le rapport contient des lignes (hors header) | Plus de 0 lignes de données |
| T1.1c | Aucun fichier n'a été renommé | Aucun fichier plus récent que le rapport |
| T1.1d | Le CSV contient les colonnes `fichier` et `nouveau_nom` | Colonnes présentes dans le header |

### T1.2 — Renommage effectif (sans LLM)

```bash
./klodo.sh rename /Volumes/ExtSSD/BIBLIO-TEST-FUNC/_INBOX --profile test --max 50 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T1.2a | Le rapport indique des fichiers renommés | Le mot `renomm` apparaît dans le CSV |
| T1.2b | Les noms renommés sont propres | Vérifier visuellement que les noms sont du type `Titre - Auteur.pdf`, pas des hash MD5 |
| T1.2c | Pas de fichier perdu après renommage | Le compteur de PDFs dans `_INBOX` est >= 50 |

### T1.3 — Rename avec LLM Vision

```bash
./klodo.sh rename /Volumes/ExtSSD/BIBLIO-TEST-FUNC/_INBOX --profile test --llm --pages 2 --max 10 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T1.3a | Le mode LLM Vision est affiché dans les logs | Contient `LLM Vision` |
| T1.3b | Des fichiers ont été renommés via LLM | Référence à `llm` ou `vision` dans le rapport |
| T1.3c | Qualité des noms LLM | Vérifier visuellement que les noms générés sont intelligents et descriptifs |

### T1.4 — Rename --force

```bash
./klodo.sh rename /Volumes/ExtSSD/BIBLIO-TEST-FUNC/_INBOX --profile test --max 20 --force --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T1.4a | Aucun fichier ignoré comme `déjà propre` | Le mot `propre` n'apparaît pas dans le rapport |

### T1.5 — Idempotence du renommage

Relancer le renommage sur des fichiers déjà propres : aucun ne devrait être modifié.

```bash
# Après T1.2 :
./klodo.sh rename /Volumes/ExtSSD/BIBLIO-TEST-FUNC/_INBOX --profile test --max 50 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T1.5a | Fichiers déjà renommés ont status INCHANGE | Le mot `INCHANGE` apparaît dans le rapport |
| T1.5b | 0 fichiers effectivement renommés au 2e passage | Aucune ligne OK dans le log de renommage |

---

## Phase 2 — Classification LLM (`classify`)

### T2.1 — Classify dry-run

```bash
./klodo.sh classify -y /Volumes/ExtSSD/BIBLIO-TEST-FUNC/_INBOX --profile test --max 30 --workers 5 --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T2.1a | Un rapport CSV classify est généré | Fichier `rapport_classify_*.csv` créé |
| T2.1b | Le rapport contient des résultats `classifié` | Le mot `classifié` apparaît dans le CSV |
| T2.1c | Aucun fichier copié en dry-run | 0 PDFs hors `_INBOX` |
| T2.1d | Un checkpoint est créé | Fichier `progress.json` existe dans le cache |
| T2.1e | Le résumé affiche les statistiques | Contient `classifié` et `fichiers` |

### T2.2 — Classify --execute (copie vers cible)

```bash
echo oui | ./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST-FUNC --execute
./klodo.sh classify -y /Volumes/ExtSSD/BIBLIO-TEST-FUNC/_INBOX --profile test --max 30 --workers 5 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T2.2a | Des fichiers sont copiés dans l'arborescence (hors `_A-TRIER`) | Compteur > 0 |
| T2.2b | Les fichiers classifiés sont retirés de l'inbox | Le log mentionne `supprimé` |
| T2.2c | Les non-classifiés sont dans `_A-TRIER` | Compteur >= 0 dans `_A-TRIER` |
| T2.2d | Les fichiers sont dans des dossiers cohérents | Vérifier visuellement `01-SCIENCES` et `02-INFORMATIQUE` |

### T2.3 — Checkpoint et reprise

```bash
# Premier run : 10 fichiers
./klodo.sh classify -y ... --max 10
# Deuxième run : reprend
./klodo.sh classify -y ... --max 20
# Avec --reset : repart de zéro
./klodo.sh classify -y ... --max 5 --reset
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T2.3a | Le checkpoint contient 10 résultats après le premier run | ~10 entrées dans le JSON |
| T2.3b | Le deuxième run affiche `Reprise` | Le mot `Reprise` apparaît 1 fois |
| T2.3c | Avec `--reset`, pas de reprise | Le mot `Reprise` n'apparaît pas |

### T2.4 — Classify --retry-errors

| # | Vérification | Attendu |
|---|-------------|---------|
| T2.4a | Le retry génère un rapport | Le mot `Rapport` apparaît dans la sortie |

### T2.5 — Classify avec escalade vision

```bash
./klodo.sh classify -y ... --max 10 --vision --pages 2 --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T2.5a | Le mode vision escalade est affiché | Contient `Vision` et `escalade` |

### T2.6 — Cache vision persistant

Vérifie que le cache vision persistant (`profiles/test/.cache/vision_cache.json`) est créé au premier run classify, réutilisé au second (économie d'appels LLM sur runs répétés) et purgeable via `clean vision-cache`.

```bash
./klodo.sh clean vision-cache --profile test --execute
./klodo.sh classify -y --profile test --workers 5 --verbose   # 1er run : population du cache
./klodo.sh clean progress --profile test --execute
./klodo.sh classify -y --profile test --workers 5 --verbose   # 2e run : hits cache vision
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T2.6a | Le fichier `vision_cache.json` est créé dans le cache du profil | `OK` affiché |
| T2.6b | Le cache contient au moins une entrée sérialisée | Nombre d'entrées > 0 |
| T2.6c | Les entrées ont la structure attendue | Clés `model`, `prompt_version`, `result.title` présentes |
| T2.6d | `clean vision-cache --execute` purge complètement le cache | Contient `PURGED` |

---

## Phase 3 — Raffinement (`refine`)

### T3.1 — Refine dry-run

```bash
./klodo.sh refine --profile test --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T3.1a | Un rapport CSV refine est généré | Fichier `refine_*.csv` créé |
| T3.1b | Le rapport contient des propositions | Plus de 0 lignes |
| T3.1c | Aucun fichier déplacé en dry-run | Le mot `déplacé` n'apparaît pas |

### T3.2 — Refine --execute

| # | Vérification | Attendu |
|---|-------------|---------|
| T3.2a | Le raffinement génère un rapport | Le mot `Rapport` apparaît |
| T3.2b | Les fichiers maths sont dans les bons sous-dossiers | Vérifier `Algebre`, `Analyse`, etc. |

### T3.3 — Refine avec LLM fallback

| # | Vérification | Attendu |
|---|-------------|---------|
| T3.3a | Le LLM refine est actif | Contient `LLM` |
| T3.3b | Des appels LLM ont été effectués | Le mot `appels` apparaît |

### T3.4 — Refine idempotence

| # | Vérification | Attendu |
|---|-------------|---------|
| T3.4a | Le deuxième run n'a rien à raffiner | Contient `bien classé` |

---

## Phase 4 — Pipeline complet (`process`)

### T4.1 — Process dry-run

| # | Vérification | Attendu |
|---|-------------|---------|
| T4.1a | Le pipeline affiche les étapes | Contient `Renommage`, `Classification`, `Pipeline` |
| T4.1b | Un rapport `process` est généré | Fichier `rapport_process_*.csv` créé |
| T4.1c | Aucun fichier copié en dry-run | 0 PDFs hors `_INBOX` |

### T4.2 — Process --execute (pipeline complet)

| # | Vérification | Attendu |
|---|-------------|---------|
| T4.2a | Les 4 étapes sont exécutées | Contient `Renommage`, `Classification`, `Copie`, `Raffinement`, `Pipeline terminé` |
| T4.2b | Des fichiers sont dans l'arborescence | Compteur > 0 hors `_INBOX` |
| T4.2c | L'inbox a été vidée des fichiers traités | Compteur réduit |
| T4.2d | Un rapport rename ET un rapport process existent | 2 fichiers CSV |

### T4.3 — Process --no-rename

| # | Vérification | Attendu |
|---|-------------|---------|
| T4.3a | Le pipeline indique que rename est désactivé | Contient `--no-rename` |
| T4.3b | Aucun rapport rename n'est généré | 0 fichier `rapport_rename_*.csv` |

### T4.4 — Process avec LLM Vision

| # | Vérification | Attendu |
|---|-------------|---------|
| T4.4a | Le mode LLM Vision est affiché | Contient `LLM Vision` |

---

## Phase 5 — Commandes utilitaires

### T5.1 — clean — Nettoyage du cache

| # | Vérification | Attendu |
|---|-------------|---------|
| T5.1a | `clean progress` en dry-run affiche les fichiers | Contient `DRY-RUN` |
| T5.1b | `clean progress --execute` supprime le checkpoint | Contient `supprimé` |
| T5.1c | `clean isbn --execute` supprime le cache ISBN | Contient `supprimé` |
| T5.1d | `clean logs --execute` supprime les rapports CSV | Contient `supprimé` |
| T5.1e | `clean` sur un cache déjà vide | Contient `Rien à nettoyer` |
| T5.1f | `clean vision-cache --execute` supprime `vision_cache.json` | Contient `DELETED` |

### T5.2 — init — Création de profil

| # | Vérification | Attendu |
|---|-------------|---------|
| T5.2a | `init` crée un nouveau profil | Contient `Profil` et `créé` |
| T5.2b | Le profil contient les fichiers YAML | Contient `profile.yaml` et `tree.yaml` |
| T5.2c | `init` sur un profil existant échoue | Contient `existe déjà` |

### T5.3 — suggest — Suggestions de dossiers

| # | Vérification | Attendu |
|---|-------------|---------|
| T5.3a | Sans suggestions, affiche un message clair | Contient `Aucune suggestion` |

---

## Phase 6 — Sécurité et cas limites

### T6.1 — Protection inbox/target

| # | Vérification | Attendu |
|---|-------------|---------|
| T6.1a | `classify` sur target directement est refusé | Contient `SÉCURITÉ` |
| T6.1b | `process` sur target directement est refusé | Contient `SÉCURITÉ` |

### T6.2 — Dossier inexistant

| # | Vérification | Attendu |
|---|-------------|---------|
| T6.2a | `classify` sur dossier inexistant échoue | Contient `introuvable` |
| T6.2b | `rename` sur dossier inexistant échoue | Contient `introuvable` |
| T6.2c | `process` sur dossier inexistant échoue | Contient `introuvable` |

### T6.3 — API key manquante

| # | Vérification | Attendu |
|---|-------------|---------|
| T6.3a | `rename --llm` sans API key affiche une erreur | Contient `clé API` |
| T6.3b | `refine --llm` sans API key affiche une erreur | Contient `clé API` |

### T6.4 — Fichiers problématiques

Créer dans `_INBOX` des fichiers aux noms difficiles : espaces multiples, noms très longs, fichiers cachés.

| # | Vérification | Attendu |
|---|-------------|---------|
| T6.4a | `rename` gère les espaces multiples sans crash | Pas de `Traceback` ni `Error` |
| T6.4b | Les noms très longs sont tronqués | Plus long nom < 200 caractères |

### T6.5 — Dossier source vide

| # | Vérification | Attendu |
|---|-------------|---------|
| T6.5a | `classify` sur dossier vide termine proprement | Exit code 0 |
| T6.5b | Pas de division par zéro dans le résumé | Aucun `ZeroDivision` dans la sortie |

### T6.6 — Fichiers PDF corrompus

Un faux PDF (fichier texte avec extension `.pdf`) ne doit pas crasher le pipeline.

| # | Vérification | Attendu |
|---|-------------|---------|
| T6.6a | `rename` gère le PDF corrompu sans crash | Exit code 0 |
| T6.6b | `classify` gère le PDF corrompu sans crash | Exit code 0 |

---

## Phase 7 — Performance et stabilité

### T7.1 — Traitement en volume (500 fichiers)

```bash
./klodo.sh process -y ... --max 500 --workers 10 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T7.1a | Le pipeline se termine sans crash | `Pipeline terminé` apparaît 1 fois |
| T7.1b | Temps de traitement raisonnable | Moins de 30 minutes |
| T7.1c | Taux de classification acceptable | Supérieur à 80% |

### T7.2 — Concurrence (20 workers)

| # | Vérification | Attendu |
|---|-------------|---------|
| T7.2a | 20 workers se terminent | Le mot `Rapport` apparaît |
| T7.2b | Le checkpoint est cohérent | JSON se charge, contient des entrées |

### T7.3 — Flatten en volume

| # | Vérification | Attendu |
|---|-------------|---------|
| T7.3a | Tous les PDFs sont dans `_INBOX` | 0 PDF hors `_INBOX` |
| T7.3b | Le nombre total est préservé | Compteur > 0 |

---

## Phase 8 — Qualité de classification

### T8.1 — Cascade de classification (4 niveaux)

Vérifie que les 4 niveaux du pipeline sont tous utilisés dans les résultats : Theme Mapping → Keyword Classifier → LLM Mapper → Suggestions.

| # | Vérification | Attendu |
|---|-------------|---------|
| T8.1a | Theme Mapping utilisé (score élevé, gratuit) | `theme_mapping` dans le rapport |
| T8.1b | Keyword Classifier utilisé (mots-clés YAML) | `keyword` dans le rapport |
| T8.1c | LLM Mapper utilisé (résolution thèmes inconnus) | `llm` dans le rapport |
| T8.1d | Suggestions de nouveaux dossiers générées | Vérifier dans le résumé ou `logs/suggestions.yaml` |

### T8.2 — Échantillon qualité (50 fichiers)

Vérification manuelle de la qualité sur un échantillon après un `process --execute`.

| # | Critère | Attendu |
|---|---------|---------|
| T8.2a | Taux de bonne classification > 85% | Plus de 85% dans le bon dossier |
| T8.2b | Pas de confusion entre sections majeures | Aucun livre de maths dans `08-LOISIRS`, aucun roman dans `01-SCIENCES` |
| T8.2c | Sous-dossiers pertinents | Deep Learning → `05-IA-ML`, pas juste `02-INFORMATIQUE` racine |
| T8.2d | Les `_A-TRIER` sont de vrais cas ambigus | Pas des erreurs évidentes |

### T8.3 — Vérification par section

| # | Critère | Attendu |
|---|---------|---------|
| T8.3a | Fichiers types dans les bonnes sections | Quantum Mechanics → PHYSIQUE, Algorithms → 02-Algorithmes, Python Tutorial → Python, Le Capital → ECONOMIE, Le Coran → ISLAM, Gray's Anatomy → Anatomie, Bobby Fischer → ECHECS, Intelligent Investor → FINANCE |

### T8.4 — Faux positifs connus

Vérifie que les pièges documentés (mots-clés ambigus) sont bien gérés.

| # | Piège | Attendu |
|---|-------|---------|
| T8.4a | `Albert Einstein` ne matche pas `bert` (NLP) | 0 Einstein dans `05-IA-ML` |
| T8.4b | `Christopher Columbus` ne matche pas `christ` (religion) | 0 Columbus/Christopher dans `05-RELIGIONS` |
| T8.4c | `Linux Bible` ne matche pas `bible` (religion) | 0 Linux dans `05-RELIGIONS` |
| T8.4d | `Blaise Pascal` (philosophe) ne matche pas `Pascal` (langage) | 0 Blaise Pascal dans `02-INFORMATIQUE` |

---

## Phase 9 — Reclassification et enrichissement

### T9.1 — Suggestions workflow complet

Générer, reviewer et appliquer les suggestions de nouveaux dossiers.

```bash
./klodo.sh classify -y ... --max 500 -w 10 --execute --verbose
./klodo.sh suggest --profile test
./klodo.sh suggest --profile test --apply --execute
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T9.1a | Suggestions mentionnées dans le résumé | Le mot `suggestion` ou `inconnu` apparaît |
| T9.1b | `suggest` liste les suggestions en attente | Exit code 0 |
| T9.1c | `suggest --apply --execute` crée les dossiers | Exit code 0 |
| T9.1d | Nouveaux dossiers créés et fichiers reclassifiés | Vérifier tree.yaml et theme_mapping.yaml mis à jour |

### T9.2 — Reclassify après enrichissement

`--reclassify` re-mappe les thèmes sans appel LLM (pure YAML).

| # | Vérification | Attendu |
|---|-------------|---------|
| T9.2a | Reclassify fonctionne | Le mot `Rapport` apparaît |
| T9.2b | Des fichiers sont re-mappés vers de meilleurs dossiers | Vérifier le rapport |

### T9.3 — Auto-apprentissage du LLM Mapper

Les thèmes résolus par le LLM sont persistés dans `theme_mapping.yaml`.

| # | Vérification | Attendu |
|---|-------------|---------|
| T9.3a | `theme_mapping.yaml` a été enrichi | Le fichier a plus de lignes qu'avant |
| T9.3b | Résolution instantanée au prochain run | Pas d'appel LLM pour les mêmes thèmes |

---

## Phase 10 — Benchmark et stabilité

### T10.1 — Benchmark workers (1 vs 10)

Compare les performances avec 1 worker vs 10 workers sur 50 fichiers.

| # | Mesure | Attendu |
|---|--------|---------|
| T10.1a | Ratio de vitesse 10w / 1w | Au moins 2x plus rapide |
| T10.1b | Temps logués | Les deux temps sont enregistrés |

### T10.2 — Benchmark par étape

| # | Mesure | Attendu |
|---|--------|---------|
| T10.2a | Renommage 500 fichiers (sans LLM) | < 60 secondes |
| T10.2b | Classification 500 fichiers (10 workers) | < 5 minutes |
| T10.2c | Pipeline complet 500 fichiers avec LLM | < 15 minutes |

### T10.3 — Stabilité réseau (erreurs API)

Provoquer des erreurs réseau avec `--delay 0` et 20 workers simultanés.

| # | Vérification | Attendu |
|---|-------------|---------|
| T10.3a | Erreurs 429 retry avec backoff | Traces de retry dans les logs |
| T10.3b | Erreurs 500 ne crashent pas le process | `erreur_api` dans le rapport, pas de crash |
| T10.3c | Le traitement continue après les erreurs | Plus de 0 lignes dans le rapport |
| T10.3d | Max erreurs consécutives arrête proprement | Message d'arrêt dans les logs |

### T10.4 — Stress test (volume complet ~18 000 fichiers)

Pipeline sur le volume complet de la bibliothèque de test.

```bash
echo oui | ./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST-FUNC --execute
./klodo.sh process -y /Volumes/ExtSSD/BIBLIO-TEST-FUNC/_INBOX --profile test -w 10 --execute --verbose
```

| # | Critère | Attendu |
|---|---------|---------|
| T10.4a | Terminaison propre | `Pipeline terminé` apparaît |
| T10.4b | Taux de classification > 90% | Vérifier dans le résumé |
| T10.4c | Temps raisonnable | < 1 heure (sans LLM rename) |
| T10.4d | Pas de Traceback non catchée | 0 Traceback dans `klodo.log` |
| T10.4e | Mémoire stable (pas de fuite) | Vérifier via `htop` |
