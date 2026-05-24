"""Sous-commande thumbnails — backfill du cache thumbnail du dashboard.

Itère sur tous les PDF/ePub d'un profil et pré-remplit le cache des
miniatures (cover de page 1) utilisé par le viewer du dashboard. Sans
ça, le dashboard génère les thumbnails à la demande au premier clic
sur un fichier — coût ~3-5 s par fichier sur SSD externe.

La clé du cache est ``MD5(head bytes du fichier)`` (alignée sur
``lib.thumbnail.compute_content_key``), donc le cache **survit aux
renommages** : si tu renommes un fichier plus tard, le thumbnail
reste valide tant que le contenu n'a pas bougé.

Usage::

    ./klodo.sh thumbnails                         # dry-run (compte ce qu'il faut faire)
    ./klodo.sh thumbnails --execute               # génère la page 1 (cover seule)
    ./klodo.sh thumbnails --execute --pages 5     # génère les 5 premières pages
    ./klodo.sh thumbnails --execute --max 100     # limiter pour tester
    ./klodo.sh thumbnails --execute --force       # régénère même les caches existants
"""

import os
import sys
import time
from pathlib import Path

from lib.logger import get_logger
from lib.thumbnail import compute_content_key, generate_thumbnail

log = get_logger()


_SUPPORTED_EXTS = (".pdf", ".epub")


def _enumerate_files(target: Path) -> list[Path]:
    """Walk the target, return every PDF/ePub. Skips dotfiles + dotdirs
    + the .trash/ folder (which is by definition out of scope)."""
    out: list[Path] = []
    for root, dirs, files in os.walk(str(target)):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith(".") or not f.lower().endswith(_SUPPORTED_EXTS):
                continue
            out.append(Path(root) / f)
    return out


def cmd_thumbnails(args, profile) -> None:
    """Pré-remplit le cache thumbnail du dashboard pour tous les fichiers
    du profil. Lance ``./klodo.sh thumbnails --execute``."""
    target = Path(profile.target)
    if not target.exists():
        log.error("❌ Target introuvable : %s", target)
        sys.exit(1)

    cache_root = Path(profile.cache_dir) / "thumbnails"
    cache_root.mkdir(parents=True, exist_ok=True)

    execute = getattr(args, "execute", False)
    force = getattr(args, "force", False)
    max_files = int(getattr(args, "max", 0) or 0)
    n_pages = max(1, min(5, int(getattr(args, "pages", 1) or 1)))

    log.info("📸 Backfill thumbnail cache pour profil « %s »", profile.name)
    log.info("   target = %s", target)
    log.info("   cache  = %s", cache_root)
    log.info("   pages  = %d par fichier", n_pages)
    if not execute:
        log.info("   mode   = DRY-RUN (ajouter --execute pour générer)")

    files = _enumerate_files(target)
    if max_files > 0:
        files = files[:max_files]
        log.info("   limite = %d fichiers (--max)", max_files)
    log.info("   scope  = %d fichiers PDF/ePub trouvés\n", len(files))

    # Pre-pass: count what would be done. "Cached" = the highest
    # requested page already exists (generate_thumbnail produces pages
    # 1..N contiguously, so if page N is on disk, pages 1..N-1 are too).
    to_generate: list[tuple[Path, Path]] = []     # (source, cache_dir)
    already_cached = 0
    unreadable = 0
    for src in files:
        key = compute_content_key(src)
        if not key:
            unreadable += 1
            continue
        dest_dir = cache_root / key
        deepest_page = dest_dir / f"{n_pages}.jpg"
        if deepest_page.exists() and not force:
            already_cached += 1
            continue
        to_generate.append((src, dest_dir))

    log.info("📊 État actuel :")
    log.info("   déjà en cache : %d", already_cached)
    log.info("   à générer     : %d", len(to_generate))
    if unreadable:
        log.info("   illisibles    : %d (skipped)", unreadable)
    log.info("")

    if not to_generate:
        log.info("✅ Rien à faire. Cache déjà complet.")
        return

    if not execute:
        log.info("🚦 Dry-run terminé. Ajoute --execute pour générer.")
        # Show a small sample so the user sees what would happen
        log.info("\nÉchantillon (5 premiers) :")
        for src, dest in to_generate[:5]:
            log.info("   %s → %s/1.jpg",
                      src.name[:60], dest.name)
        return

    # Generation. Single-threaded by design — generate_thumbnail uses
    # poppler which is already pretty fast (~30-80 ms per PDF page at
    # 150 dpi). Parallel speedup would be modest and complicate the
    # progress display.
    log.info("⚙ Génération en cours…\n")
    t0 = time.perf_counter()
    n_done = 0
    n_failed = 0
    progress_interval = max(1, len(to_generate) // 50)  # ~50 progress ticks total
    for i, (src, dest_dir) in enumerate(to_generate, start=1):
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            n = generate_thumbnail(src, dest_dir, n_pages=n_pages, start_page=1)
            if n > 0:
                n_done += 1
            else:
                n_failed += 1
        except Exception as exc:  # pragma: no cover — defensive
            log.warning("  ⚠ %s : %s", src.name, exc)
            n_failed += 1
        if i % progress_interval == 0 or i == len(to_generate):
            pct = 100 * i / len(to_generate)
            elapsed = time.perf_counter() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(to_generate) - i) / rate if rate > 0 else 0
            log.info("   [%4d/%4d] %5.1f%% · %.1f f/s · ETA %.0fs",
                      i, len(to_generate), pct, rate, eta)

    elapsed = time.perf_counter() - t0
    log.info("\n✅ Terminé en %.1fs", elapsed)
    log.info("   générés : %d", n_done)
    if n_failed:
        log.info("   échoués : %d", n_failed)
    log.info("   moyenne : %.1f f/s", n_done / elapsed if elapsed > 0 else 0)
