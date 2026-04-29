#!/usr/bin/env python3
"""
Restaure les noms originaux des PDFs dans une bibliothèque de test.

Lit les fichiers `log_renommage_*.csv` (produits par `execute()` dans renamer.py)
et restaure les noms originaux avant qu'un flatten ne remette tout dans _INBOX.

Usage :
    uv run python scripts/restore_original_names.py /chemin/BIBLIO logs
    uv run python scripts/restore_original_names.py /chemin/BIBLIO logs --execute
"""

import argparse
import csv
import sys
from pathlib import Path


def build_mapping(logs_dir: Path) -> dict[str, str]:
    """Construit le mapping basename_actuel → basename_original.

    Parcourt récursivement les log_renommage_*.csv du plus récent au plus ancien.
    Premier mapping trouvé gagne (évite d'écraser un renommage plus récent).
    """
    mapping: dict[str, str] = {}
    logs = sorted(logs_dir.rglob("log_renommage_*.csv"),
                  key=lambda p: p.name, reverse=True)

    for log in logs:
        with open(log, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("status") != "OK":
                    continue
                old = Path(row["ancien_chemin"]).name
                new = Path(row["nouveau_chemin"]).name
                if new in mapping:
                    continue
                mapping[new] = old
    return mapping


def restore_names(library: Path, logs_dir: Path, execute: bool) -> tuple[int, int]:
    """Restaure les noms originaux dans la bibliothèque.

    Returns:
        (restored_count, collision_count)
    """
    mapping = build_mapping(logs_dir)
    if not mapping:
        print("  ℹ Aucun mapping trouvé dans les logs.")
        return 0, 0

    print(f"  📖 {len(mapping)} mappings chargés depuis les logs.")

    restored = 0
    collisions = 0

    for pdf in library.rglob("*.pdf"):
        if pdf.name not in mapping:
            continue

        original_name = mapping[pdf.name]
        target = pdf.parent / original_name

        # Gestion des collisions : suffixer si le nom original existe déjà
        if target.exists() and target != pdf:
            stem = target.stem
            counter = 2
            while target.exists():
                target = pdf.parent / f"{stem} ({counter}).pdf"
                counter += 1
            collisions += 1

        if execute:
            try:
                pdf.rename(target)
                restored += 1
            except OSError as e:
                print(f"  ⚠ Erreur renommage {pdf.name}: {e}")
        else:
            print(f"  [dry-run] {pdf.name} → {target.name}")
            restored += 1

    return restored, collisions


def main():
    parser = argparse.ArgumentParser(
        description="Restaure les noms originaux des PDFs depuis les logs de renommage."
    )
    parser.add_argument("library", help="Chemin de la bibliothèque")
    parser.add_argument("logs_dir", help="Dossier contenant les log_renommage_*.csv")
    parser.add_argument("--execute", action="store_true", help="Appliquer (sinon dry-run)")
    args = parser.parse_args()

    library = Path(args.library)
    logs_dir = Path(args.logs_dir)

    if not library.exists():
        print(f"❌ Bibliothèque introuvable : {library}")
        sys.exit(1)
    if not logs_dir.exists():
        print(f"❌ Dossier logs introuvable : {logs_dir}")
        sys.exit(1)

    print(f"🔄 Restauration des noms originaux dans {library}")
    print(f"   Logs : {logs_dir}")
    print(f"   Mode : {'EXÉCUTION' if args.execute else 'DRY-RUN'}")
    print()

    restored, collisions = restore_names(library, logs_dir, args.execute)

    print()
    print(f"  ✅ {restored} fichiers restaurés")
    if collisions > 0:
        print(f"  ⚠ {collisions} collisions (suffixe numérique ajouté)")


if __name__ == "__main__":
    main()
