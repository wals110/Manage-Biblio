# tests/functional/ — Tests fonctionnels Klodo

**123 checks** répartis en **11 phases, 45 séries**.

## Artefacts
- **[tests.yaml](tests.yaml)** — Source de vérité YAML (à éditer directement)
- **[CAHIER_TESTS_FONCTIONNELS.md](CAHIER_TESTS_FONCTIONNELS.md)** — Vue Markdown générée
- **[run_functional.py](run_functional.py)** — Runner (code maintenu manuellement)
- **[db.py](db.py)** — Backend DuckDB
- **[reports/](reports/)** — Rapports JSON + Markdown

## Runner
```bash
uv run python tests/functional/run_functional.py [options]
  --interactive       Mode interactif
  --phase N           Une seule phase
  --dry-run           Sans exécution
  --rerun-failures    Relance seulement FAIL + SKIP du dernier rapport
  --label "nom"       Label personnalisé du run
```

Le runner capture stdout du setup dans `logs/.setup_output_<series>.txt` pour les checks qui grep la sortie.

## Pièges critiques
- **Clean logs interdit en pre_run** : ne JAMAIS `clean all` / `clean logs` dans les pre_run — utiliser **`clean progress` uniquement**
- **Nommage des rapports** : `rapport_rename_*.csv`, `rapport_process_*.csv`, `refine_*.csv`. **Pas** `rapport_classify_*` quand lancé via `process`
- **Remise à zéro du dataset** : `./scripts/flatten_to_inbox.sh BIBLIO-TEST --execute` (demande confirmation → piper `echo oui |`). Le script appelle automatiquement `scripts/restore_original_names.py` qui parcourt récursivement `logs/log_renommage_*.csv` et `tests/functional/logs/**` pour restaurer les noms d'origine **avant** le déplacement vers `_INBOX/`
- **Pièges shell des checks** : `grep -c` retourne exit 1 sur 0 match → toujours utiliser `C=$(grep -c X "$F" 2>/dev/null || true); echo "${C:-0}"` pour éviter le double `0` ou le crash. Commandes non-bloquantes, assertions strictes
- **Idempotence** : les tests d'idempotence (T1.5) doivent lancer **2 passages** dans `setup` pour pouvoir vérifier que le 2e ne touche à rien
- **Pas de `--max N` dans les commandes** : c'est l'utilisateur qui contrôle la taille du dataset via le contenu de `_INBOX` (cf. onglet Curation du dashboard)
- **Pas de `${INBOX}` explicite** dans les commandes `klodo rename/classify/process` : le profil fournit `inbox` automatiquement via `args.path or profile.inbox`
- **Name patterns** : les profils ont des `rename.name_patterns` (regex) qui valident le format des noms après wordcheck. Cf. `profiles/test/profile.yaml`

## Skills associés (globaux, `~/.claude/skills/`)
- **functional-test-plan** — Génère/met à jour `tests.yaml` (2 modes : `generate` from scratch, `update` incrémental préservant les IDs)
- **test-issues-sync** — Sync avec GitHub Projects (1 issue par série, 40 issues sur le board "Tests Fonctionnels - Klodo")
- **functional-testing** — Orchestrateur

Note : le runner `run_functional.py` est du code maintenu manuellement (pas de skill runner).

Design spec : [../../docs/superpowers/specs/2026-04-05-functional-testing-skills-design.md](../../docs/superpowers/specs/2026-04-05-functional-testing-skills-design.md)
