# Agent Onboarding — bootstrap d'un profil depuis un répertoire brut

**Date** : 2026-06-14
**Statut** : validé (brainstorming)
**Remplace** : `docs/onboarding-agent-spec.md` (antérieure aux refontes de juin — obsolète)

## Contexte & objectif

Aujourd'hui, créer un profil = `./klodo.sh init <nom> --target /path` puis **éditer 3 YAMLs à la
main** (`tree.yaml`, `theme_mapping.yaml`, `categories.yaml`) avant de pouvoir classer quoi que ce
soit. Problème **chicken-and-egg** : on ne sait pas écrire un bon `theme_mapping.yaml` avant de
connaître les thèmes que la LLM Vision va faire sortir du corpus.

L'agent Onboarding casse ce blocage pour un profil **from scratch** : il analyse un répertoire brut,
propose une **taxonomie de départ** (les 3 YAMLs), montre la **vraie couverture** par un dry-run, et
rend la main aux outils Taxonomie/Apply existants pour le raffinage et l'application.

C'est le **2ᵉ agent** de Klodo après Refonte. Il vit dans `agents/onboarding/`.

## Le vrai problème : analyse complète, pas échantillon

**Décision structurante.** Un échantillon (50-200 fichiers) ne fait sortir qu'une *fraction* des
thèmes réels → un `theme_mapping` bâti dessus laisse la **majorité des fichiers non mappés** → ils
tombent tous dans « Autres » → profil inutilisable.

Donc, pour un profil from scratch, l'onboarding **actionne tout le pipeline de classification** :
**Vision LLM sur l'ensemble du corpus** → tous les thèmes réels → clustering → proposition d'une
taxonomie qui les **couvre vraiment** → **dry-run de classify sur tout le corpus** → vraie
distribution → raffinage → application. Le coût Vision complet est assumé : c'est le prix d'un vrai
onboarding, et il est récupéré (le `vision_cache` peuplé rend le classify final gratuit, sans
re-Vision).

## Décisions actées (brainstorming)

| Décision | Choix |
| --- | --- |
| Interaction | **UI atomique** (boutons + panneaux), **pas de chat** — le chat agentique de Refonte a été désactivé faute de valeur (PR #170) |
| Où vit la taxonomie proposée | **Profil brouillon créé tôt** + raffinage via l'**onglet Mappings existant** (zéro éditeur de YAMLs en double) |
| Architecture | **Pipeline mince réutilisant l'existant**, pas un agent ReAct conversationnel ; l'« intelligence » LLM se concentre sur 2 appels (proposition de hiérarchie + mots-clés) |
| Source de l'analyse | **Vision sur tout le corpus** (pas d'échantillon) + **dry-run complet** |
| Sorties proposées | **3 YAMLs** : `tree.yaml` + `theme_mapping.yaml` (P1) + `categories.yaml` (P2, mots-clés **ancrés sur le contenu** via `categories_llm`) |
| Reverse-engineering d'une pré-orga existante | **Détecté + signalé** seulement ; la proposition reste pilotée par le contenu. Reverse complet = v2 (YAGNI) |

## Les 3 étapes

> L'agent réalise **Scan & estimation** et **Analyse & proposition**. **Raffinage & application**
> réutilise l'UI Taxonomie + Apply existante.

### 1️⃣ Scan & estimation *(léger, lecture seule, pas encore de profil)*

Compte les fichiers du répertoire brut (volume, formats PDF/ePub/autres), détecte une éventuelle
**pré-organisation** (dossiers déjà présents — affichée pour information), et calcule l'**estimation
de coût** Vision (nb fichiers × coût/appel du modèle). Sortie : un court récap + l'estimation. C'est
le **go/no-go** avant de dépenser. Aucune écriture, aucun appel LLM.

### 2️⃣ Analyse & proposition *(le cœur — tout le pipeline tourne)*

1. **Crée le profil brouillon** : `lib.profile.init_profile(name, target)` (skeleton vide) + flag
   `onboarding_draft: true` dans `profile.yaml`.
2. **Vision LLM sur TOUT le corpus** → peuple `profiles/<nom>/.cache/vision_cache.json` (via
   `lib.vision.analyze_cover`, même pipeline que `klodo process` sans déplacement). Opération longue :
   thread daemon + barre de progression pollée (patron `agent_refonte`). **Interruptible et
   reprenable** — la Vision est incrémentale (le cache mémorise les fichiers déjà analysés).
3. **Clusterise tous les thèmes** : réutilise `lib.theme_canon` (`extract_themes_from_vision_cache`
   + clustering syntactic/sémantique) sur le cache complet.
4. **Propose la hiérarchie (1 appel LLM — nouveau)** : à partir des clusters (avec compteurs + titres
   d'exemple), le LLM propose une **arborescence ≤ 2 niveaux** + l'assignation de chaque cluster à un
   dossier → génère `tree.yaml` + `theme_mapping.yaml` (chaque thème observé → son dossier).
5. **Propose les mots-clés** : réutilise `agents/refonte/categories_llm.propose_keywords_for_new_folders`
   en lui passant les dossiers proposés comme « creations » + des **titres d'exemple réels par
   dossier** (issus de la Vision) → `categories.yaml` ancré sur le contenu.
6. **Écrit les 3 YAMLs** dans le profil brouillon (atomique, après backup `agent_backup`).
7. **Dry-run complet** : `dashboard.taxonomy.reclassify_dryrun(profile)` sur le cache complet contre
   la config écrite → **rapport de couverture** : n mappés / n « Autres » / n erreurs, distribution
   par destination.

### 3️⃣ Raffinage & application *(outils existants — handoff)*

Bascule vers **Taxonomie → Mappings** (profil brouillon actif, bandeau « en cours d'onboarding »).
L'utilisateur raffine avec ce qui existe déjà : éditeur d'arbre (créer/renommer/déplacer), **adopter**
les dossiers hors-config (#178), mappings en lot (#175), **« Voir ce qui bougerait »** (le dry-run de
couverture en direct). Quand la couverture convient, **Apply global** (#177) déplace réellement les
fichiers. **« Finaliser »** retire le flag brouillon (ou implicite après le 1ᵉʳ apply réussi).

## Architecture

### Le pipeline `agents/onboarding/`

Module calqué sur `agents/refonte/` mais **pipeline-shaped** (orchestration déterministe + appels LLM
ciblés), **pas** un agent ReAct/conversationnel :

- `state.py` — état typé `OnboardingState` (inbox_path, profile_name, étape courante, stats, chemins).
- `scan.py` — **Scan & estimation** : compte fichiers/formats, détecte pré-orga, estime le coût.
- `proposition.py` — **Analyse & proposition** : orchestre Vision → clustering → proposition
  hiérarchie (LLM) → categories (`categories_llm`) → écriture des 3 YAMLs → dry-run.
- `tools.py` — l'appel LLM « propose une hiérarchie depuis les clusters » + prompt + parsing Pydantic
  (sortie structurée, garde anti-hallucination : tout dossier proposé doit être cohérent).
- `__init__.py` — surface publique appelée par le wrapper dashboard.

Wrapper dashboard : `dashboard/agent_onboarding.py` (thread daemon + `status.json` polling, patron
`agent_refonte.py`).

### Le profil brouillon

`init_profile` crée un skeleton vide ; l'onboarding y écrit ensuite les YAMLs proposés. Le profil est
**réel** dès l'étape 2️⃣ (pour réutiliser l'éditeur Mappings sans dupliquer d'UI), marqué
`onboarding_draft: true`. État géré dans `profiles/<nom>/.cache/onboarding/state.json` +
`status.json` (progression Vision). Journal des actions agent dans `.cache/agent-journal.jsonl`
(réutilise `agent_journal`).

## Réutilisation exacte

**Réutilisé tel quel** :

- `lib.vision.analyze_cover` + `lib.vision_cache` — Vision + cache.
- `lib.theme_canon` — clustering des thèmes.
- `agents/refonte/categories_llm.propose_keywords_for_new_folders` — génération `categories.yaml`.
- `dashboard.taxonomy.reclassify_dryrun` (+ `classify_combined`) — couverture / dry-run.
- `lib.profile.init_profile` — skeleton du profil.
- l'**éditeur Taxonomie/Mappings + adopt** (#178), **mappings en lot** (#175), **Apply global** (#177).
- `agents/refonte/agent_journal` + `agent_backup` — journal + backup (ancrés au profil brouillon).
- le patron dashboard thread daemon + polling `status.json` (`agent_refonte`).

**Neuf à écrire** :

- le pipeline `agents/onboarding/` (scan/estimation + l'appel LLM « propose une hiérarchie depuis les
  clusters » + son prompt).
- le concept de **profil brouillon** (flag `onboarding_draft`, badge sélecteur, bandeau Taxonomie,
  « Finaliser »).
- la **page assistant Onboarding** (stepper config → progression Vision → résumé proposition) +
  ~4 endpoints.
- l'estimateur de coût.
- les tests.

## Garde-fous & coût

- **Coût** : estimation affichée à l'étape 1️⃣ → **confirmation explicite** avant de lancer la Vision
  (étape 2️⃣) → **pas de cap dur** (l'utilisateur décide) mais **avertissement** au-delà d'un seuil
  (« ~$X, ~Y min, confirmer ? »). Vision **interruptible et reprenable** (cache incrémental).
- **Sécurité** : backup (`agent_backup`) avant d'écrire les 3 YAMLs (re-proposition réversible) ·
  journal append-only des actions agent · validation target (`os.path.isdir`, refus si target ==
  target d'un autre profil) · refus de nom de profil déjà existant (suffixe proposé) · écritures YAML
  atomiques (tmp + `os.rename`) + `yaml.safe_load` avant écriture.
- **Anti-hallucination** : chaque entrée `theme_mapping`/`categories` proposée doit pointer un dossier
  réellement présent dans le `tree.yaml` proposé (garde déjà dans `categories_llm`).

## État brouillon — cycle de vie

- `onboarding_draft: true` écrit dans `profile.yaml` à l'étape 2️⃣.
- Sélecteur de profil : badge **« brouillon »**.
- Onglet Taxonomie : **bandeau** « Profil en cours d'onboarding — N % couverts · Raffine puis
  applique · [Finaliser] ».
- **« Finaliser »** retire le flag (ou implicite après le 1ᵉʳ Apply global réussi).
- **Supprimable** à tout moment (abandon) — un brouillon orphelin ne pollue rien d'autre.

## UI

- **Point d'entrée** : entrée **« ➕ Nouveau profil »** dans le sélecteur de profil global → ouvre la
  page Onboarding. Seul nouvel emplacement de nav.
- **Page Onboarding** (stepper atomique, style du bloc apply de Refonte) :
  - **1️⃣ Configuration** : nom + répertoire + estimation de coût + bouton « Analyser le répertoire ».
  - **2️⃣ Analyse Vision** : barre de progression pollée (thread+poll), note « peuple le cache, aucun
    déplacement ».
  - **3️⃣ Taxonomie proposée** : compteurs (sections / mappings / **couverture %**) + distribution
    dry-run (mappés / Autres / erreurs) + aperçu d'arbre + bouton « Continuer dans Mappings → ».
- **Handoff** : bouton → onglet **Taxonomie/Mappings** du brouillon. Tout le raffinage et
  l'application sont l'existant. Maquette de référence : `docs/mockups/2026-06-14-onboarding-mockup.html`.

## Endpoints (`dashboard/app.py`, logique dans `dashboard/agent_onboarding.py`)

| Route | Effet |
| --- | --- |
| `POST /api/agent/onboarding/scan` `{inbox_path}` | Scan & estimation (compteurs + estimation coût + pré-orga détectée) |
| `POST /api/agent/onboarding/start` `{profile_name, inbox_path}` | Crée le brouillon + lance Vision + proposition + dry-run (thread) |
| `GET  /api/agent/onboarding/status?profile=` | Progression (Vision %, étape, couverture finale) |
| `POST /api/agent/onboarding/finalize` `{profile}` | Retire le flag `onboarding_draft` |

## Tests & métriques

- **Tests unitaires** par étape (Vision/LLM **mockés**, zéro SSD réel) : scan + estimation, clustering
  (réutilise les tests `theme_canon`), proposition hiérarchie (mock LLM → arbre + mapping cohérents),
  categories (réutilise `categories_llm`), création brouillon (skeleton + flag), couverture dry-run.
- **Smoke test** (règle projet) : 3-5 PDFs réels — Vision + proposition non vides avant tout run long.
- **Tests HTTP** des 4 endpoints (mock du thread synchrone, patron `_spawn` de l'apply).
- **Métriques cibles** (recalibrées) : profil utilisable après onboarding · couverture ≥ 70 % à la 1ʳᵉ
  passe · ≤ 30 % en « Autres » · coût recalculé sur le modèle réel (Qwen3-VL-32B) · 0 corruption de
  profil.

## Hors périmètre (YAGNI)

- **Renommage des fichiers** (c'est `klodo rename` après création).
- **Reverse-engineering complet** d'une arbo pré-existante (détecté + signalé seulement ; v2).
- **Chat conversationnel** (UI atomique actée).
- **Échantillonnage** comme base de proposition (analyse complète actée).
- **Multi-profils en parallèle** (un onboarding = un profil).
- **Éditeur UI de `categories.yaml`** (chantier *taxonomie followup* séparé ; premier jet LLM
  suffit ici, raffinage YAML manuel en attendant).

## Fichiers critiques

- `agents/onboarding/` — **nouveau** : `state.py`, `scan.py`, `proposition.py`, `tools.py`, `__init__.py`.
- `dashboard/agent_onboarding.py` — **nouveau** : wrapper thread+poll.
- `dashboard/app.py` — 4 routes `/api/agent/onboarding/*`.
- `lib/profile.py:init_profile` — réutilisé (skeleton) + ajout du flag `onboarding_draft`.
- `agents/refonte/categories_llm.py`, `lib/theme_canon.py`, `lib/vision.py`,
  `dashboard/taxonomy.py:reclassify_dryrun` — réutilisés.
- `dashboard/templates/` + `static/js/` — page assistant + badge brouillon + bandeau Taxonomie.
- `tests/auto/test_agent_onboarding.py` — **nouveau**.
