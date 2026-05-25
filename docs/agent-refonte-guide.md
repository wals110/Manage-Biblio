# Guide d'usage — Agent Refonte (Phase A)

[← Retour au README](../README.md) · [Spec complète](refonte-agent-spec.md) · [Autres agents](contributing.md#idées-de-contribution)

> Premier agent IA de Klodo, en lecture seule. Analyse la taxonomie d'un profil et produit un rapport markdown listant les anomalies (catch-all qui débordent, dossiers sous-utilisés, doublons sémantiques, mappings orphelins, couverture faible).
>
> **Statut : Phase A livrée.** Phases B (proposition de refonte) et C (dialog + mutations) à venir — cf. [spec](refonte-agent-spec.md).

## Pré-requis

- `SILICONFLOW_API_KEY` valide dans `.env` (compte sur https://cloud.siliconflow.com/)
- Le profil cible existe avec `tree.yaml` + `theme_mapping.yaml` au minimum

## Lancer un diagnostic

### Via le dashboard (recommandé)

```bash
./klodo.sh dashboard          # port 8080 par défaut
```

Puis dans le navigateur :

1. Aller sur **Taxonomie**
2. Cliquer sur le sous-onglet **🤖 Refonte**
3. Sélectionner le profil dans le header Taxonomie
4. Ajuster le **Budget LLM** si besoin (default 5 appels max)
5. Cliquer **🚀 Lancer le diagnostic**
6. La page poll automatiquement toutes les 2s — quand `status: done`, le rapport apparaît à droite

Les runs précédents s'affichent dans la colonne de gauche, cliquables pour réafficher leur rapport.

> **URL directe** : `/agent/refonte?profile=<name>` (même panel, hors Taxonomie)

### Via le script smoke test (debug)

Utile pour itérer sur les prompts ou tester un nouveau profil sans passer par l'UI :

```bash
uv run python scripts/agent_refonte_smoke.py default     # budget default 5
uv run python scripts/agent_refonte_smoke.py test 3      # budget custom
```

Affiche en sortie : status, llm_calls, durée, tool_calls/tool_results, rapport markdown complet.

## Coût et durée typiques

| Profil | Fichiers | Durée | LLM calls | Coût SiliconFlow |
|---|---|---|---|---|
| `test` (30 fichiers) | < 50 | ~30s | 3-5 | < $0.01 |
| `default` (réel) | ~18 250 | ~75s | 5-7 | ~$0.01-0.05 |

Modèle utilisé : **DeepSeek V3.2-Exp** (`deepseek-ai/DeepSeek-V3.2-Exp` sur SiliconFlow). Surchargeable :

```bash
export KLODO_AGENT_MODEL="zai-org/GLM-4.6"   # alternative
```

## Interpréter le rapport

Le rapport markdown contient 4 sections :

### 1. Stats globales
Métriques d'ensemble : nombre de dossiers, mappings, dossiers les plus peuplés.

### 2. Anomalies détectées (par priorité)

Catégories standard :

- **Catch-all qui débordent** : dossiers `/Autres` ou `/Generales` avec count anormalement haut → candidats à scission (cf. trigger N3 PR #147 qui les détecte aussi côté classify)
- **Dossiers sous-utilisés** : < 5 fichiers → candidats à fusion ou suppression
- **Doublons sémantiques** : indice de Jaccard > 0.3 sur les thèmes mappés vers deux dossiers
- **Mappings orphelins** : thème pointe vers un dossier absent de `tree.yaml`
- **Couverture faible** : trop de `FAILED` dans le dernier `classify_*.csv` (> 10%)

Si une catégorie n'a pas été analysée (par exemple budget LLM épuisé avant qu'elle ne soit explorée), l'agent l'indique explicitement par `(non analysé)` — pas de fabrication de chiffres.

### 3. Recommandations
3 à 5 actions concrètes au format impératif ("Scinder X", "Fusionner A et B", etc.) avec un effort estimé (faible / moyen / élevé).

## Exemple de rapport produit

Extrait d'un run sur le profil `default` (18 250 fichiers, 5 appels LLM, 75s, ~$0.02) :

```markdown
# Diagnostic taxonomy — profil `default` — 2026-05-24

## Stats globales
- 113 dossiers dans l'arborescence
- 387 mappings thème → dossier
- Dossier le plus peuplé : `02-INFORMATIQUE/03-Langages-Programmation/Autres` (4 489 fichiers)
- 18 dossiers avec 0 fichier, 15 avec 1-5 fichiers

## Anomalies détectées (par priorité)

### Catch-all qui débordent (7 cas critiques)
1. `02-INFORMATIQUE/03-Langages-Programmation/Autres` — 4 489 fichiers
2. `01-SCIENCES/MATHEMATIQUES/08-Mathematiques-Generales` — 2 659 fichiers
3. `02-INFORMATIQUE/01-Fondamentaux-CS` — 1 285 fichiers
...

### Dossiers sous-utilisés (18 cas)
1. `02-INFORMATIQUE/03-Langages-Programmation/JavaScript` — 0 fichier
2. `07-LANGUES/AUTRES` — 0 fichier
...

## Recommandations
1. **Scinder le catch-all `.../Autres`** en sous-catégories (Go, Haskell, Kotlin, etc.) — Effort : élevé
2. **Fusionner les dossiers de langages vides** (JavaScript, Python, Java) — Effort : faible
3. **Exécuter une classification complète** pour métriques de couverture — Effort : moyen
```

## Limitations connues (Phase A)

- **Lecture seule** — aucune mutation YAML, FS, ou cache. Pour appliquer les recommandations, faire les changements à la main dans l'onglet Mappings de Taxonomie. (Phase C, à venir, fera ces mutations conversationnellement.)
- **Budget borné** — par défaut 5 appels LLM. Si le profil est très complexe, certaines catégories peuvent être marquées "(non analysé)". Augmenter le budget via le champ "Budget LLM" dans l'UI.
- **Date hallucinée si non injectée** — l'agent recevait la date via prompt explicite (fix livré en A.5). Tout LLM, sans contexte, hallucine "aujourd'hui".
- **Pas de streaming** — le rapport apparaît d'un bloc à la fin. Le polling tourne à 2s.
- **Pas de persistance long terme** — les runs sont gardés dans `profile/<name>/.cache/refonte/<run_id>/`. Pas de rotation automatique, à nettoyer manuellement si nécessaire.

## Architecture interne (résumé)

- **Graphe LangGraph** : `START → init → [explore ↔ tools]* → write_report → END`
- **6 outils read-only** : `list_folders`, `count_files_per_folder`, `read_theme_mapping`, `list_themes_per_folder`, `compute_folder_overlap`, `get_classifier_breakdown`
- **Stack** : `langgraph` + `langchain-openai` (compatible OpenAI client → SiliconFlow)
- **Storage** : `profile/<name>/.cache/refonte/<run_id>/{status.json, report.md}`

Pour les détails complets : [spec](refonte-agent-spec.md) · [diagramme des phases](diagrams/agent-refonte-phases.svg)

## Troubleshooting

| Symptôme | Cause probable | Fix |
|---|---|---|
| `RuntimeError: SILICONFLOW_API_KEY is required` | clé absente de `.env` ou de l'env shell | Ajouter `SILICONFLOW_API_KEY=sk-...` dans `.env` (régénérer sur cloud.siliconflow.com si périmée) |
| `AuthenticationError 401: Api key is invalid` | Clé périmée, révoquée, ou (rare) endpoint cn vs com | Régénérer la clé. Vérifier que `agents/llm.py` pointe sur `api.siliconflow.com` (pas `.cn`) |
| Status `error` avec timeout LLM | SiliconFlow lent ou surchargé | Réessayer. Si persistant, essayer `KLODO_AGENT_MODEL=zai-org/GLM-4.6` |
| Rapport markdown qui répète le prompt | Bug de l'A.5 (corrigé) — le nettoyage `# Diagnostic` strip tout préambule | Si encore présent : ouvrir un issue |
| Rapport avec une date passée | Bug si `date` n'est plus injectée dans le prompt | Vérifier `_make_write_report_node` dans `agents/refonte/diagnostic.py` |

## Suite

- **Phase B** (Proposition) — génère `tree-proposed.yaml` + `theme_mapping-proposed.yaml` + simulation reclassify
- **Phase C** (Dialog) — conversationnelle, peut appliquer les changements validés sur les YAML de production avec backup + journal

Cf. [refonte-agent-spec.md](refonte-agent-spec.md) pour le détail.

## Backlog

Items techniques discutés mais reportés.

### A.7 — Provider LLM alternatif `claude -p` (CLI Claude Code)

**Idée** : ajouter une option `KLODO_AGENT_PROVIDER=claude-code-cli` qui ferait passer l'agent par `claude -p --output-format stream-json` (subprocess) au lieu de SiliconFlow. Permettrait d'utiliser le quota d'un abonnement Claude Code (coût marginal $0) à la place du pay-as-you-go SiliconFlow (~$0.02/run).

**Pourquoi reporté** :

- L'agent marche déjà à ~$0.02/run sur DeepSeek V3.2 — gain marginal vs ~1-2 j-h d'effort dev
- Risque de fragilité (parsing stream-json) et de quota (run 18k fichiers pourrait épuiser le quota Claude Code de la journée)
- Phase B/C bénéficierait davantage de Claude (raisonnement plus complexe, dialog multi-tour) — investir dans le wrapper à ce moment-là est plus rentable

**À revisiter** : au démarrage de Phase B. Implémentation envisagée :

- Custom ChatModel subclasse de `BaseChatModel`
- `subprocess.run(['claude', '-p', '--output-format', 'stream-json', ...])` à chaque appel
- `bind_tools()` qui injecte les définitions d'outils dans le system prompt
- Parsing du stream-json pour extraire content + tool_use blocks
- Alternative pay-as-you-go simple : `langchain-anthropic` direct (~$0.15/run avec Sonnet 4.6)
