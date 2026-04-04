# Cahier de tests fonctionnels — Klodo v1.0.0-dev

## Prérequis

**Environnement de test :**
- Copie de la bibliothèque sur un volume dédié (ex. `/Volumes/ExtSSD/BIBLIO-TEST`)
- Structure identique à la production : 9 sections + `_A-TRIER` + `_INBOX`
- Profil de test pointant vers cette copie (modifier `profile.yaml` temporairement)
- Clé API SiliconFlow configurée dans `SILICONFLOW_API_KEY`

**Script de remise à zéro :**
```bash
# Dry-run : voir ce qui serait déplacé
./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST

# Déplacer tout vers _INBOX
./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST --execute

# Variante : ne remettre que 500 fichiers (tests rapides)
./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST --max 500 --execute
```

**Après chaque série de tests :**
- Relancer `flatten_to_inbox.sh --execute` pour repartir de zéro
- Supprimer les checkpoints : `rm logs/checkpoint_*.json`

---

## Phase 0 — Vérifications préalables

### T0.1 — Intégrité de l'arborescence
Après `flatten_to_inbox.sh`, vérifier que l'arborescence est vide mais intacte.

| # | Action | Vérification |
|---|--------|-------------|
| T0.1a | `find BIBLIO-TEST -name "*.pdf" -not -path "*/_INBOX/*" \| wc -l` | Résultat = 0 |
| T0.1b | `ls BIBLIO-TEST/02-INFORMATIQUE/05-IA-ML/` | Les sous-dossiers existent toujours (Deep-Learning, Machine-Learning, NLP, Vision-par-Ordinateur) |
| T0.1c | `find BIBLIO-TEST/_INBOX -name "*.pdf" \| wc -l` | Résultat = nombre total de PDFs de la copie |

### T0.2 — Profil et configuration
| # | Action | Vérification |
|---|--------|-------------|
| T0.2a | `./klodo.sh profiles` | Le profil `default` apparaît avec le bon chemin target |
| T0.2b | `./klodo.sh classify --help` | L'aide s'affiche sans erreur |
| T0.2c | Vérifier que `SILICONFLOW_API_KEY` est défini | `echo $SILICONFLOW_API_KEY` affiche la clé |

---

## Phase 1 — Renommage (`rename`)

### T1.1 — Dry-run sur un petit lot
```bash
./klodo.sh rename /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 20 --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T1.1a | Le rapport CSV est généré dans `logs/` | Fichier `rapport_rename_*.csv` créé |
| T1.1b | Aucun fichier n'est renommé (pas de `--execute`) | Les noms sur disque sont inchangés |
| T1.1c | Le rapport contient 20 lignes | `wc -l` sur le CSV (hors header) |
| T1.1d | Colonnes `fichier`, `nouveau_nom`, `status` présentes | Ouvrir le CSV |

### T1.2 — Renommage effectif (sans LLM)
```bash
./klodo.sh rename /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 50 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T1.2a | Les fichiers avec noms "junk" (hash MD5, IDs numériques) sont renommés | Noms propres "Titre - Auteur.pdf" ou "Titre.pdf" |
| T1.2b | Les fichiers déjà propres gardent leur nom | Status `déjà_propre` dans le rapport |
| T1.2c | Pas de doublon créé | Aucun écrasement, suffixe `(2)` si collision |
| T1.2d | Le rapport CSV reflète les renommages effectués | `status = renommé` pour les fichiers traités |

### T1.3 — Renommage avec LLM Vision (fallback)
```bash
./klodo.sh rename /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 20 --llm --pages 2 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T1.3a | Les fichiers que le renommage classique n'a pas pu traiter sont envoyés au LLM | Log contient "LLM Vision" pour ces fichiers |
| T1.3b | Le LLM retourne un titre/auteur exploitable | Noms plus intelligents que les originaux |
| T1.3c | `--pages 2` envoie bien 2 pages au LLM | Log mentionne "2 pages" |
| T1.3d | Pas d'erreur API fatale | Pas de crash, erreurs API loguées proprement |

### T1.4 — Renommage LLM en mode force
```bash
./klodo.sh rename /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 10 --llm --force --pages 3 --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T1.4a | `--force` ré-analyse même les fichiers au nom propre | Tous les 10 fichiers passent par le LLM |
| T1.4b | `--pages 3` envoie 3 pages | Log confirme |
| T1.4c | Le LLM est appelé en priorité (pas en fallback) | Comportement différent du T1.3 |

### T1.5 — Idempotence du renommage
```bash
# Relancer le même lot
./klodo.sh rename /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 50 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T1.5a | Les fichiers déjà renommés ne sont pas re-renommés | Status `déjà_propre` |
| T1.5b | Le rapport montre 0 fichiers effectivement renommés | Tous en `déjà_propre` ou `inchangé` |

---

## Phase 2 — Classification (`classify`)

### T2.1 — Dry-run classification
```bash
./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 100 --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T2.1a | Rapport CSV généré avec colonnes `destination`, `score`, `mot_cle`, `theme_detecte` | Fichier dans `logs/` |
| T2.1b | Aucun fichier déplacé (pas de `--execute`) | Fichiers toujours dans `_INBOX` |
| T2.1c | Le résumé affiche le breakdown par status | classifié / non_classifié / erreur |
| T2.1d | Le top thèmes détectés est cohérent | Les thèmes correspondent aux titres |

### T2.2 — Classification effective (petit lot)
```bash
./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 100 -w 10 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T2.2a | Les fichiers classifiés sont copiés dans les bons sous-dossiers | `ls` dans les dossiers cible |
| T2.2b | Les fichiers classifiés sont supprimés de `_INBOX` | Plus présents dans `_INBOX` |
| T2.2c | Les non-classifiés vont dans `_A-TRIER` | `ls _A-TRIER` contient les fallbacks |
| T2.2d | La sécurité inbox fonctionne | Pas de perte de données |
| T2.2e | Le checkpoint est créé | `logs/checkpoint_classify_*.json` existe |

### T2.3 — Cascade de classification (4 niveaux)
Repérer dans le rapport CSV des fichiers classifiés par chaque niveau :

| # | Niveau | Comment vérifier | Indice dans le rapport |
|---|--------|-----------------|----------------------|
| T2.3a | Theme Mapping | `mot_cle` contient un thème connu (ex: "machine learning") | `score` élevé, pas d'appel LLM |
| T2.3b | Keyword Classifier | `mot_cle` contient un mot-clé du YAML | `score` entre 0.5-0.9 |
| T2.3c | LLM Mapper | Fichier sans mot-clé évident mais classifié | Log mentionne "LLM Mapper" |
| T2.3d | Suggestion | Thème inconnu → suggestion dans `logs/suggestions.yaml` | Status `suggestion` |

### T2.4 — Classification avec Vision
```bash
./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 30 --vision --pages 2 -w 5 --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T2.4a | Les fichiers non résolus par texte passent en Vision | Log "escalade vision" |
| T2.4b | La Vision améliore le taux de classification | Plus de `classifié` qu'en T2.2 |
| T2.4c | Le coût API est raisonnable | Résumé affiche coût < $0.02 pour 30 fichiers |

### T2.5 — Multi-workers
```bash
# Comparer 1 worker vs 10 workers sur 200 fichiers
time ./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 200 -w 1 --verbose
# (reset checkpoint)
time ./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 200 -w 10 --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T2.5a | Les résultats sont identiques avec 1 et 10 workers | Mêmes destinations |
| T2.5b | 10 workers est significativement plus rapide | Au moins 3x plus rapide |
| T2.5c | Pas de race condition ou crash | Aucune erreur de thread |

---

## Phase 3 — Pipeline complet (`process`)

### T3.1 — Pipeline sans renommage
```bash
./klodo.sh process /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --no-rename --max 100 -w 10 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T3.1a | Le renommage est sauté | Pas de rapport rename, pas de log renommage |
| T3.1b | La classification s'exécute | Rapport CSV de classification créé |
| T3.1c | Les fichiers sont copiés/déplacés | Moins de fichiers dans `_INBOX` |

### T3.2 — Pipeline complet (rename + classify + refine)
```bash
# Remettre à zéro d'abord
./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST --execute
rm logs/checkpoint_*.json

./klodo.sh process /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --llm --pages 2 -w 10 --max 200 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T3.2a | Le renommage s'exécute en premier | Rapport rename créé |
| T3.2b | La classification suit | Rapport classify créé |
| T3.2c | Le refine est lancé à la fin | Log "raffinement" visible |
| T3.2d | Les fichiers passent par les 3 étapes | Noms propres + dans le bon dossier |
| T3.2e | Le résumé final est cohérent | Nombres de fichiers corrects |

### T3.3 — Pipeline sur grand volume (stress test)
```bash
# Remettre tout à plat
./scripts/flatten_to_inbox.sh /Volumes/ExtSSD/BIBLIO-TEST --execute
rm logs/checkpoint_*.json

# Lancer sur les ~18000 fichiers
./klodo.sh process /Volumes/ExtSSD/BIBLIO-TEST/_INBOX -w 10 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T3.3a | Le process ne crash pas sur 18000+ fichiers | Terminaison propre |
| T3.3b | Taux de classification > 90% | Résumé final |
| T3.3c | Temps raisonnable | < 1h sans LLM, < 4h avec LLM Vision |
| T3.3d | La mémoire reste stable | Pas de fuite mémoire (`htop`) |
| T3.3e | Les erreurs sont loguées proprement | Pas de stacktrace non catchée |

---

## Phase 4 — Raffinement (`refine`)

### T4.1 — Refine après classification
Après un `classify --execute`, lancer le refine :
```bash
./klodo.sh refine --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T4.1a | Le scan détecte les fichiers dans des dossiers parents | Ex: fichier dans `02-INFORMATIQUE` au lieu de `02-INFORMATIQUE/05-IA-ML/Deep-Learning` |
| T4.1b | Les règles YAML matchent les bons fichiers | `mot_cle` dans le rapport correspond aux règles de `refinement.yaml` |
| T4.1c | Le matching implicite (noms de sous-dossiers) fonctionne | Fichier "Python Tutorial.pdf" → `03-Langages-Programmation/Python` |
| T4.1d | Le rapport CSV de refine est généré | `logs/rapport_refine_*.csv` |

### T4.2 — Refine avec LLM fallback
```bash
./klodo.sh refine --llm --max 50 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T4.2a | Les fichiers non résolus par YAML/implicite passent au LLM | Log "LLM fallback" |
| T4.2b | Les déplacements sont effectués | Fichiers bougés vers les sous-dossiers |
| T4.2c | `--max 50` limite les appels LLM | Au plus 50 appels API |
| T4.2d | Les fichiers `_AUCUN` restent en place | Pas de déplacement si LLM dit `_AUCUN` |

### T4.3 — Refine avec escalade Vision
```bash
./klodo.sh refine --llm --vision --max 20 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T4.3a | Quand le LLM texte échoue, la Vision prend le relais | Log "escalade vision" |
| T4.3b | La Vision améliore le taux de raffinement | Plus de fichiers déplacés qu'en T4.2 |

---

## Phase 5 — Reprise et checkpoint

### T5.1 — Interruption et reprise
```bash
# Lancer une classification longue
./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST/_INBOX -w 10 --execute --verbose &
PID=$!

# Attendre 30 secondes puis interrompre
sleep 30 && kill $PID

# Reprendre
./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST/_INBOX -w 10 --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T5.1a | Le checkpoint est sauvegardé pendant le traitement | `logs/checkpoint_classify_*.json` existe |
| T5.1b | La reprise ne retraite pas les fichiers déjà faits | Log "reprise depuis checkpoint, N fichiers déjà traités" |
| T5.1c | Le résultat final est identique à un run sans interruption | Mêmes destinations |

### T5.2 — Reset du checkpoint
```bash
./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 50 --execute
./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 50 --reset --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T5.2a | `--reset` supprime le checkpoint | Fichier checkpoint effacé |
| T5.2b | Le traitement repart de zéro | Tous les fichiers sont retraités |

### T5.3 — Retry des erreurs
```bash
./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --retry-errors --execute --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T5.3a | Seuls les fichiers en erreur sont retraités | Les `classifié` et `non_classifié` ne bougent pas |
| T5.3b | Les erreurs transientes (erreur_api) sont résolues au retry | Certains passent de `erreur_api` à `classifié` |

---

## Phase 6 — Suggestions de nouveaux dossiers

### T6.1 — Générer des suggestions
```bash
# Classifier un lot qui contient des thèmes atypiques
./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 500 -w 10 --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T6.1a | Des suggestions apparaissent dans le résumé | "N suggestions de nouveaux dossiers" |
| T6.1b | Le fichier `logs/suggestions.yaml` est créé | Contient des entrées avec `theme`, `folder`, `reason` |

### T6.2 — Review des suggestions
```bash
./klodo.sh suggest
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T6.2a | Les suggestions sont listées avec thème, dossier proposé, raison | Tableau formaté |
| T6.2b | Les fichiers concernés sont mentionnés | Nom du PDF associé |

### T6.3 — Appliquer les suggestions
```bash
./klodo.sh suggest --apply --execute
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T6.3a | Les nouveaux dossiers sont créés sur disque | `ls` confirme |
| T6.3b | `tree.yaml` est mis à jour | Nouvelle entrée dans le fichier |
| T6.3c | `theme_mapping.yaml` est enrichi | Nouveau mapping thème → dossier |
| T6.3d | Les fichiers concernés sont reclassifiés | Déplacés vers le nouveau dossier |

---

## Phase 7 — Sécurité et cas limites

### T7.1 — Protection inbox = target
```bash
# Essayer de classifier la bibliothèque sur elle-même
./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST --execute
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T7.1a | L'outil refuse de s'exécuter | Message d'erreur "inbox et target pointent vers le même dossier" |
| T7.1b | Aucun fichier n'est touché | Zéro modification |

### T7.2 — Fichiers avec noms problématiques
Créer manuellement dans `_INBOX` des fichiers aux noms difficiles :
```bash
touch "_INBOX/fichier avec  espaces   multiples.pdf"
touch "_INBOX/fichier{avec}accolades.pdf"
touch '_INBOX/fichier"avec"guillemets.pdf'
touch "_INBOX/très-long-nom-$(python3 -c 'print("a"*200)').pdf"
touch "_INBOX/.fichier_caché.pdf"
touch "_INBOX/fichier.pdf.pdf"
touch "_INBOX/ALLCAPS_TITRE_LIVRE.pdf"
```

| # | Action | Attendu |
|---|--------|---------|
| T7.2a | `./klodo.sh rename _INBOX --max 10 --verbose` | Noms assainis sans crash |
| T7.2b | `./klodo.sh classify _INBOX --max 10 --verbose` | Noms sanitisés dans les prompts LLM, pas d'erreur `.format()` |
| T7.2c | Le fichier caché (`.fichier`) est ignoré ou traité proprement | Pas de crash |
| T7.2d | Le nom trop long est tronqué | Nom < 200 caractères |

### T7.3 — Fichiers PDF corrompus
```bash
# Créer un faux PDF (fichier texte avec extension .pdf)
echo "ceci n'est pas un pdf" > _INBOX/faux_document.pdf
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T7.3a | `./klodo.sh rename _INBOX --max 5 --verbose` | Status `erreur_extraction`, pas de crash |
| T7.3b | `./klodo.sh classify _INBOX --max 5 --verbose` | Idem, erreur loguée proprement |

### T7.4 — Pas de clé API
```bash
unset SILICONFLOW_API_KEY
./klodo.sh classify /Volumes/ExtSSD/BIBLIO-TEST/_INBOX --max 5 --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T7.4a | Message d'erreur clair | "SILICONFLOW_API_KEY non défini" ou équivalent |
| T7.4b | Pas de crash avec traceback | Message lisible |

### T7.5 — Dossier source vide
```bash
mkdir -p /tmp/empty_inbox
./klodo.sh classify /tmp/empty_inbox --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T7.5a | Message "aucun fichier trouvé" | Terminaison propre, pas d'erreur |
| T7.5b | Pas de division par zéro dans le résumé | Résumé correct |

### T7.6 — Dossier source inexistant
```bash
./klodo.sh classify /chemin/qui/nexiste/pas --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T7.6a | Message d'erreur clair | "Dossier introuvable" |
| T7.6b | Exit code non-zéro | `echo $?` = 1 |

---

## Phase 8 — Qualité de classification (validation manuelle)

### T8.1 — Échantillon de 50 fichiers
Après un `process --execute` sur un lot de 200+ fichiers, vérifier manuellement la qualité.

```bash
# Extraire un échantillon aléatoire du rapport
shuf -n 50 logs/rapport_classify_*.csv | head -50
```

| # | Critère | Attendu |
|---|---------|---------|
| T8.1a | Taux de bonne classification | > 85% des fichiers sont dans le bon dossier |
| T8.1b | Pas de confusion entre sections majeures | Un livre de maths n'est pas dans 08-LOISIRS |
| T8.1c | Les sous-dossiers sont pertinents | "Deep Learning with Python" → `05-IA-ML/Deep-Learning`, pas juste `05-IA-ML` |
| T8.1d | Les `_A-TRIER` sont légitimes | Vrais cas ambigus, pas des erreurs évidentes |

### T8.2 — Vérification par section
Pour chaque section majeure, vérifier un échantillon :

| Section | Fichier type à vérifier | Doit être dans |
|---------|------------------------|----------------|
| 01-SCIENCES | "Quantum Mechanics - Griffiths.pdf" | PHYSIQUE/05-Relativite-Quantique |
| 02-INFORMATIQUE | "Introduction to Algorithms - Cormen.pdf" | 02-Algorithmes-Structures |
| 02-INFORMATIQUE | "Learning Python - Mark Lutz.pdf" | 03-Langages-Programmation/Python |
| 03-INGENIERIE | "Digital Signal Processing.pdf" | TRAITEMENT-SIGNAL |
| 04-SHS | "Le Capital - Karl Marx.pdf" | ECONOMIE |
| 05-RELIGIONS | "Le Coran.pdf" | ISLAM |
| 06-MEDECINE | "Gray's Anatomy.pdf" | Anatomie |
| 08-LOISIRS | "Bobby Fischer Teaches Chess.pdf" | ECHECS |
| 09-BUSINESS | "The Intelligent Investor.pdf" | FINANCE |

### T8.3 — Faux positifs connus
Vérifier que les pièges documentés dans CLAUDE.md sont gérés :

| # | Piège | Attendu |
|---|-------|---------|
| T8.3a | "Albert Einstein" ne matche pas "bert" (NLP) | Pas dans 05-IA-ML/NLP |
| T8.3b | "Christopher Columbus" ne matche pas "christ" (religion) | Pas dans 05-RELIGIONS |
| T8.3c | "Linux Bible" ne matche pas "bible" (religion) | Reste dans 02-INFORMATIQUE |
| T8.3d | "Blaise Pascal" (philosophe) ne matche pas "Pascal" (langage) | Dans 04-SHS/PHILOSOPHIE, pas dans Langages |

---

## Phase 9 — Reclassification et enrichissement

### T9.1 — Reclassify après enrichissement du mapping
```bash
# 1. Lancer une classification
./klodo.sh classify _INBOX --max 200 -w 10 --execute

# 2. Ajouter manuellement des entrées dans theme_mapping.yaml
# 3. Relancer avec --reclassify
./klodo.sh classify _INBOX --reclassify --execute
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T9.1a | Les fichiers dont le thème matche le nouveau mapping sont reclassifiés | Déplacés vers le bon dossier |
| T9.1b | Pas d'appel LLM supplémentaire | Le re-mapping est gratuit (YAML) |

### T9.2 — Auto-apprentissage du LLM Mapper
```bash
./klodo.sh classify _INBOX --max 100 -w 10 --execute --verbose
# Observer logs/theme_mapping.yaml
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T9.2a | Les thèmes résolus par le LLM Mapper sont ajoutés à `theme_mapping.yaml` | Nouvelles entrées dans le fichier |
| T9.2b | Au prochain run, ces thèmes sont résolus instantanément | Pas d'appel LLM pour ces thèmes |

---

## Phase 10 — Performance et stabilité

### T10.1 — Mesurer les temps
```bash
# Renommage seul (sans LLM)
time ./klodo.sh rename _INBOX --max 500

# Classification seule (10 workers)
time ./klodo.sh classify _INBOX --max 500 -w 10

# Pipeline complet avec LLM
time ./klodo.sh process _INBOX --llm --pages 1 -w 10 --max 500
```

| # | Mesure | Attendu |
|---|--------|---------|
| T10.1a | Renommage 500 fichiers (sans LLM) | < 60 secondes |
| T10.1b | Classification 500 fichiers (10 workers) | < 5 minutes (dépend de l'API) |
| T10.1c | Pipeline complet 500 fichiers avec LLM | < 15 minutes |

### T10.2 — Stabilité réseau (erreurs API)
```bash
# Limiter le timeout pour provoquer des erreurs
./klodo.sh classify _INBOX --max 50 --delay 0 -w 20 --verbose
```

| # | Vérification | Attendu |
|---|-------------|---------|
| T10.2a | Les erreurs 429 (rate limit) sont retry avec backoff | Log "retry" visible |
| T10.2b | Les erreurs 500 ne crashent pas | Status `erreur_api` dans le rapport |
| T10.2c | Le traitement continue après les erreurs | Les autres fichiers sont traités |
| T10.2d | Max erreurs consécutives arrête proprement | Si > 10 erreurs d'affilée, arrêt avec message |

---

## Grille récapitulative

| Phase | Tests | Priorité | Durée estimée |
|-------|-------|----------|---------------|
| 0 — Prérequis | 3 | Obligatoire | 5 min |
| 1 — Renommage | 5 séries | Haute | 20 min |
| 2 — Classification | 5 séries | Haute | 30 min |
| 3 — Pipeline complet | 3 séries | Haute | 45 min |
| 4 — Raffinement | 3 séries | Moyenne | 20 min |
| 5 — Checkpoint/reprise | 3 séries | Haute | 15 min |
| 6 — Suggestions | 3 séries | Moyenne | 15 min |
| 7 — Sécurité/limites | 6 séries | Haute | 15 min |
| 8 — Qualité manuelle | 3 séries | Haute | 30 min |
| 9 — Reclassification | 2 séries | Moyenne | 10 min |
| 10 — Performance | 2 séries | Basse | 20 min |
| **Total** | **38 séries** | | **~3h30** |

---

## Checklist rapide (mini-run)

Pour un test rapide en 30 minutes, exécuter dans l'ordre :

1. `./scripts/flatten_to_inbox.sh BIBLIO-TEST --max 200 --execute`
2. `./klodo.sh rename _INBOX --max 50 --execute --verbose` → vérifier rapport
3. `./klodo.sh classify _INBOX --max 100 -w 10 --execute --verbose` → vérifier destinations
4. `./klodo.sh refine --verbose` → vérifier raffinement
5. `./klodo.sh suggest` → vérifier suggestions
6. Vérifier 10 fichiers manuellement dans l'arborescence
7. `./scripts/flatten_to_inbox.sh BIBLIO-TEST --execute` → remettre à zéro
