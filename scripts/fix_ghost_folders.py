"""Add missing folders to tree.yaml to fix ghost folders identified by audit.

Re-emits tree.yaml with the same section-grouped, alphabetically-sorted
format used by lib/llm_mapper.py when it appends new folders.

Folders to add (15 paths total) — mix of:
- 12 ghost folders (referenced in theme_mapping.yaml, missing from tree.yaml)
- 3 disk-orphan parents (folders containing files but neither in mapping
  nor tree, likely auto-created)

Usage: uv run python scripts/fix_ghost_folders.py [--profile default] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

# Folders to add. Each is justified by either:
#  - reference in theme_mapping.yaml (ghost) — taxonomy-health.md §1
#  - presence on disk with files (disk orphan) — taxonomy-health.md §4
TO_ADD = [
    # Ghost folders (in mapping, missing from tree)
    "01-SCIENCES",
    "01-SCIENCES/BIOLOGIE",
    "01-SCIENCES/PHYSIQUE",
    "02-INFORMATIQUE",
    "02-INFORMATIQUE/05-IA-ML",
    "02-INFORMATIQUE/06-Data-Science",
    "02-INFORMATIQUE/09-Systemes-OS",
    "04-SHS",
    "05-RELIGIONS",
    "06-MEDECINE",
    "07-LANGUES",
    "09-BUSINESS",
    # Disk orphans (files exist on disk, no tree declaration)
    "01-SCIENCES/MATHEMATIQUES",
    "02-INFORMATIQUE/03-Langages-Programmation",
    "08-LOISIRS/DESSIN/Techniques-Materiaux",
]


def emit_tree_yaml(folders: list[str]) -> str:
    """Emit folders as YAML with section comments matching existing format."""
    sorted_folders = sorted(folders)
    n = len([f for f in sorted_folders if not f.startswith("_")])

    lines = [
        "# " + "=" * 75,
        "# Arborescence cible",
        f"# {n} dossiers",
        "# " + "=" * 75,
        "",
        "folders:",
    ]

    current_section = ""
    for folder in sorted_folders:
        section = folder.split("/")[0]
        if section != current_section:
            if current_section:
                lines.append("")
            lines.append(f"  # ── {section} ──")
            current_section = section
        lines.append(f"  - {folder}")

    lines.append("")  # trailing newline
    return "\n".join(lines)


def main(profile: str, dry_run: bool) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    tree_path = repo_root / "profiles" / profile / "tree.yaml"

    tree = yaml.safe_load(tree_path.read_text())
    existing = set(tree.get("folders", []))
    existing_count = len(existing)

    already_present = [f for f in TO_ADD if f in existing]
    new = [f for f in TO_ADD if f not in existing]

    print(f"Profile           : {profile}")
    print(f"Path              : {tree_path.relative_to(repo_root)}")
    print(f"Folders existants : {existing_count}")
    print(f"Folders à ajouter : {len(new)} (sur {len(TO_ADD)} demandés)")
    if already_present:
        print(f"Déjà présents     : {len(already_present)} → {already_present}")
    print()

    if not new:
        print("Rien à faire — tous les dossiers sont déjà déclarés.")
        return 0

    print("Nouveaux dossiers :")
    for f in new:
        print(f"  + {f}")
    print()

    final = sorted(existing | set(TO_ADD))
    output = emit_tree_yaml(final)

    if dry_run:
        print("--- DRY RUN — preview tree.yaml output ---")
        print(output[:1500])
        print("...")
        print(f"({len(output.splitlines())} lignes au total)")
        return 0

    tree_path.write_text(output)
    print(f"✅ {tree_path.relative_to(repo_root)} mis à jour : {existing_count} → {len(final) - 1} dossiers (hors _A-TRIER)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--dry-run", action="store_true", help="Affiche sans écrire")
    args = parser.parse_args()
    sys.exit(main(args.profile, args.dry_run))
