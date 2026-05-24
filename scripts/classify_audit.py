#!/usr/bin/env python3
"""Audit empirique de la cascade de classification.

Pour chaque fichier d'un sample, calcule INDÉPENDAMMENT :
  - N1 : classify_by_theme(theme, theme_mapping)
  - N2 : KeywordClassifier.classify(enriched_text)
  - N3 : LLMMapper.resolve(theme, title, filename)

Et écrit un CSV avec les 3 résultats côte-à-côte pour décider si une
cascade conditionnelle ("lancer N3 en parallèle quand N1 est suspect")
vaut la complexité ajoutée.

Pourquoi ce script et pas une commande klodo : c'est une mesure
one-off, pas un workflow récurrent. Une fois la décision archi prise,
on supprime le script (ou on le déplace dans review/).

Pas d'effet de bord : LLMMapper.resolve() met les résolutions dans
self.learned (dict en mémoire), mais on n'appelle JAMAIS
save_learned() — theme_mapping.yaml reste intact.

Usage::

    # Smoke 5 fichiers seulement (pour vérifier le pipeline)
    uv run python -m scripts.classify_audit --profile default --sample 5 --smoke

    # Run complet 500 fichiers (~17 min, ~$0.50)
    uv run python -m scripts.classify_audit --profile default --sample 500

    # Reproducible (seed fixe)
    uv run python -m scripts.classify_audit --profile default --sample 500 --seed 42
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.classifier import (  # noqa: E402
    _is_generic_fallback,
    classify_by_theme,
    classify_combined,
    load_keyword_classifier,
)
from lib.llm_mapper import LLMMapper  # noqa: E402
from lib.logger import get_logger, setup_logger  # noqa: E402
from lib.profile import Profile  # noqa: E402
from lib.vision_cache import compute_cache_key, load_cache, lookup  # noqa: E402

log = get_logger()


_SUPPORTED_EXTS = (".pdf", ".epub")


def _enumerate_audited_files(target: Path, vc: dict, model: str,
                             n_pages: int) -> list[tuple[Path, dict]]:
    """Walk the target, keep only files that have a vision_cache hit
    with a usable result (title + at least one theme). Returns pairs
    (abs_path, vision_result)."""
    out: list[tuple[Path, dict]] = []
    for root, dirs, files in os.walk(str(target)):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith(".") or not f.lower().endswith(_SUPPORTED_EXTS):
                continue
            abs_path = Path(root) / f
            key = compute_cache_key(str(abs_path), model=model, n_pages=n_pages)
            if not key:
                continue
            result = lookup(vc, key)
            if not isinstance(result, dict):
                continue
            title = (result.get("title") or "").strip()
            themes = result.get("themes") or []
            if not title or not themes:
                continue
            out.append((abs_path, result))
    return out


def _run_n2(classifier, title: str, theme: str, filename: str
            ) -> tuple[str | None, float, str]:
    """Mimics the KeywordClassifier path inside classify_combined."""
    if not classifier:
        return (None, 0.0, "")
    enriched = " ".join(p for p in (title, theme, filename) if p)
    try:
        results = classifier.classify(enriched, "")
    except Exception:
        return (None, 0.0, "")
    if not results:
        return (None, 0.0, "")
    best = results[0]
    if not best[0]:
        return (None, 0.0, "")
    return (
        best[0],
        float(best[1]) if len(best) > 1 else 0.0,
        str(best[2]) if len(best) > 2 else "",
    )


def _classify_for_audit(
    abs_path: Path,
    vision_result: dict,
    theme_mapping: dict,
    classifier,
    mapper,
) -> dict:
    """Compute N1, N2, N3 independently for ONE file. Returns a row dict
    ready for CSV writing. Each level is computed in isolation so we can
    see the raw verdict before the cascade picks a winner."""
    title = (vision_result.get("title") or "").strip()
    themes = vision_result.get("themes") or []
    top_theme = ""
    if themes:
        top_theme = (themes[0].get("theme") or "").strip()
    filename = abs_path.name

    # N1 — straight theme_mapping lookup on the top theme
    n1_path = classify_by_theme(top_theme, theme_mapping) or ""
    n1_score = float(vision_result.get("confidence") or 0.0) if n1_path else 0.0

    # N2 — keyword classifier on enriched text
    n2_path, n2_score, n2_keyword = _run_n2(
        classifier, title, top_theme, filename)
    n2_path = n2_path or ""

    # N3 — LLM Mapper. We call it INDEPENDENTLY of N1 to see what it would
    # have said, EVEN if N1 already had a hit. resolve() returns the path
    # or None. The internal self.learned dict gets populated but we never
    # save it → no side effect on theme_mapping.yaml.
    n3_path = ""
    n3_source = ""
    if mapper and top_theme:
        try:
            resolved = mapper.resolve(top_theme, title=title,
                                       filename=filename)
            if resolved:
                n3_path = resolved
                n3_source = "LLM (mapper)"
        except Exception as exc:
            n3_source = "ERROR: " + str(exc)[:80]

    # combined — what classify_combined() actually returns with the
    # full cascade + the new generic-fallback trigger enabled. This is
    # the value that the production reclassify would write. Comparing
    # combined_path to n1_path tells us if the trigger fired and what
    # it changed.
    try:
        combined_path, combined_score, combined_source = classify_combined(
            vision_result, filename, theme_mapping,
            classifier=classifier, llm_mapper=mapper,
            pdf_path=str(abs_path),
        )
        combined_path = combined_path or ""
    except Exception as exc:
        combined_path = ""
        combined_score = 0.0
        combined_source = "ERROR: " + str(exc)[:80]

    # Did the new trigger fire? Trigger only fires when:
    #   - N1 returned a generic fallback path
    #   - combined ended up returning a DIFFERENT path in the same section
    n1_is_generic = bool(n1_path) and _is_generic_fallback(n1_path)
    trigger_fired = (
        n1_is_generic
        and combined_path
        and combined_path != n1_path
        and "N3" in (combined_source or "")
    )

    # Specificity: count of path segments (folders deep)
    def _depth(p: str) -> int:
        return p.count("/") + 1 if p else 0

    n1_depth = _depth(n1_path)
    n2_depth = _depth(n2_path)
    n3_depth = _depth(n3_path)
    combined_depth = _depth(combined_path)

    # Quick verdict on N1 vs N3
    if not n1_path and not n3_path:
        n1_n3_status = "neither"
    elif not n1_path:
        n1_n3_status = "n3_only"
    elif not n3_path:
        n1_n3_status = "n1_only"
    elif n1_path == n3_path:
        n1_n3_status = "agree"
    elif n3_path.startswith(n1_path + "/"):
        n1_n3_status = "n3_more_specific"   # N3 a affiné N1
    elif n1_path.startswith(n3_path + "/"):
        n1_n3_status = "n1_more_specific"
    else:
        # Different paths, not parent/child
        n1_top = n1_path.split("/")[0]
        n3_top = n3_path.split("/")[0]
        if n1_top == n3_top:
            n1_n3_status = "same_section_different_subfolder"
        else:
            n1_n3_status = "disagree_top_section"

    return {
        "rel_path": str(abs_path),
        "title": title[:100],
        "top_theme": top_theme[:50],
        "n_themes": len(themes),
        "current_folder": str(abs_path.parent.relative_to(abs_path.parents[-1])),
        "n1_path": n1_path,
        "n1_score": round(n1_score, 3),
        "n1_depth": n1_depth,
        "n1_is_generic": int(n1_is_generic),
        "n2_path": n2_path,
        "n2_score": round(n2_score, 3),
        "n2_keyword": n2_keyword[:40],
        "n2_depth": n2_depth,
        "n3_path": n3_path,
        "n3_source": n3_source,
        "n3_depth": n3_depth,
        "n1_n3_status": n1_n3_status,
        # combined = ce que la production écrirait au reclassify, avec
        # le trigger N3-on-catch-all en place
        "combined_path": combined_path,
        "combined_source": combined_source,
        "combined_score": round(float(combined_score or 0), 3),
        "combined_depth": combined_depth,
        "trigger_fired": int(trigger_fired),
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Audit empirique de la cascade de classification — "
                    "compare N1/N2/N3 sur un sample pour décider si la "
                    "parallélisation conditionnelle vaut le coût.")
    p.add_argument("--profile", default="default",
                   help="Profil (défaut: default)")
    p.add_argument("--sample", type=int, default=500,
                   help="Nombre de fichiers à auditer (défaut: 500)")
    p.add_argument("--seed", type=int, default=42,
                   help="Seed RNG pour reproductibilité (défaut: 42)")
    p.add_argument("--smoke", action="store_true",
                   help="Mode smoke : 5 fichiers + résultats à l'écran")
    p.add_argument("--output", default=None,
                   help="Chemin CSV de sortie (défaut: review/classify-audit-<ts>.csv)")
    args = p.parse_args()

    setup_logger(verbose=False, log_file="logs/classify_audit.log")

    # Load profile
    profile = Profile(args.profile)
    target = Path(profile.target)
    if not target.exists():
        log.error("❌ Target introuvable : %s", target)
        return 1

    # Load vision_cache
    cache_path = Path(profile.cache_dir) / "vision_cache.json"
    if not cache_path.exists():
        log.error("❌ vision_cache.json introuvable : %s", cache_path)
        return 1
    vc = load_cache(cache_path)
    log.info("✓ vision_cache : %d entrées", len(vc))

    model = profile.llm_model
    n_pages = int(profile.defaults.get("pages", 2))

    # Enumerate eligible files (have a vision_cache hit with title + themes)
    log.info("📂 Énumération des fichiers avec metadata LLM exploitable…")
    eligible = _enumerate_audited_files(target, vc, model, n_pages)
    log.info("   %d fichiers éligibles", len(eligible))

    # Sample
    sample_size = 5 if args.smoke else args.sample
    sample_size = min(sample_size, len(eligible))
    rng = random.Random(args.seed)
    sample = rng.sample(eligible, sample_size)
    log.info("🎲 Sample (seed=%d) : %d fichiers", args.seed, sample_size)

    # Load classifiers
    log.info("⚙ Chargement des classifieurs…")
    cats_path = Path(profile.profile_dir) / "categories.yaml"
    classifier = (load_keyword_classifier(str(cats_path))
                  if cats_path.exists() else None)
    if not classifier:
        log.warning("⚠ KeywordClassifier non chargé — N2 ne tournera pas")

    api_key = os.environ.get("SILICONFLOW_API_KEY", "")
    if not api_key:
        # Fallback: read .env at the project root (the project doesn't
        # use python-dotenv; klodo.sh sources .env itself, but this
        # script runs directly via `python -m`).
        env_file = PROJECT_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == "SILICONFLOW_API_KEY":
                    api_key = v.strip().strip('"').strip("'")
                    break
    if not api_key:
        log.error("❌ SILICONFLOW_API_KEY non défini — pas de N3 possible")
        log.error("   Ajoute-le dans .env ou export SILICONFLOW_API_KEY=sk-xxx")
        return 1

    mapper = LLMMapper(
        folders=profile.tree,
        api_key=api_key,
        endpoint=profile.llm_endpoint,
        model=profile.llm_model,
        verbose=False,
        vision=False,
    )

    # Output path
    if args.output:
        out_path = Path(args.output)
    else:
        review_dir = PROJECT_ROOT / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = "smoke" if args.smoke else "full"
        out_path = review_dir / f"classify-audit-{ts}-{suffix}.csv"

    # Cost / time estimate
    est_seconds = sample_size * 2.0   # ~2s per LLM call sequential
    log.info("")
    log.info("⏱ Estimation : %d appels LLM × ~2s = %.0f min",
              sample_size, est_seconds / 60)
    log.info("💰 Coût estimé : ~$%.2f-%.2f",
              sample_size * 0.0001, sample_size * 0.001)
    log.info("📄 Output : %s", out_path)

    if not args.smoke:
        log.info("")
        log.info("⚠  ATTENTION : ce run va coûter de l'argent + prendre du temps.")
        log.info("   Lance d'abord avec --smoke (5 fichiers, ~10s, négligeable)")
        log.info("   pour vérifier que tout marche, puis enlève --smoke.")
        try:
            answer = input("Continuer ? [oui/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            log.info("Annulé.")
            return 0
        if answer not in ("oui", "o", "y", "yes"):
            log.info("Annulé.")
            return 0

    # Run
    log.info("")
    log.info("🚀 Audit en cours sur %d fichiers…", sample_size)
    t0 = time.perf_counter()
    rows: list[dict] = []
    n_errors = 0
    for i, (abs_path, vision_result) in enumerate(sample, start=1):
        try:
            row = _classify_for_audit(
                abs_path, vision_result, profile.theme_mapping,
                classifier, mapper)
            rows.append(row)
        except Exception as exc:
            log.warning("  ⚠ %s : %s", abs_path.name, exc)
            n_errors += 1
        if i % max(1, sample_size // 20) == 0 or i == sample_size:
            elapsed = time.perf_counter() - t0
            pct = 100 * i / sample_size
            rate = i / elapsed if elapsed > 0 else 0
            eta = (sample_size - i) / rate if rate > 0 else 0
            log.info("   [%3d/%3d] %5.1f%% · %.1f f/s · ETA %.0fs",
                      i, sample_size, pct, rate, eta)

    elapsed = time.perf_counter() - t0
    log.info("")
    log.info("✅ Terminé en %.1fs (%.1f f/s)", elapsed, len(rows) / elapsed)
    if n_errors:
        log.warning("   %d erreurs (voir log)", n_errors)

    # Write CSV
    if rows:
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        log.info("📄 CSV écrit : %s", out_path)

    # Summary
    log.info("")
    log.info("════ RÉSUMÉ ════")
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r["n1_n3_status"]] = statuses.get(r["n1_n3_status"], 0) + 1
    for status, count in sorted(statuses.items(), key=lambda x: -x[1]):
        pct = 100 * count / len(rows) if rows else 0
        log.info("  %-32s : %4d (%5.1f%%)", status, count, pct)

    # Interesting cases : N1 generic + N3 specific
    interesting = [r for r in rows if r["n1_n3_status"] == "n3_more_specific"]
    if interesting:
        log.info("")
        log.info("🔎 N3 a affiné N1 — vue brute (top 20) :")
        for r in interesting[:20]:
            log.info("  %s", r["title"][:50])
            log.info("    N1: %s", r["n1_path"])
            log.info("    N3: %s", r["n3_path"])
            log.info("    theme: %s", r["top_theme"])
            log.info("")

    # Effet du nouveau trigger (combined vs n1)
    n1_generic = [r for r in rows if r["n1_is_generic"]]
    fired = [r for r in rows if r["trigger_fired"]]
    log.info("")
    log.info("════ EFFET DU TRIGGER catch-all → N3 ════")
    log.info("  fichiers où N1 a tapé un catch-all  : %d / %d (%.1f%%)",
              len(n1_generic), len(rows),
              100 * len(n1_generic) / max(1, len(rows)))
    log.info("  trigger a effectivement upgradé     : %d / %d (%.1f%%)",
              len(fired), len(rows),
              100 * len(fired) / max(1, len(rows)))
    if n1_generic:
        log.info("  taux d'upgrade sur les catch-all   : %.1f%%",
                  100 * len(fired) / len(n1_generic))
    log.info("")
    if fired:
        log.info("📈 Trigger fired — cas où la prod va effectivement bouger :")
        for r in fired[:20]:
            log.info("  %s", r["title"][:50])
            log.info("    N1 (avant) : %s", r["n1_path"])
            log.info("    combined   : %s", r["combined_path"])
            log.info("    source     : %s", r["combined_source"])
            log.info("")

    # Mapper learned (en mémoire, non sauvé)
    if mapper and mapper.learned:
        log.info("📊 Le LLM Mapper aurait appris %d nouveaux thèmes "
                  "(non sauvegardé — audit uniquement).", len(mapper.learned))

    return 0


if __name__ == "__main__":
    sys.exit(main())
