#!/usr/bin/env python3
"""Tests pour Phase B (Proposition) — tool propose_changes + graphe.

3 axes :
  1. propose_changes : applique correctement créations/fusions/renommages/mappings
  2. Graph compile + invoke avec FakeLLM, propose_changes appelé une fois → done
  3. Refus d'inputs invalides (profile vide, diagnostic_run_id absent, diagnostic non-done)
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml
from langchain_core.messages import AIMessage, ToolMessage

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from agents.refonte.proposition import (  # noqa: E402
    build_proposition_graph,
    parse_catchall_folders_from_report,
    parse_orphan_mappings_from_report,
    parse_underutilized_folders_from_report,
)
from agents.refonte.proposition_tools import propose_changes  # noqa: E402
from dashboard import data  # noqa: E402
from dashboard import taxonomy as tax

# ─── Fixtures ──────────────────────────────────────────────────────────────


def _make_profile(profiles_root: Path, name: str, *, target: Path,
                  folders: list[str], mapping: dict[str, str]) -> None:
    pdir = profiles_root / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "profile.yaml").write_text(yaml.safe_dump({"name": name, "target": str(target)}))
    (pdir / "tree.yaml").write_text(yaml.safe_dump({"folders": folders}))
    (pdir / "theme_mapping.yaml").write_text(yaml.safe_dump(mapping))
    target.mkdir(parents=True, exist_ok=True)


def _seed_diagnostic_run(profiles_root: Path, profile: str, run_id: str,
                         status: str = "done",
                         report_md: str | None = None) -> None:
    """Crée un faux run de Phase A done (avec report.md minimal)."""
    rundir = profiles_root / profile / ".cache" / "refonte" / run_id
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "status.json").write_text(json.dumps({
        "run_id": run_id, "profile": profile, "status": status, "phase": "A",
        "started_at": "2026-05-25T00:00:00+00:00",
        "completed_at": "2026-05-25T00:01:00+00:00",
        "llm_calls": 7,
    }))
    (rundir / "report.md").write_text(report_md or (
        "# Diagnostic taxonomy — profil `" + profile + "` — 2026-05-25\n\n"
        "## Stats globales\n- 3 dossiers\n\n"
        "## Anomalies détectées (par priorité)\n\n"
        "### Mappings manquants critiques (1 cas)\n"
        "- **Rust** (50 fichiers) → dossier cible évident : `A/Rust`\n"
    ))


class _FakeLLM:
    """LLM scripté retournant une séquence prédéfinie d'AIMessage."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.invocations: list[list] = []

    def bind_tools(self, _tools):
        return self

    def invoke(self, messages):
        self.invocations.append(list(messages))
        if not self.responses:
            return AIMessage(content="(fake LLM: no more)")
        return self.responses.pop(0)


def _ai_tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": "call_" + name, "type": "tool_call"}],
    )


class _ProposBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_phaseb_"))
        self.profiles_root = self.tmp / "profiles"
        self.profiles_root.mkdir()
        self.target = self.tmp / "biblio"
        self._patcher = mock.patch.object(data, "get_project_root", return_value=self.tmp)
        self._patcher.start()
        tax.reset_cache()
        _make_profile(
            self.profiles_root, "p",
            target=self.target,
            folders=["A", "A/Python", "B"],
            mapping={"python programming": "A/Python", "data": "B"},
        )

    def tearDown(self):
        self._patcher.stop()
        tax.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ─── 1. propose_changes : sémantique ───────────────────────────────────────


class TestProposeChanges(_ProposBase):
    def test_writes_three_artifacts(self):
        result = propose_changes(
            profile="p",
            run_id="r-1",
            creations=[{"path": "A/Rust", "rationale": "Rust orphelin"}],
            mappings_added=[
                {"theme": "rust", "folder": "A/Rust", "rationale": "thème orphelin"},
            ],
        )
        # Les 3 fichiers existent
        for key in ("tree_proposed_path", "mapping_proposed_path", "rationale_path"):
            self.assertTrue(Path(result[key]).exists(), f"{key} missing on disk")
        # Compte cohérent
        self.assertEqual(result["n_creations"], 1)
        self.assertEqual(result["n_mappings_added"], 1)

    def test_creations_added_to_tree(self):
        propose_changes(
            profile="p", run_id="r-2",
            creations=[{"path": "A/Rust", "rationale": "x"}],
        )
        tree_path = self.tmp / "profiles" / "p" / ".cache" / "refonte" / "r-2" / "proposed" / "tree-proposed.yaml"
        folders = yaml.safe_load(tree_path.read_text())["folders"]
        self.assertIn("A/Rust", folders)
        self.assertIn("A", folders)  # existant préservé

    def test_renaming_replaces_in_tree_and_mapping(self):
        propose_changes(
            profile="p", run_id="r-3",
            renamings=[{"old_path": "A/Python", "new_path": "A/Python3", "rationale": "x"}],
        )
        rundir = self.tmp / "profiles" / "p" / ".cache" / "refonte" / "r-3" / "proposed"
        folders = yaml.safe_load((rundir / "tree-proposed.yaml").read_text())["folders"]
        mapping = yaml.safe_load((rundir / "theme_mapping-proposed.yaml").read_text())
        self.assertIn("A/Python3", folders)
        self.assertNotIn("A/Python", folders)
        # Le mapping existant qui pointait sur A/Python doit suivre
        self.assertEqual(mapping["python programming"], "A/Python3")

    def test_fusion_consolidates_in_tree_and_mapping(self):
        propose_changes(
            profile="p", run_id="r-4",
            fusions=[
                {"sources": ["A", "B"], "target": "ALL", "rationale": "tout fusionné"},
            ],
        )
        rundir = self.tmp / "profiles" / "p" / ".cache" / "refonte" / "r-4" / "proposed"
        folders = yaml.safe_load((rundir / "tree-proposed.yaml").read_text())["folders"]
        mapping = yaml.safe_load((rundir / "theme_mapping-proposed.yaml").read_text())
        self.assertIn("ALL", folders)
        self.assertNotIn("A", folders)
        self.assertNotIn("B", folders)
        # Les mappings sur A et B doivent pointer sur ALL
        # (sauf "python programming" qui pointait sur A/Python qui n'est pas dans les sources)
        self.assertEqual(mapping["data"], "ALL")

    def test_rationale_md_has_sections(self):
        propose_changes(
            profile="p", run_id="r-5",
            creations=[{"path": "X", "rationale": "x"}],
            fusions=[{"sources": ["A"], "target": "X", "rationale": "y"}],
            renamings=[{"old_path": "B", "new_path": "C", "rationale": "z"}],
            mappings_added=[{"theme": "t1", "folder": "X", "rationale": "w"}],
        )
        rationale = (self.tmp / "profiles" / "p" / ".cache" / "refonte" / "r-5"
                     / "proposed" / "refonte-rationale.md").read_text()
        self.assertIn("CRÉATIONS (1)", rationale)
        self.assertIn("FUSIONS (1)", rationale)
        self.assertIn("RENOMMAGES (1)", rationale)
        self.assertIn("MAPPINGS AJOUTÉS (1)", rationale)


# ─── 2. Graphe Phase B avec FakeLLM ────────────────────────────────────────


class TestPropositionGraph(_ProposBase):
    def test_happy_path_calls_propose_then_finalizes(self):
        _seed_diagnostic_run(self.profiles_root, "p", "diag-1")
        fake = _FakeLLM([
            # 1er analyze : appelle propose_changes
            _ai_tool_call("propose_changes", {
                "creations": [{"path": "A/Rust", "rationale": "thème orphelin"}],
                "mappings_added": [
                    {"theme": "rust", "folder": "A/Rust", "rationale": "cf diagnostic"},
                ],
            }),
            # 2e analyze : LLM termine sans tool_call
            AIMessage(content="Proposition envoyée."),
        ])
        graph = build_proposition_graph(profile="p", run_id="b-1", llm=fake, max_llm_calls=5)
        result = graph.invoke({
            "profile": "p", "run_id": "b-1", "diagnostic_run_id": "diag-1",
        })
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["proposal_summary"]["n_creations"], 1)
        self.assertEqual(result["proposal_summary"]["n_mappings_added"], 1)
        # Le proposal_dir contient bien les 3 artefacts
        pdir = Path(result["proposal_dir"])
        for fname in ("tree-proposed.yaml", "theme_mapping-proposed.yaml",
                      "refonte-rationale.md"):
            self.assertTrue((pdir / fname).exists())
        # ToolMessage retourné dans l'historique
        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0].name, "propose_changes")

    def test_no_propose_call_returns_error(self):
        """Si l'agent termine sans jamais appeler propose_changes → status=error."""
        _seed_diagnostic_run(self.profiles_root, "p", "diag-2")
        fake = _FakeLLM([
            AIMessage(content="Je n'ai rien à proposer."),  # pas de tool_call
        ])
        graph = build_proposition_graph(profile="p", run_id="b-2", llm=fake, max_llm_calls=5)
        result = graph.invoke({
            "profile": "p", "run_id": "b-2", "diagnostic_run_id": "diag-2",
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("propose_changes", result["error"])

    def test_missing_profile_short_circuits(self):
        fake = _FakeLLM([])
        graph = build_proposition_graph(profile="p", run_id="b-3", llm=fake, max_llm_calls=5)
        result = graph.invoke({"run_id": "b-3", "diagnostic_run_id": "anything"})
        self.assertEqual(result["status"], "error")
        self.assertIn("profile", result["error"].lower())
        self.assertEqual(len(fake.invocations), 0)

    def test_missing_diagnostic_run_id_short_circuits(self):
        fake = _FakeLLM([])
        graph = build_proposition_graph(profile="p", run_id="b-4", llm=fake, max_llm_calls=5)
        result = graph.invoke({"profile": "p", "run_id": "b-4"})
        self.assertEqual(result["status"], "error")
        self.assertIn("diagnostic_run_id", result["error"])

    def test_unknown_diagnostic_returns_error(self):
        fake = _FakeLLM([])
        graph = build_proposition_graph(profile="p", run_id="b-5", llm=fake, max_llm_calls=5)
        result = graph.invoke({
            "profile": "p", "run_id": "b-5", "diagnostic_run_id": "ghost-id",
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("Rapport de diagnostic introuvable", result["error"])


# ─── 3. Backend dashboard start_proposition ────────────────────────────────


class TestStartProposition(_ProposBase):
    def test_rejects_missing_profile(self):
        from dashboard import agent_refonte
        with self.assertRaises(ValueError) as ctx:
            agent_refonte.start_proposition("", "any-id")
        self.assertIn("profile", str(ctx.exception).lower())

    def test_rejects_unknown_diagnostic(self):
        from dashboard import agent_refonte
        with self.assertRaises(FileNotFoundError) as ctx:
            agent_refonte.start_proposition("p", "ghost-run")
        self.assertIn("diagnostic run not found", str(ctx.exception).lower())

    def test_rejects_diagnostic_not_done(self):
        from dashboard import agent_refonte
        _seed_diagnostic_run(self.profiles_root, "p", "diag-pending", status="pending")
        with self.assertRaises(ValueError) as ctx:
            agent_refonte.start_proposition("p", "diag-pending")
        self.assertIn("'pending'", str(ctx.exception))

    def test_happy_path_returns_run_id_and_thread_runs(self):
        """Avec un graphe mocké, start_proposition retourne et le thread écrit."""
        from dashboard import agent_refonte
        _seed_diagnostic_run(self.profiles_root, "p", "diag-3")

        # Fake graph used by _run_proposition via import
        class _FakeGraph:
            def stream(self, state, stream_mode="values"):
                yield {**state, "llm_calls": 0}
                yield {
                    **state,
                    "llm_calls": 2,
                    "status": "done",
                    "proposal_dir": "/tmp/fake",
                    "proposal_summary": {"n_creations": 1, "n_mappings_added": 0},
                }

        with mock.patch(
            "agents.refonte.proposition.build_proposition_graph",
            return_value=_FakeGraph(),
        ):
            result = agent_refonte.start_proposition("p", "diag-3")
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["phase"], "B")
            self.assertEqual(result["diagnostic_run_id"], "diag-3")
            self.assertTrue(result["run_id"])
            # Attendre que le thread écrive le status final
            time.sleep(0.4)
            status = agent_refonte.get_status("p", result["run_id"])
            self.assertEqual(status["status"], "done")
            self.assertEqual(status["proposal_summary"]["n_creations"], 1)


class TestParseOrphanMappings(unittest.TestCase):
    """Parser d'orphan mappings depuis le rapport Phase A — alimente
    le HumanMessage de Phase B avec un JSON pré-extrait."""

    def test_parses_standard_format(self):
        md = """
# Diagnostic taxonomy — profil `default` — 2026-05-25

## Anomalies détectées (par priorité)

### Mappings manquants critiques (30 cas)
- **Functional Analysis** (176 fichiers) → dossier cible évident : `01-SCIENCES/MATHEMATIQUES/02-Analyse`
- **Complex Analysis** (114 fichiers) → dossier cible évident : `01-SCIENCES/MATHEMATIQUES/02-Analyse`
- **Pattern Recognition** (114 fichiers) → dossier cible évident : `02-INFORMATIQUE/05-IA-ML/Machine-Learning`
"""
        result = parse_orphan_mappings_from_report(md)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["theme"], "Functional Analysis")
        self.assertEqual(result[0]["count"], 176)
        self.assertEqual(result[0]["target_folder"], "01-SCIENCES/MATHEMATIQUES/02-Analyse")
        self.assertEqual(result[2]["theme"], "Pattern Recognition")
        self.assertEqual(result[2]["target_folder"], "02-INFORMATIQUE/05-IA-ML/Machine-Learning")

    def test_handles_variants_in_separators(self):
        """Le parser tolère 'dossier cible :' au lieu de 'dossier cible évident :'."""
        md = (
            "- **Theme1** (50 fichiers) → dossier cible : `A/B`\n"
            "- **Theme2** (10 fichiers) → dossier cible évident : `C/D`\n"
            # Lignes sans count ne matchent pas
            "- **Theme3** truc sans count\n"
        )
        result = parse_orphan_mappings_from_report(md)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["theme"], "Theme1")
        self.assertEqual(result[1]["theme"], "Theme2")

    def test_strips_backticks_from_target(self):
        md = "- **X** (5 fichiers) → dossier cible évident : `A/B/C`"
        result = parse_orphan_mappings_from_report(md)
        self.assertEqual(result[0]["target_folder"], "A/B/C")

    def test_works_without_backticks(self):
        md = "- **X** (5 fichiers) → dossier cible évident : A/B/C"
        result = parse_orphan_mappings_from_report(md)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["target_folder"], "A/B/C")

    def test_returns_empty_on_no_match(self):
        self.assertEqual(parse_orphan_mappings_from_report(""), [])
        self.assertEqual(parse_orphan_mappings_from_report("rapport vide sans liste"), [])

    def test_picks_only_well_formed_lines(self):
        """Lignes mal formées (manque arrow, manque count, etc.) sont ignorées."""
        md = (
            "Cette ligne **Theme** n'a pas d'arrow.\n"
            "- **Good** (100 fichiers) → cible : `Path/A`\n"
            "Un texte sans bullet ni format **Bad** → essayer.\n"
        )
        result = parse_orphan_mappings_from_report(md)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["theme"], "Good")

    def test_parses_30_realistic_orphans(self):
        """Sanity check sur un rapport simulé proche du vrai format de prod."""
        lines = ["### Mappings manquants critiques (30 cas)"]
        for i, (theme, count) in enumerate([
            ("Functional Analysis", 176), ("Complex Analysis", 114),
            ("Pattern Recognition", 114), ("Group Theory", 112),
            ("Real Analysis", 93), ("Materials Science", 78),
            ("Optimization", 76), ("Penetration Testing", 73),
            ("Combinatorics", 69), ("Software Testing", 69),
        ]):
            lines.append(
                f"- **{theme}** ({count} fichiers) → dossier cible évident : `01-SCIENCES/FOO`"
            )
        result = parse_orphan_mappings_from_report("\n".join(lines))
        self.assertEqual(len(result), 10)
        # Ordre préservé
        self.assertEqual(result[0]["theme"], "Functional Analysis")
        self.assertEqual(result[-1]["theme"], "Software Testing")


class TestParseUnderutilizedFolders(unittest.TestCase):
    """Parser pour la section 'Dossiers sous-utilisés' du rapport Phase A."""

    def test_parses_underutilized_section(self):
        md = """
### Mappings manquants critiques (1 cas)
- **Theme1** (10 fichiers) → dossier cible évident : `A/B`

### Dossiers sous-utilisés avec thèmes orphelins correspondants (3 cas)
**02-INFORMATIQUE/03-Langages-Programmation/Python** (0 fichier) ↔ thème orphelin "python programming" (320 occurrences)
**01-SCIENCES/MATHEMATIQUES** (0 fichier) ↔ tous les sous-dossiers sont peuplés (normal)
**08-LOISIRS/DESSIN** (0 fichier) – aucun thème orphelin associé

### Catch-all qui débordent (1 cas)
**/Autres** (4000 fichiers) – fourre-tout
"""
        result = parse_underutilized_folders_from_report(md)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["folder"], "02-INFORMATIQUE/03-Langages-Programmation/Python")
        self.assertEqual(result[0]["count"], 0)
        self.assertEqual(result[2]["folder"], "08-LOISIRS/DESSIN")
        # Le note du 1er doit mentionner le thème orphelin
        self.assertIn("python", result[0]["note"].lower())

    def test_handles_thousands_with_spaces(self):
        md = "### Dossiers sous-utilisés (1 cas)\n**A/B** (1 234 fichiers) – grosse note"
        result = parse_underutilized_folders_from_report(md)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["count"], 1234)

    def test_returns_empty_when_section_absent(self):
        md = "### Une autre section\n**A** (0 fichier) – note"
        self.assertEqual(parse_underutilized_folders_from_report(md), [])


class TestParseCatchallFolders(unittest.TestCase):
    """Parser pour 'Catch-all qui débordent'."""

    def test_parses_catchall_section(self):
        md = """
### Catch-all qui débordent (2 cas)
1. **02-INFORMATIQUE/03-Langages-Programmation/Autres** (4 489 fichiers) – contient tous les langages non spécifiques
2. **01-SCIENCES/MATHEMATIQUES/08-Mathematiques-Generales** (2 659 fichiers) – fourre-tout mathématique
"""
        result = parse_catchall_folders_from_report(md)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["folder"], "02-INFORMATIQUE/03-Langages-Programmation/Autres")
        self.assertEqual(result[0]["count"], 4489)
        self.assertEqual(result[1]["count"], 2659)

    def test_returns_empty_for_non_analysed(self):
        md = "### Catch-all qui débordent (non analysé)\n- Pas d'analyse effectuée"
        self.assertEqual(parse_catchall_folders_from_report(md), [])


class TestProposeChangesWithDeletions(_ProposBase):
    """Sémantique des deletions ajoutée en B.3-ter."""

    def test_deletion_removes_folder_from_tree(self):
        result = propose_changes(
            profile="p", run_id="del-1",
            deletions=[{"path": "B", "rationale": "vide, plus utile"}],
        )
        self.assertEqual(result["n_deletions"], 1)
        rundir = self.tmp / "profiles" / "p" / ".cache" / "refonte" / "del-1" / "proposed"
        folders = yaml.safe_load((rundir / "tree-proposed.yaml").read_text())["folders"]
        # B était dans le profil de base (cf. setUp _make_profile)
        self.assertNotIn("B", folders)
        # A et A/Python toujours là
        self.assertIn("A", folders)
        self.assertIn("A/Python", folders)

    def test_deletion_drops_mappings_pointing_to_deleted_folder(self):
        """Si on supprime B, le mapping 'data: B' doit disparaître."""
        propose_changes(
            profile="p", run_id="del-2",
            deletions=[{"path": "B", "rationale": "vide"}],
        )
        rundir = self.tmp / "profiles" / "p" / ".cache" / "refonte" / "del-2" / "proposed"
        mapping = yaml.safe_load((rundir / "theme_mapping-proposed.yaml").read_text())
        # "data" pointait sur B → drop
        self.assertNotIn("data", mapping)
        # "python programming" pointait sur A/Python → toujours là
        self.assertEqual(mapping["python programming"], "A/Python")

    def test_rationale_md_includes_deletions_section(self):
        propose_changes(
            profile="p", run_id="del-3",
            deletions=[{"path": "Z", "rationale": "à supprimer"}],
        )
        rationale = (self.tmp / "profiles" / "p" / ".cache" / "refonte" / "del-3"
                     / "proposed" / "refonte-rationale.md").read_text()
        self.assertIn("SUPPRESSIONS (1)", rationale)
        self.assertIn("Z", rationale)


class TestInitNodeOrphanInjection(_ProposBase):
    """_init_node : appel direct find_orphan_themes + fusion suggestions markdown."""

    def _init_state(self, profile: str = "p", diag_id: str = "diag-init") -> dict:
        from agents.refonte.proposition import _init_node
        _seed_diagnostic_run(
            self.profiles_root, profile, diag_id,
            report_md=(
                "# Diagnostic\n\n"
                "### Mappings orphelins prioritaires\n"
                "- **Functional Analysis** (176 fichiers) → dossier cible : "
                "`A/MATH/Analyse`\n"
                "- **Group Theory** (112 fichiers) → cible : `A/MATH/Algebre`\n"
            ),
        )
        return _init_node({
            "profile": profile, "run_id": "b-init",
            "diagnostic_run_id": diag_id,
        })

    def test_uses_find_orphan_themes_when_available(self):
        """Si find_orphan_themes renvoie 200 entrées, on les injecte toutes
        avec les suggestions du markdown pour le top 30 et None pour le reste."""
        fake_orphans = [
            {"theme": "Functional Analysis", "count": 176, "is_orphan": True,
             "sample_titles": ["Rudin", "Brezis"]},
            {"theme": "Group Theory", "count": 112, "is_orphan": True,
             "sample_titles": []},
            {"theme": "Long Tail Theme", "count": 12, "is_orphan": True,
             "sample_titles": []},
        ]
        with mock.patch(
            "agents.refonte.proposition.find_orphan_themes",
            return_value=fake_orphans,
        ):
            state = self._init_state()
        human_msg = state["messages"][1].content
        # Les 3 thèmes doivent apparaître
        self.assertIn("Functional Analysis", human_msg)
        self.assertIn("Group Theory", human_msg)
        self.assertIn("Long Tail Theme", human_msg)
        # Le markdown suggère un target pour Functional Analysis / Group Theory
        self.assertIn("A/MATH/Analyse", human_msg)
        self.assertIn("A/MATH/Algebre", human_msg)
        # Long Tail Theme n'a PAS de suggestion → le champ doit être absent
        # pour cette entrée (le LLM choisira)
        self.assertIn("target_folder_suggested", human_msg)
        # n=3 affiché
        self.assertIn("(n=3)", human_msg)

    def test_falls_back_to_markdown_when_find_orphan_fails(self):
        """Si find_orphan_themes échoue (pas de vision_cache), on retombe
        sur le parsing markdown au lieu de partir avec une liste vide."""
        with mock.patch(
            "agents.refonte.proposition.find_orphan_themes",
            side_effect=FileNotFoundError("vision_cache absent"),
        ):
            state = self._init_state()
        human_msg = state["messages"][1].content
        # Le fallback markdown a injecté les 2 entrées du diagnostic
        self.assertIn("Functional Analysis", human_msg)
        self.assertIn("Group Theory", human_msg)
        self.assertIn("A/MATH/Analyse", human_msg)

    def test_falls_back_when_find_orphan_returns_empty(self):
        """find_orphan_themes peut renvoyer [] (pas d'orphelin OU pas de cache).
        Dans ce cas on prend le markdown comme source."""
        with mock.patch(
            "agents.refonte.proposition.find_orphan_themes",
            return_value=[],
        ):
            state = self._init_state()
        human_msg = state["messages"][1].content
        self.assertIn("Functional Analysis", human_msg)
        self.assertIn("Group Theory", human_msg)


class TestProposeChangesCategoriesIntegration(unittest.TestCase):
    """Tests d'intégration : propose_changes écrit categories-proposed.yaml."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profiles_root = Path(self.tmp) / "profiles"
        self.profiles_root.mkdir(parents=True)
        target = Path(self.tmp) / "target"
        target.mkdir()
        _make_profile(
            self.profiles_root, "test_p",
            target=target,
            folders=["02-INFORMATIQUE/14-Web"],
            mapping={"Web Development": "02-INFORMATIQUE/14-Web"},
        )
        # Write categories.yaml
        (self.profiles_root / "test_p" / "categories.yaml").write_text(
            yaml.safe_dump({
                "informatique": [
                    {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3,
                     "mots_cles": ["html", "css"]},
                ],
            }, allow_unicode=True),
            encoding="utf-8",
        )
        # Patch chemins
        self.patcher_root = mock.patch(
            "dashboard.data.get_project_root",
            return_value=Path(self.tmp),
        )
        self.patcher_root.start()

    def tearDown(self):
        self.patcher_root.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_propose_changes_writes_categories_proposed_yaml(self):
        """Un rename est proposé → categories-proposed.yaml écrit avec
        l'entry remappée."""
        from agents.refonte.proposition_tools import propose_changes
        # Mock le LLM categories pour éviter l'appel réel
        with mock.patch(
            "agents.refonte.proposition_tools.propose_keywords_for_new_folders",
            return_value=[],
        ):
            propose_changes(
                profile="test_p",
                run_id="testrun-001",
                creations=[],
                renamings=[{
                    "old_path": "02-INFORMATIQUE/14-Web",
                    "new_path": "02-INFORMATIQUE/14-Web-Frontend",
                    "rationale": "clearer naming",
                }],
            )
        out_dir = self.profiles_root / "test_p" / ".cache" / "refonte" / "testrun-001" / "proposed"
        cat_path = out_dir / "categories-proposed.yaml"
        self.assertTrue(cat_path.exists())
        proposed = yaml.safe_load(cat_path.read_text(encoding="utf-8"))
        chemins = [e["chemin"] for e in proposed["informatique"]]
        self.assertIn("02-INFORMATIQUE/14-Web-Frontend", chemins)
        self.assertNotIn("02-INFORMATIQUE/14-Web", chemins)

    def test_propose_changes_no_categories_when_no_changes_apply(self):
        """Pas de renames/fusions/deletions/creations → pas de fichier écrit."""
        from agents.refonte.proposition_tools import propose_changes
        with mock.patch(
            "agents.refonte.proposition_tools.propose_keywords_for_new_folders",
            return_value=[],
        ):
            propose_changes(
                profile="test_p",
                run_id="testrun-002",
                mappings_added=[{
                    "theme": "Foo", "folder": "02-INFORMATIQUE/14-Web",
                    "rationale": "x",
                }],
            )
        cat_path = (self.profiles_root / "test_p" / ".cache" / "refonte"
                    / "testrun-002" / "proposed" / "categories-proposed.yaml")
        self.assertFalse(cat_path.exists())

    def test_propose_changes_no_categories_when_profile_has_none(self):
        """Profil sans categories.yaml → pas de fichier produit, pas de crash."""
        # Remove categories.yaml
        (self.profiles_root / "test_p" / "categories.yaml").unlink()

        from agents.refonte.proposition_tools import propose_changes
        with mock.patch(
            "agents.refonte.proposition_tools.propose_keywords_for_new_folders",
            return_value=[],
        ):
            result = propose_changes(
                profile="test_p",
                run_id="testrun-003",
                creations=[{
                    "path": "02-INFORMATIQUE/05-IA-ML/RAG",
                    "rationale": "RAG folder",
                }],
            )
        cat_path = (self.profiles_root / "test_p" / ".cache" / "refonte"
                    / "testrun-003" / "proposed" / "categories-proposed.yaml")
        self.assertFalse(cat_path.exists())
        # propose_changes a quand même réussi (tree + mapping écrits)
        self.assertIn("tree_proposed_path", result)


if __name__ == "__main__":
    unittest.main()
