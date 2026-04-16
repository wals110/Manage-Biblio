"""Benchmark mDeBERTa zero-shot vs theme_mapping.yaml ground truth.

Décision go/no-go avant d'intégrer mDeBERTa dans llm_mapper.

Usage:
    uv run python scripts/bench_mdeberta.py [--profile default] [--samples 50]
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from pathlib import Path

import yaml

# Repo root sur le path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# Mapping section technique -> description humaine sémantiquement riche
SECTION_HUMANIZE = {
    "01-SCIENCES": "science",
    "02-INFORMATIQUE": "computing and information technology",
    "03-INGENIERIE": "engineering",
    "04-SHS": "humanities and social sciences",
    "05-RELIGIONS": "religion",
    "06-MEDECINE": "medicine and healthcare",
    "07-LANGUES": "language learning",
    "08-LOISIRS": "hobbies and leisure",
    "09-BUSINESS": "business and management",
}


def humanize_folder(folder: str) -> str:
    """Transforme un chemin technique en description humaine multilingue.

    '01-SCIENCES/MATHEMATIQUES/08-Mathematiques-Generales'
        -> 'general mathematics (science)'
    '02-INFORMATIQUE/04-Genie-Logiciel'
        -> 'software engineering (computing and information technology)'
    """
    parts = folder.split("/")
    section = SECTION_HUMANIZE.get(parts[0], parts[0].lower())

    def clean(s: str) -> str:
        s = re.sub(r"^\d+-", "", s)
        return s.replace("-", " ").replace("_", " ").lower().strip()

    if len(parts) == 1:
        return section
    last = clean(parts[-1])
    if len(parts) == 2:
        return f"{last} ({section})"
    mid = clean(parts[1])
    return f"{last} — {mid} ({section})"


def load_dataset(profile: str) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Charge theme_mapping (théme -> dossier), liste de dossiers, et mapping folder->human."""
    root = Path(__file__).resolve().parents[1]
    mapping = yaml.safe_load(open(root / f"profiles/{profile}/theme_mapping.yaml"))
    tree = yaml.safe_load(open(root / f"profiles/{profile}/tree.yaml"))
    folders = [f for f in tree.get("folders", []) if not f.startswith("_")]
    human_map = {f: humanize_folder(f) for f in folders}
    return mapping, folders, human_map


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="default")
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--device", default="mps", choices=["cpu", "mps", "cuda"])
    args = parser.parse_args()

    print("Chargement du dataset...")
    mapping, folders, human_map = load_dataset(args.profile)
    human_to_folder = {v: k for k, v in human_map.items()}
    human_labels = list(human_map.values())
    print(f"  {len(mapping)} thèmes labellisés, {len(folders)} dossiers")
    print(f"  exemples humanisés :")
    for f in folders[:3]:
        print(f"    {f}")
        print(f"      -> {human_map[f]}")

    random.seed(42)
    sample = random.sample(list(mapping.items()), min(args.samples, len(mapping)))

    print(f"\nChargement mDeBERTa (device={args.device})...")
    t0 = time.time()
    from transformers import pipeline

    pipe = pipeline(
        "zero-shot-classification",
        model="MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7",
        device=args.device,
    )
    print(f"  Chargé en {time.time() - t0:.1f}s")

    print(f"\nBench sur {len(sample)} thèmes...")
    results = []
    t_start = time.time()
    for theme, expected in sample:
        t0 = time.time()
        out = pipe(
            f"This book is about {theme}",
            candidate_labels=human_labels,
            multi_label=False,
            hypothesis_template="This book is about {}.",
        )
        latency_ms = (time.time() - t0) * 1000
        top1_human = out["labels"][0]
        top1_score = out["scores"][0]
        top1 = human_to_folder.get(top1_human, top1_human)
        top3_humans = out["labels"][:3]
        top5_humans = out["labels"][:5]
        top10_humans = out["labels"][:10]
        top3 = [(human_to_folder.get(h, h), s) for h, s in zip(top3_humans, out["scores"][:3])]
        top5 = [human_to_folder.get(h, h) for h in top5_humans]
        top10 = [human_to_folder.get(h, h) for h in top10_humans]
        correct_top1 = top1 == expected
        correct_top3 = expected in [lab for lab, _ in top3]
        correct_top5 = expected in top5
        correct_top10 = expected in top10
        results.append({
            "theme": theme,
            "expected": expected,
            "top1": top1,
            "top1_score": top1_score,
            "correct_top1": correct_top1,
            "correct_top3": correct_top3,
            "correct_top5": correct_top5,
            "correct_top10": correct_top10,
            "latency_ms": latency_ms,
        })

    total_time = time.time() - t_start
    n = len(results)
    top1_acc = sum(r["correct_top1"] for r in results) / n
    top3_acc = sum(r["correct_top3"] for r in results) / n
    top5_acc = sum(r["correct_top5"] for r in results) / n
    top10_acc = sum(r["correct_top10"] for r in results) / n
    avg_latency = sum(r["latency_ms"] for r in results) / n

    # Accuracy par seuil de confidence
    thresholds = [0.5, 0.6, 0.7, 0.8, 0.85, 0.9]
    buckets = {t: {"n": 0, "correct": 0} for t in thresholds}
    for r in results:
        for t in thresholds:
            if r["top1_score"] >= t:
                buckets[t]["n"] += 1
                if r["correct_top1"]:
                    buckets[t]["correct"] += 1

    print("\n" + "=" * 60)
    print("RÉSULTATS")
    print("=" * 60)
    print(f"Échantillon            : {n} thèmes")
    print(f"Top-1 accuracy         : {top1_acc:.1%}  ({sum(r['correct_top1'] for r in results)}/{n})")
    print(f"Top-3 accuracy         : {top3_acc:.1%}")
    print(f"Top-5 accuracy         : {top5_acc:.1%}")
    print(f"Top-10 accuracy        : {top10_acc:.1%}")
    print(f"Latence moyenne        : {avg_latency:.0f} ms/call")
    print(f"Temps total            : {total_time:.1f}s ({n/total_time:.1f} req/s)")

    print("\nAccuracy par seuil (fast path):")
    print(f"{'seuil':>8} {'n cover':>10} {'coverage':>10} {'accuracy':>10}")
    for t in thresholds:
        b = buckets[t]
        if b["n"] > 0:
            acc = b["correct"] / b["n"]
            cov = b["n"] / n
            print(f"{t:>8.2f} {b['n']:>10} {cov:>10.1%} {acc:>10.1%}")
        else:
            print(f"{t:>8.2f} {b['n']:>10} {'0%':>10} {'n/a':>10}")

    # Erreurs représentatives
    errors = [r for r in results if not r["correct_top1"]]
    print(f"\nErreurs : {len(errors)}/{n}")
    for r in errors[:10]:
        print(f"  ✗ '{r['theme']}'  (score {r['top1_score']:.2f})")
        print(f"    attendu : {r['expected']}")
        print(f"    obtenu  : {r['top1']}")

    # Verdict
    print("\n" + "=" * 60)
    fast_path_85 = buckets[0.85]
    if fast_path_85["n"] > 0:
        fast_acc = fast_path_85["correct"] / fast_path_85["n"]
        fast_cov = fast_path_85["n"] / n
    else:
        fast_acc = 0
        fast_cov = 0

    if top1_acc >= 0.75 and fast_cov >= 0.4 and fast_acc >= 0.9:
        print("VERDICT : GO — mDeBERTa dépasse les critères")
    elif top1_acc >= 0.6:
        print("VERDICT : MARGINAL — envisager avec seuils plus conservateurs")
    else:
        print("VERDICT : NO-GO — précision insuffisante")
    print(f"  critère 1 : top-1 ≥ 75%          → {top1_acc:.1%}")
    print(f"  critère 2 : fast@0.85 coverage ≥ 40% → {fast_cov:.1%}")
    print(f"  critère 3 : fast@0.85 accuracy ≥ 90% → {fast_acc:.1%}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
