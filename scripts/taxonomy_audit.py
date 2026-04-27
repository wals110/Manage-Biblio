"""Taxonomy health audit.

Cross-references 3 sources of truth to detect desalignments:
- tree.yaml               : declared folder hierarchy
- theme_mapping.yaml      : theme → folder mappings
- <target_path>/**        : actual folders on disk (SSD)

Generates a markdown report in review/taxonomy-health.md listing:
- Ghost folders (in mapping but not in tree)
- Empty folders (in tree but never used in mapping, and empty on disk)
- Orphan folders (on disk but not in tree)
- Duplicate themes (same folder reached via slightly different theme strings)
- Case inconsistencies in themes

Usage: uv run python scripts/taxonomy_audit.py [--profile default]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml


def main(profile: str) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    profile_dir = repo_root / "profiles" / profile
    tree_path = profile_dir / "tree.yaml"
    mapping_path = profile_dir / "theme_mapping.yaml"
    profile_path = profile_dir / "profile.yaml"

    tree = yaml.safe_load(tree_path.read_text())
    mapping = yaml.safe_load(mapping_path.read_text())
    profile_cfg = yaml.safe_load(profile_path.read_text())

    tree_folders = {f for f in tree.get("folders", []) if not f.startswith("_")}
    mapping_folders = set(mapping.values())

    target_base = Path(profile_cfg.get("target") or profile_cfg.get("target_path") or "")
    disk_folders: set[str] = set()
    disk_file_counts: Counter[str] = Counter()

    if target_base.exists():
        for pdf in target_base.rglob("*.pdf"):
            rel = pdf.relative_to(target_base)
            folder = str(rel.parent)
            if folder.startswith("_") or "/_" in folder:
                continue
            disk_folders.add(folder)
            disk_file_counts[folder] += 1

    # --- Analysis ---
    ghost_in_mapping = mapping_folders - tree_folders
    unused_in_mapping = tree_folders - mapping_folders
    orphans_on_disk = disk_folders - tree_folders
    empty_on_disk = {
        f for f in tree_folders if f not in disk_folders
    }

    # Case/whitespace inconsistencies in theme keys
    norm_themes = defaultdict(list)
    for theme in mapping:
        norm = theme.lower().strip()
        norm_themes[norm].append(theme)
    case_conflicts = {k: v for k, v in norm_themes.items() if len(v) > 1}

    # Near-duplicate themes (string similarity — coarse)
    folder_themes: dict[str, list[str]] = defaultdict(list)
    for theme, folder in mapping.items():
        folder_themes[folder].append(theme)
    # Folders where multiple themes are very similar
    near_dupes: list[tuple[str, list[str]]] = []
    for folder, themes in folder_themes.items():
        normed = [t.lower().strip().rstrip("s") for t in themes]
        counts = Counter(normed)
        dupes = {k: v for k, v in counts.items() if v > 1}
        if dupes:
            examples = [t for t in themes if t.lower().strip().rstrip("s") in dupes]
            near_dupes.append((folder, examples))

    # --- Report ---
    report = []
    report.append("# Taxonomy Health Report")
    report.append("")
    report.append(f"**Profil** : `{profile}`  ")
    report.append(f"**Target** : `{target_base}`  ")
    report.append(f"**Date génération** : `scripts/taxonomy_audit.py`")
    report.append("")
    report.append("## Résumé")
    report.append("")
    report.append("| Métrique | Valeur |")
    report.append("|---|---|")
    report.append(f"| Dossiers déclarés dans `tree.yaml` | {len(tree_folders)} |")
    report.append(f"| Dossiers référencés dans `theme_mapping.yaml` | {len(mapping_folders)} |")
    report.append(f"| Dossiers physiques sur disque | {len(disk_folders)} |")
    report.append(f"| Thèmes dans `theme_mapping.yaml` | {len(mapping)} |")
    report.append(f"| Fichiers classifiés sur disque | {sum(disk_file_counts.values())} |")
    report.append("")

    report.append("## ❌ Dossiers fantômes (dans mapping mais absents de tree.yaml)")
    report.append("")
    if ghost_in_mapping:
        report.append(f"**{len(ghost_in_mapping)} dossiers** référencés par le mapping mais absents de la taxonomie officielle. Les fichiers classés vers ces dossiers contournent silencieusement la structure déclarée.")
        report.append("")
        report.append("| Dossier fantôme | Thèmes associés | Fichiers sur disque |")
        report.append("|---|---|---|")
        for folder in sorted(ghost_in_mapping):
            themes = [t for t, f in mapping.items() if f == folder]
            count = disk_file_counts.get(folder, 0)
            themes_str = ", ".join(f"`{t}`" for t in themes[:5])
            if len(themes) > 5:
                themes_str += f" (+{len(themes) - 5})"
            report.append(f"| `{folder}` | {themes_str} | {count} |")
    else:
        report.append("✅ Aucun dossier fantôme.")
    report.append("")

    report.append("## ⚠️ Dossiers déclarés mais jamais référencés par le mapping")
    report.append("")
    if unused_in_mapping:
        report.append(f"**{len(unused_in_mapping)} dossiers** présents dans `tree.yaml` mais aucun thème ne pointe vers eux. Ces dossiers ne seront jamais atteints par le classifier LLM.")
        report.append("")
        for folder in sorted(unused_in_mapping):
            count = disk_file_counts.get(folder, 0)
            status = f"contient {count} fichiers" if count > 0 else "**vide sur disque**"
            report.append(f"- `{folder}` — {status}")
    else:
        report.append("✅ Tous les dossiers déclarés sont référencés.")
    report.append("")

    report.append("## ⚠️ Dossiers déclarés mais vides sur disque")
    report.append("")
    if empty_on_disk:
        report.append(f"**{len(empty_on_disk)} dossiers** déclarés dans `tree.yaml` sans aucun fichier sur disque. Soit inutilisés, soit jamais atteints par classification.")
        report.append("")
        for folder in sorted(empty_on_disk):
            in_mapping = any(v == folder for v in mapping.values())
            mark = "référencé par mapping" if in_mapping else "non référencé"
            report.append(f"- `{folder}` ({mark})")
    else:
        report.append("✅ Tous les dossiers déclarés contiennent au moins un fichier.")
    report.append("")

    report.append("## ⚠️ Dossiers physiques sur disque absents de tree.yaml")
    report.append("")
    if orphans_on_disk:
        report.append(f"**{len(orphans_on_disk)} dossiers** contiennent des fichiers mais n'existent pas dans la taxonomie officielle.")
        report.append("")
        report.append("| Dossier orphelin | Fichiers |")
        report.append("|---|---|")
        for folder in sorted(orphans_on_disk, key=lambda f: -disk_file_counts[f]):
            report.append(f"| `{folder}` | {disk_file_counts[folder]} |")
    else:
        report.append("✅ Tous les dossiers physiques sont déclarés.")
    report.append("")

    report.append("## ⚠️ Conflits de casse / whitespace sur les clés de thèmes")
    report.append("")
    if case_conflicts:
        report.append(f"**{len(case_conflicts)} thèmes** ont des variantes (même texte normalisé lowercase+strip) traitées comme des entrées distinctes.")
        report.append("")
        for norm, variants in sorted(case_conflicts.items()):
            variants_str = " ≠ ".join(f"`{v!r}`" for v in variants)
            report.append(f"- {variants_str}")
    else:
        report.append("✅ Aucun conflit de casse.")
    report.append("")

    report.append("## ⚠️ Quasi-doublons (même folder via thèmes très proches)")
    report.append("")
    if near_dupes:
        report.append(f"**{len(near_dupes)} folders** pointés par des thèmes qui ne diffèrent que par un `s` terminal (ex. `Chemical Science`/`Chemical Sciences`).")
        report.append("")
        for folder, themes in near_dupes:
            themes_str = " ≠ ".join(f"`{t!r}`" for t in themes)
            report.append(f"- `{folder}` ← {themes_str}")
    else:
        report.append("✅ Aucun quasi-doublon détecté.")
    report.append("")

    # Distribution of files per folder
    report.append("## 📊 Distribution des fichiers par dossier (disk)")
    report.append("")
    if disk_file_counts:
        counts_list = sorted(disk_file_counts.items(), key=lambda x: -x[1])
        report.append(f"- **min** : {min(disk_file_counts.values())}")
        report.append(f"- **max** : {max(disk_file_counts.values())} (`{counts_list[0][0]}`)")
        report.append(f"- **médiane** : {sorted(disk_file_counts.values())[len(disk_file_counts)//2]}")
        report.append(f"- **classes ≥ 100 fichiers** : {sum(1 for v in disk_file_counts.values() if v >= 100)}")
        report.append(f"- **classes < 10 fichiers** : {sum(1 for v in disk_file_counts.values() if v < 10)}")
        report.append("")
        report.append("### Top 10 dossiers les plus peuplés")
        report.append("")
        for folder, n in counts_list[:10]:
            report.append(f"- `{folder}` : {n} fichiers")
        report.append("")
        report.append("### 10 dossiers les moins peuplés (hors vides)")
        report.append("")
        non_empty = [(f, n) for f, n in counts_list if n > 0]
        for folder, n in non_empty[-10:]:
            report.append(f"- `{folder}` : {n} fichiers")
    report.append("")

    # Actionable recommendations
    report.append("## ✅ Recommandations")
    report.append("")
    recs = []
    if ghost_in_mapping:
        recs.append(f"**Réconcilier les {len(ghost_in_mapping)} dossiers fantômes** — soit les ajouter à `tree.yaml`, soit corriger le mapping pour pointer vers des dossiers déclarés.")
    if orphans_on_disk:
        recs.append(f"**Examiner les {len(orphans_on_disk)} dossiers orphelins sur disque** — soit les ajouter à `tree.yaml`, soit déplacer les fichiers.")
    if case_conflicts:
        recs.append(f"**Dédupliquer les {len(case_conflicts)} conflits de casse** dans `theme_mapping.yaml`.")
    if near_dupes:
        recs.append(f"**Fusionner les {len(near_dupes)} quasi-doublons** (pluriels / singuliers).")
    if unused_in_mapping:
        recs.append(f"**Décider du sort des {len(unused_in_mapping)} dossiers déclarés mais non référencés** — ajouter des entrées au mapping ou supprimer les dossiers de la taxonomie.")

    if not recs:
        report.append("✅ Aucune action requise — la taxonomie est cohérente.")
    else:
        for i, rec in enumerate(recs, 1):
            report.append(f"{i}. {rec}")
    report.append("")

    # Write report
    out_path = repo_root / "review" / "taxonomy-health.md"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text("\n".join(report))

    # Summary to console
    print(f"Taxonomy audit complete → {out_path.relative_to(repo_root)}")
    print(f"  - {len(ghost_in_mapping)} dossiers fantômes (mapping ∉ tree)")
    print(f"  - {len(unused_in_mapping)} dossiers déclarés jamais référencés")
    print(f"  - {len(orphans_on_disk)} dossiers orphelins sur disque")
    print(f"  - {len(empty_on_disk)} dossiers déclarés vides sur disque")
    print(f"  - {len(case_conflicts)} conflits de casse")
    print(f"  - {len(near_dupes)} quasi-doublons")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    args = parser.parse_args()
    sys.exit(main(args.profile))
