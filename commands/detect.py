"""Sous-commande detect — détection de patterns de nommage."""

import glob as globmod
import os
import re
from pathlib import Path

from lib.logger import get_logger
from lib.pattern_detector import PatternResult, detect_pattern, test_coverage

log = get_logger()


def _collect_filenames(args, profile) -> list[str]:
    """Collecte les noms de fichiers selon le mode choisi."""
    if getattr(args, "files", None):
        # Mode direct : liste de fichiers
        return [os.path.basename(f) for f in args.files]

    if getattr(args, "dir", None):
        # Mode dossier/glob
        target = args.dir
        if os.path.isdir(target):
            files = globmod.glob(os.path.join(target, "*.pdf"))
        else:
            files = globmod.glob(target)
        files = [f for f in files if f.lower().endswith(".pdf")]
        if not files:
            log.info("  ⚠ Aucun fichier PDF trouvé dans %s", target)
            return []
        return [os.path.basename(f) for f in sorted(files)]

    # Mode interactif
    return _interactive_selection()


def _interactive_selection() -> list[str]:
    """Sélection interactive de fichiers."""
    try:
        raw = input(
            "\n  Entrez un dossier, un pattern glob, ou des noms séparés par des virgules :\n  > "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return []

    if not raw:
        return []

    # Essayer comme dossier
    if os.path.isdir(raw):
        files = sorted(globmod.glob(os.path.join(raw, "*.pdf")))
    # Essayer comme glob
    elif "*" in raw or "?" in raw:
        files = sorted(f for f in globmod.glob(raw) if f.lower().endswith(".pdf"))
    # Traiter comme liste séparée par des virgules
    else:
        names = [n.strip() for n in raw.split(",") if n.strip()]
        return names

    if not files:
        log.info("  ⚠ Aucun fichier PDF trouvé.")
        return []

    basenames = [os.path.basename(f) for f in files]

    # Afficher la liste numérotée
    print()
    for i, name in enumerate(basenames, 1):
        print(f"  {i:3d}. {name}")
    print()

    try:
        sel = input(
            "  Sélectionnez les numéros (ex: 1,3,5-8) ou Entrée pour tous : "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return []

    if not sel:
        return basenames

    # Parser la sélection
    indices = _parse_selection(sel, len(basenames))
    return [basenames[i] for i in indices]


def _parse_selection(s: str, max_n: int) -> list[int]:
    """Parse une sélection comme '1,3,5-8' en indices (0-based)."""
    indices: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                start = int(bounds[0]) - 1
                end = int(bounds[1]) - 1
                indices.extend(range(max(0, start), min(max_n, end + 1)))
            except ValueError:
                continue
        else:
            try:
                idx = int(part) - 1
                if 0 <= idx < max_n:
                    indices.append(idx)
            except ValueError:
                continue
    return sorted(set(indices))


def _display_result(result: PatternResult) -> None:
    """Affiche le résultat de la détection."""
    pct_conf = int(result.confidence * 100)

    print()
    print("=" * 60)
    print("  Pattern détecté")
    print("=" * 60)
    print(f"  Regex       : {result.regex}")
    print(f"  Description : {result.description}")
    print(f"  Confiance   : {pct_conf}%")

    if result.examples_matched:
        print()
        print("  Exemples qui correspondent :")
        for ex in result.examples_matched[:5]:
            print(f"    ✓ {ex}")

    if result.examples_rejected:
        print()
        print("  Exemples qui ne correspondent pas :")
        for ex in result.examples_rejected[:5]:
            print(f"    ✗ {ex}")

    if result.coverage_total > 0:
        pct = int(result.coverage_count / result.coverage_total * 100)
        print()
        print(
            f"  Couverture bibliothèque : "
            f"{result.coverage_count:,} / {result.coverage_total:,} fichiers ({pct}%)"
        )
        if result.false_positive_samples:
            print(f"  Potentiels faux positifs ({len(result.false_positive_samples)} exemples) :")
            for fp in result.false_positive_samples:
                print(f"    ? {fp}")

    print("=" * 60)
    print()


def _inject_pattern(result: PatternResult, profile_dir: Path) -> bool:
    """Injecte un pattern dans profile.yaml en préservant les commentaires.

    Returns:
        True si le pattern a été ajouté, False sinon.
    """
    yaml_path = profile_dir / "profile.yaml"
    if not yaml_path.exists():
        log.warning("  ⚠ Fichier introuvable : %s", yaml_path)
        return False

    content = yaml_path.read_text(encoding="utf-8")
    escaped_regex = result.regex.replace('"', '\\"')
    new_line = f'    - "{escaped_regex}"'

    # Vérifier si le pattern existe déjà
    if result.regex in content:
        log.info("  ℹ Pattern déjà présent dans le profil.")
        return False

    lines = content.splitlines()

    # Chercher le bloc name_patterns:
    patterns_idx = None
    last_item_idx = None
    for i, line in enumerate(lines):
        if re.match(r'^\s+name_patterns:\s*$', line):
            patterns_idx = i
        elif patterns_idx is not None and re.match(r'^\s+-\s+".*"', line):
            last_item_idx = i
        elif patterns_idx is not None and last_item_idx is not None:
            # On a quitté le bloc name_patterns
            if not re.match(r'^\s+-', line):
                break

    if last_item_idx is not None:
        # Insérer après le dernier item
        lines.insert(last_item_idx + 1, new_line)
    elif patterns_idx is not None:
        # name_patterns: existe mais est vide
        lines.insert(patterns_idx + 1, new_line)
    else:
        # Pas de bloc rename/name_patterns — l'ajouter à la fin
        lines.append("")
        lines.append("# ── Renommage ──")
        lines.append("rename:")
        lines.append("  name_patterns:")
        lines.append(new_line)

    yaml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def cmd_detect(args, profile) -> None:
    """Commande detect — détection de patterns de nommage."""
    filenames = _collect_filenames(args, profile)
    if len(filenames) < 2:
        log.info("  ⚠ Il faut au moins 2 fichiers pour détecter un pattern.")
        return

    log.info("\n  🔍 Analyse de %d fichiers...", len(filenames))
    for f in filenames[:10]:
        log.info("    • %s", f)
    if len(filenames) > 10:
        log.info("    ... et %d autres", len(filenames) - 10)

    # Récupérer la clé API
    api_key = os.environ.get("SILICONFLOW_API_KEY", "")
    if not api_key and profile.llm_provider != "ollama":
        log.info("  ⚠ Clé API manquante (SILICONFLOW_API_KEY).")
        return

    result = detect_pattern(
        filenames,
        api_key=api_key,
        endpoint=profile.llm_endpoint,
        model=profile.llm_model,
        verbose=getattr(args, "verbose", False),
    )

    if result is None:
        log.info("  ⚠ Impossible de détecter un pattern.")
        return

    # Tester la couverture sur la bibliothèque
    library_path = getattr(args, "library", None) or profile.target
    if library_path and os.path.isdir(library_path):
        log.info("  📊 Test de couverture sur %s...", library_path)
        all_pdfs = []
        for root, _, files in os.walk(library_path):
            all_pdfs.extend(f for f in files if f.lower().endswith(".pdf"))
        input_stems = [Path(f).stem for f in filenames]
        test_coverage(result, all_pdfs, input_stems=input_stems)

    _display_result(result)

    if not getattr(args, "execute", False):
        log.info("  📋 Dry-run. Utilisez --execute pour injecter dans profile.yaml.")
        return

    if not getattr(args, "yes", False):
        try:
            answer = input("  Injecter ce pattern dans profile.yaml ? (o/N) : ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            log.info("\n  Annulé.")
            return
        if answer not in ("o", "oui", "y", "yes"):
            log.info("  Annulé.")
            return

    if _inject_pattern(result, profile.profile_dir):
        log.info("  ✅ Pattern ajouté à profiles/%s/profile.yaml", profile.name)
    else:
        log.info("  ℹ Aucune modification.")
