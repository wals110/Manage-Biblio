"""Re-cycle vision sur les fichiers SANS cache v3 uniquement.

Quand le PROMPT_VERSION du cache vision a été bumpé (v1 → v3 lors du fix
parser du 2026-05-04), toutes les entrées v1 deviennent inaccessibles
au pipeline. Ce script identifie les fichiers réels de la lib qui n'ont
pas de cache v3, et lance vision uniquement sur eux.

Usage:
    # smoke test obligatoire avant tout run >$1 (voir CLAUDE.md)
    uv run python scripts/recycle_vision_missing.py --smoke

    # dry-run : compter les fichiers à traiter, pas d'appel API
    uv run python scripts/recycle_vision_missing.py --dry-run

    # run réel (limite l'enveloppe avec --limit N pour test progressif)
    uv run python scripts/recycle_vision_missing.py --limit 50
    uv run python scripts/recycle_vision_missing.py --workers 8
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_print_lock = threading.Lock()


def safe_print(msg: str, file=None) -> None:
    """Thread-safe print that swallows I/O errors (handles 'closed stderr'
    issues that can arise when many workers spam stderr in parallel)."""
    f = file or sys.stdout
    try:
        with _print_lock:
            print(msg, file=f, flush=True)
    except (OSError, ValueError):
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib import vision_cache  # noqa: E402
from lib.profile import Profile  # noqa: E402
from lib.vision import analyze_cover_cached  # noqa: E402

_EXTS = (".pdf", ".epub")


def list_missing_v3(profile: Profile) -> list[Path]:
    """Walk the profile's target dir, return files with no v3 cache hit."""
    target = Path(profile.target)
    cache_path = Path(profile.cache_dir) / "vision_cache.json"
    cache = vision_cache.load_cache(cache_path) if cache_path.exists() else {}

    model = profile.llm_model
    n_pages = int(profile.defaults.get("pages", 1))

    missing: list[Path] = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if fname.startswith("."):
                continue
            if not fname.lower().endswith(_EXTS):
                continue
            full = Path(root) / fname
            key = vision_cache.compute_cache_key(
                str(full), model=model, n_pages=n_pages)
            if key and key in cache:
                continue
            missing.append(full)
    return missing


def process_one(pdf_path: Path, profile: Profile, cache_path: Path) -> dict:
    """Analyze + cache one file. Returns a small status dict.

    NOTE: we force n_candidates=0 (no smart page selection) because
    the smart-selection path returns empty titles for many files,
    silently busting the cache (analyze_cover_cached only stores
    entries with a non-empty title). With n_candidates=0 the LLM
    receives the first N pages directly, which works reliably.
    """
    try:
        result = analyze_cover_cached(
            str(pdf_path),
            cache_path=cache_path,
            api_key=os.environ.get("SILICONFLOW_API_KEY", ""),
            endpoint=profile.llm_endpoint,
            model=profile.llm_model,
            n_pages=int(profile.defaults.get("pages", 1)),
            n_candidates=0,
        )
    except Exception as exc:
        return {"path": str(pdf_path), "ok": False, "error": str(exc)}
    if isinstance(result, dict) and result.get("error"):
        return {"path": str(pdf_path), "ok": False, "error": result["error"]}
    return {"path": str(pdf_path), "ok": True,
            "title": (result or {}).get("title", "")}


def main(profile_name: str, workers: int, limit: int, smoke: bool, dry_run: bool) -> int:
    profile = Profile(profile_name)
    cache_path = Path(profile.cache_dir) / "vision_cache.json"

    print(f"Profile      : {profile_name}")
    print(f"Target       : {profile.target}")
    print(f"Model        : {profile.llm_model}")
    print(f"n_pages      : {profile.defaults.get('pages', 1)}")
    print(f"Workers      : {workers}")
    print()

    print("Recensement des fichiers sans cache v3…")
    missing = list_missing_v3(profile)
    print(f"  → {len(missing)} fichiers à analyser")
    print()

    if dry_run:
        print("DRY-RUN — aucun appel LLM.")
        print("Premiers fichiers qui seraient traités :")
        for p in missing[:10]:
            print(f"  - {p.relative_to(profile.target)}")
        return 0

    if smoke:
        missing = missing[:5]
        print(f"SMOKE TEST — limite à {len(missing)} fichiers (~10s, ~$0.01)")
    elif limit > 0:
        missing = missing[:limit]
        print(f"LIMITE — traitement de {len(missing)} fichiers max")

    if not missing:
        print("Rien à faire — tous les fichiers ont déjà du cache v3.")
        return 0

    if not os.environ.get("SILICONFLOW_API_KEY"):
        print("ERROR: SILICONFLOW_API_KEY manquant dans l'env", file=sys.stderr)
        return 2

    print()
    print(f"Lancement avec {workers} workers…")
    print()

    ok = 0
    err = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = [exe.submit(process_one, p, profile, cache_path) for p in missing]
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            if r["ok"]:
                ok += 1
            else:
                err += 1
                safe_print(f"  ✗ {Path(r['path']).name}: {r.get('error', '?')[:100]}",
                           file=sys.stderr)
            if i % 25 == 0 or i == len(futures):
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(missing) - i) / rate if rate > 0 else 0
                safe_print(
                    f"  [{i}/{len(missing)}] ok={ok} err={err}  "
                    f"{rate:.1f} pdf/s  ETA {int(eta)}s"
                )
    print()
    print(f"Terminé en {int(time.time() - t0)}s.")
    print(f"  Succès : {ok}")
    print(f"  Erreurs: {err}")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0,
                        help="Limite à N fichiers (0 = tout)")
    parser.add_argument("--smoke", action="store_true",
                        help="Smoke test : 5 fichiers seulement")
    parser.add_argument("--dry-run", action="store_true",
                        help="Liste seulement, pas d'appel API")
    args = parser.parse_args()
    sys.exit(main(args.profile, args.workers, args.limit,
                  args.smoke, args.dry_run))
