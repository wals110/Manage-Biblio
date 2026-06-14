# agents/ — Agents IA Klodo

Module hébergeant les agents IA, séparé de `lib/` (cœur CLI). Chaque agent a un
wrapper côté `dashboard/` qui expose ses routes et son UI.

## Factory LLM partagée

- **[llm.py](llm.py)** — `get_agent_llm(model=None, *, temperature=0.1, ...) -> ChatOpenAI`.
  Pointe par défaut sur **SiliconFlow** (`api.siliconflow.com`, **jamais** `.cn`)
  avec **GLM-4.7** (`zai-org/GLM-4.7`), `streaming=True`. Surchargeable via
  `KLODO_AGENT_MODEL`. **Lève `RuntimeError` sans `SILICONFLOW_API_KEY`** → en
  test, toujours mocker `get_agent_llm` (jamais d'appel LLM réel).

## Agent Refonte (`agents/refonte/`)

Réorganise une taxonomie existante. Graphe **LangGraph**, 3 phases nommées :

- **Diagnostic** (`diagnostic.py`) — read-only, rapport markdown des anomalies.
- **Proposition** (`proposition.py` + `simulator.py` + `tools.py` +
  `proposition_tools.py` + `categories_llm.py`) — génère `tree-proposed.yaml`,
  simule un reclassify, produit un diff d'arbre.
- **Dialog/Mutations** (`dialog.py` + `mutations.py`) — agent conversationnel
  (UI désactivée depuis 2026-06-07, code conservé).
- Support : `agent_journal.py` (JSONL append-only des mutations + undo),
  `agent_backup.py` (snapshots `tree.yaml + theme_mapping.yaml` avant chaque
  batch, rotation, `BackupError` si rien à snapshoter), `state.py` (schémas
  Pydantic du state graph).
- Wrappers dashboard : `dashboard/agent_refonte.py`, `agent_refonte_phase_c.py`,
  `agent_refonte_apply.py`. Routes `/api/agent/refonte/*`.

## Agent Onboarding (`agents/onboarding/`)

Bootstrap d'un **nouveau** profil depuis un répertoire brut. **Pipeline**
(fonctions pures + 2 appels LLM directs `with_structured_output(...).invoke(...)`),
**pas** un agent ReAct/LangGraph. 3 étapes nommées : **Scan & estimation** →
**Analyse & proposition** → **Raffinage & application**.

- **`scan.py`** — `scan_directory` (compte pdf/epub, détecte une pré-organisation)
  + `estimate_cost` (coût/ETA Vision). Read-only, aucun LLM.
- **`taxonomy_llm.py`** — `propose_taxonomy(llm, clusters)` : 1 appel LLM qui mappe
  les clusters de thèmes vers une hiérarchie de dossiers. Les **sections** viennent
  du contenu ; la **forme** est imposée (≤ 2 niveaux, 1er niveau MAJUSCULES, sous-
  dossiers TitleCase, `_A-TRIER` résiduel, `_INBOX` réservé, segments FS-safe).
- **`proposition.py`** — orchestration : `run_vision` (Vision full-corpus,
  reprenable via `vision_cache`) → `cluster_corpus` (via `lib/theme_canon` +
  `theme_normalizer`) → `propose_taxonomy` → `propose_categories` (réutilise
  `agents/refonte/categories_llm`) → `write_proposal` (écrit les 3 YAMLs après
  backup + dry-run de couverture via `dashboard/taxonomy.reclassify_dryrun`).
  `build_proposal` enchaîne tout le pipeline.
- Wrapper dashboard : `dashboard/agent_onboarding.py` (scan synchrone, start en
  thread daemon, status.json, finalize). Routes `/api/agent/onboarding/*`, page
  `/onboarding`. Profil **brouillon** (`onboarding_draft: true`) jusqu'à
  finalisation. L'étape « Raffinage & application » = handoff vers l'onglet
  Mappings + Apply global existants (aucun code neuf).

## Conventions

- Réutiliser la factory `get_agent_llm` (pas de client LLM ad hoc).
- `sanitize_for_prompt` (lib/utils) avant d'interpoler du texte non fiable dans
  un prompt.
- Tests : **jamais** de Vision/LLM réel — mocker `analyze_cover`, `get_agent_llm`,
  `propose_keywords_for_new_folders`, `propose_taxonomy`, et `_spawn` (exécution
  synchrone des wrappers thread).
