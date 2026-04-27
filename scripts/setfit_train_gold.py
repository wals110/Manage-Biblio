"""SetFit training on gold dataset (étape 1).

Loads profiles/<profile>/.cache/setfit_dataset.jsonl built by
build_setfit_dataset.py, samples a balanced subset per class, trains
SetFit, and evaluates on a stratified held-out val set.

Strategy:
- Cap N samples per class to keep training tractable (default 40)
- Stratified 80/20 split on multi-sample classes, one-sample stays in train
- Evaluates top-k + sweet spot vs confidence threshold

Usage: uv run --group ml python scripts/setfit_train_gold.py
          [--profile default] [--per-class 40] [--iterations 10]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from datasets import Dataset
from setfit import SetFitModel, Trainer, TrainingArguments
from sklearn.model_selection import train_test_split

RANDOM_SEED = 42
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def load_jsonl(path: Path) -> list[tuple[str, str]]:
    pairs = []
    for line in path.read_text().splitlines():
        d = json.loads(line)
        pairs.append((d["text"], d["label"]))
    return pairs


def balanced_subsample(
    pairs: list[tuple[str, str]], per_class: int, seed: int
) -> list[tuple[str, str]]:
    """Keep at most `per_class` samples per class (random)."""
    rng = random.Random(seed)
    by_class: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for p in pairs:
        by_class[p[1]].append(p)
    out = []
    for cls, items in by_class.items():
        if len(items) > per_class:
            out.extend(rng.sample(items, per_class))
        else:
            out.extend(items)
    return out


def build_stratified_split(
    pairs: list[tuple[str, str]], seed: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    counts = Counter(folder for _, folder in pairs)
    multi = [(t, f) for t, f in pairs if counts[f] >= 2]
    singletons = [(t, f) for t, f in pairs if counts[f] == 1]
    labels = [f for _, f in multi]
    train_multi, val_multi = train_test_split(
        multi, test_size=0.2, random_state=seed, stratify=labels
    )
    return train_multi + singletons, val_multi


def top_k_accuracy(probas: np.ndarray, expected: list[str], labels: list[str], k: int) -> float:
    correct = 0
    for i, exp in enumerate(expected):
        top_k_idx = np.argsort(-probas[i])[:k]
        if exp in [labels[j] for j in top_k_idx]:
            correct += 1
    return correct / len(expected)


def main(profile: str, per_class: int, iterations: int) -> int:
    random.seed(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)

    repo_root = Path(__file__).resolve().parent.parent
    dataset_path = repo_root / "profiles" / profile / ".cache" / "setfit_dataset.jsonl"
    if not dataset_path.exists():
        print(
            f"ERROR: dataset not found at {dataset_path}. Run build_setfit_dataset.py first.",
            file=sys.stderr,
        )
        return 2

    print("=" * 60)
    print("SetFit Étape 1 — training sur gold dataset SSD")
    print("=" * 60)
    print(f"Dataset : {dataset_path.relative_to(repo_root)}")
    print(f"Per-class cap : {per_class}")
    print(f"Num iterations : {iterations}")
    print(f"Seed : {RANDOM_SEED}")
    print()

    full_pairs = load_jsonl(dataset_path)
    print(f"Dataset complet : {len(full_pairs)} exemples, {len({f for _, f in full_pairs})} classes")

    # Balanced subsample
    pairs = balanced_subsample(full_pairs, per_class, RANDOM_SEED)
    counts = Counter(f for _, f in pairs)
    print(f"Après sous-échantillonnage : {len(pairs)} exemples, {len(counts)} classes")
    print(f"  min/max/avg per class : {min(counts.values())}/{max(counts.values())}/{sum(counts.values())/len(counts):.1f}")
    print()

    train_pairs, val_pairs = build_stratified_split(pairs, RANDOM_SEED)
    train_classes = {f for _, f in train_pairs}
    val_classes = {f for _, f in val_pairs}
    print(f"Train : {len(train_pairs)} exemples, {len(train_classes)} classes")
    print(f"Val   : {len(val_pairs)} exemples, {len(val_classes)} classes")
    print(f"Classes val ∉ train : {len(val_classes - train_classes)}")
    print()

    train_ds = Dataset.from_dict(
        {"text": [t for t, _ in train_pairs], "label": [f for _, f in train_pairs]}
    )
    val_ds = Dataset.from_dict(
        {"text": [t for t, _ in val_pairs], "label": [f for _, f in val_pairs]}
    )

    print(f"Chargement encoder {MODEL_NAME!r}...")
    t0 = time.time()
    model = SetFitModel.from_pretrained(MODEL_NAME)
    print(f"  chargé en {time.time() - t0:.1f}s")

    args = TrainingArguments(
        batch_size=32,
        num_epochs=1,
        num_iterations=iterations,
    )
    trainer = Trainer(
        model=model, args=args, train_dataset=train_ds, eval_dataset=val_ds
    )

    print("\nTraining SetFit...")
    t0 = time.time()
    trainer.train()
    training_time = time.time() - t0
    print(f"  terminé en {training_time:.1f}s ({training_time/60:.1f} min)")
    print()

    # Eval
    val_texts = [t for t, _ in val_pairs]
    val_expected = [f for _, f in val_pairs]

    t0 = time.time()
    probas = model.predict_proba(val_texts)
    latency_ms = (time.time() - t0) / len(val_texts) * 1000
    probas_np = np.asarray(probas)

    labels_idx = model.labels if hasattr(model, "labels") else sorted(train_classes)

    top1 = top_k_accuracy(probas_np, val_expected, labels_idx, 1)
    top3 = top_k_accuracy(probas_np, val_expected, labels_idx, 3)
    top5 = top_k_accuracy(probas_np, val_expected, labels_idx, 5)
    top10 = top_k_accuracy(probas_np, val_expected, labels_idx, 10)

    # Sweet spot
    top1_scores = probas_np.max(axis=1)
    top1_preds = [labels_idx[i] for i in probas_np.argmax(axis=1)]
    correct = np.array([p == e for p, e in zip(top1_preds, val_expected)])

    thresholds = np.arange(0.1, 1.01, 0.05)
    sweet_points = []
    for th in thresholds:
        mask = top1_scores >= th
        cov = float(mask.sum() / len(top1_scores))
        acc = float(correct[mask].mean()) if mask.sum() > 0 else 0.0
        sweet_points.append((float(th), acc, cov))

    sweet_90_30 = any(a >= 0.90 and c >= 0.30 for _, a, c in sweet_points)
    sweet_85_40 = any(a >= 0.85 and c >= 0.40 for _, a, c in sweet_points)
    sweet_80_50 = any(a >= 0.80 and c >= 0.50 for _, a, c in sweet_points)

    print("=" * 60)
    print("RÉSULTATS ÉTAPE 1")
    print("=" * 60)
    print(f"Top-1 accuracy     : {top1:.1%}")
    print(f"Top-3 accuracy     : {top3:.1%}")
    print(f"Top-5 accuracy     : {top5:.1%}")
    print(f"Top-10 accuracy    : {top10:.1%}")
    print(f"Latence / call     : {latency_ms:.1f} ms")
    print(f"Training time      : {training_time:.1f}s ({training_time/60:.1f} min)")
    print()
    print(f"Sweet spot @ acc≥90% cov≥30% : {'oui' if sweet_90_30 else 'non'}")
    print(f"Sweet spot @ acc≥85% cov≥40% : {'oui' if sweet_85_40 else 'non'}")
    print(f"Sweet spot @ acc≥80% cov≥50% : {'oui' if sweet_80_50 else 'non'}")
    print()

    # Show best (acc, cov) points
    print("Trade-off (seuils à surveiller) :")
    for target_acc in [0.95, 0.90, 0.85, 0.80]:
        feasible = [(t, a, c) for t, a, c in sweet_points if a >= target_acc and c >= 0.1]
        if feasible:
            t, a, c = min(feasible, key=lambda x: x[0])
            print(f"  acc ≥ {target_acc:.0%} : seuil={t:.2f}, coverage={c:.1%}, acc réelle={a:.1%}")
        else:
            print(f"  acc ≥ {target_acc:.0%} : aucun seuil viable")
    print()

    if top1 >= 0.70:
        verdict = "GO"
        msg = "fast-path viable — SetFit peut remplacer llm_mapper"
    elif top1 >= 0.55 and sweet_80_50:
        verdict = "PIVOT-SOLIDE"
        msg = "pre-filter top-5 très robuste → LLM sur 5 candidats, -85% tokens"
    elif top1 >= 0.50:
        verdict = "PIVOT-LIGHT"
        msg = "pre-filter utilisable mais gain modeste vs complexité ajoutée"
    else:
        verdict = "NO-GO"
        msg = "même avec 16k exemples SetFit ne suffit pas — abandon du chantier"

    print(f"VERDICT ÉTAPE 1 : {verdict}")
    print(f"  → {msg}")
    print("=" * 60)

    results_path = repo_root / "review" / "setfit-step1-results.json"
    results_path.write_text(
        json.dumps(
            {
                "profile": profile,
                "model": MODEL_NAME,
                "random_seed": RANDOM_SEED,
                "per_class_cap": per_class,
                "num_iterations": iterations,
                "dataset_size": len(full_pairs),
                "subsampled_size": len(pairs),
                "train_samples": len(train_pairs),
                "val_samples": len(val_pairs),
                "train_classes": len(train_classes),
                "val_classes": len(val_classes),
                "training_time_s": training_time,
                "latency_ms_per_call": latency_ms,
                "top1": top1,
                "top3": top3,
                "top5": top5,
                "top10": top10,
                "sweet_spot_90_30": sweet_90_30,
                "sweet_spot_85_40": sweet_85_40,
                "sweet_spot_80_50": sweet_80_50,
                "verdict": verdict,
            },
            indent=2,
        )
    )
    print(f"\nRésultats écrits → {results_path.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument(
        "--per-class", type=int, default=40, help="Max samples per class (default: 40)"
    )
    parser.add_argument(
        "--iterations", type=int, default=10, help="SetFit num_iterations (default: 10)"
    )
    args = parser.parse_args()
    sys.exit(main(args.profile, args.per_class, args.iterations))
