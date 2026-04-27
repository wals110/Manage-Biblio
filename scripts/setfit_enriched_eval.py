"""SetFit mapper — Étape 0 : split stratifié seul (baseline corrigée).

Variante de notebooks/01-setfit-mapper-exploration.ipynb avec une correction
méthodologique : split stratifié avec les classes à 1 exemple gardées en
train uniquement (exclues de val).

Note : une première version ajoutait les tokens du chemin du folder dans
l'input (ex. "nlp | informatique ia ml nlp" → 02-INFORMATIQUE/05-IA-ML/NLP).
Résultat : top-1 = 100%. C'était du **data leakage** — en production, on
n'a pas le folder au moment de la prédiction (c'est ce qu'on prédit). Le
vrai enrichissement légitime viendra à l'étape 1 via les filenames du SSD.

Objectif ici : mesurer l'impact de la stratification seule. Gain attendu
modeste (+2 à +5 pts vs 43%) car on reste avec un input ultra-court.

Usage: uv run --group ml python scripts/setfit_enriched_eval.py [--profile default]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import yaml
from datasets import Dataset
from setfit import SetFitModel, Trainer, TrainingArguments
from sklearn.model_selection import train_test_split

RANDOM_SEED = 42
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def clean_theme(theme: str) -> str:
    """Normalize theme for input (lowercase, strip, no folder info)."""
    return theme.lower().strip()


def build_stratified_split(
    pairs: list[tuple[str, str]], seed: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Split pairs into (train, val) keeping one-sample classes in train only.

    Returns:
      train: 80% of multi-sample classes + 100% of one-sample classes
      val:   20% of multi-sample classes (every class in val exists in train)
    """
    counts = Counter(folder for _, folder in pairs)
    multi = [(t, f) for t, f in pairs if counts[f] >= 2]
    singletons = [(t, f) for t, f in pairs if counts[f] == 1]

    labels = [f for _, f in multi]
    train_multi, val_multi = train_test_split(
        multi,
        test_size=0.2,
        random_state=seed,
        stratify=labels,
    )

    train = train_multi + singletons
    val = val_multi
    return train, val


def top_k_accuracy(probas: np.ndarray, expected: list[str], labels: list[str], k: int) -> float:
    correct = 0
    for i, exp in enumerate(expected):
        top_k_idx = np.argsort(-probas[i])[:k]
        top_k_labels = [labels[j] for j in top_k_idx]
        if exp in top_k_labels:
            correct += 1
    return correct / len(expected)


def main(profile: str) -> int:
    random.seed(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)

    repo_root = Path(__file__).resolve().parent.parent
    profile_dir = repo_root / "profiles" / profile
    mapping_path = profile_dir / "theme_mapping.yaml"
    tree_path = profile_dir / "tree.yaml"

    print(f"=== SetFit Étape 0 — input enrichi + split stratifié ===")
    print(f"Profil : {profile}")
    print(f"Seed   : {RANDOM_SEED}")
    print()

    # --- Chargement ---
    mapping = yaml.safe_load(mapping_path.read_text())
    tree = yaml.safe_load(tree_path.read_text())
    folders_in_tree = {f for f in tree.get("folders", []) if not f.startswith("_")}
    folders_in_mapping = set(mapping.values())
    ghost_folders = folders_in_mapping - folders_in_tree

    print(f"Mappings         : {len(mapping)}")
    print(f"Dossiers (tree)  : {len(folders_in_tree)}")
    print(f"Dossiers (mapping): {len(folders_in_mapping)}")
    print(f"Dossiers fantômes (mapping ∉ tree) : {len(ghost_folders)}")
    print()

    # --- Normalisation input (pas d'enrichissement légitime possible ici :
    # le folder ne peut pas être injecté car c'est le label à prédire) ---
    pairs = [(clean_theme(theme), folder) for theme, folder in mapping.items()]
    sample_sizes = [len(text.split()) for text, _ in pairs]
    print(f"Longueur texte input : min={min(sample_sizes)}, max={max(sample_sizes)}, avg={np.mean(sample_sizes):.1f}")
    print(f"Exemple : {pairs[0][0]!r} → {pairs[0][1]}")
    print()

    # --- Split stratifié ---
    train_pairs, val_pairs = build_stratified_split(pairs, RANDOM_SEED)
    train_classes = {f for _, f in train_pairs}
    val_classes = {f for _, f in val_pairs}
    print(f"Train : {len(train_pairs)} exemples, {len(train_classes)} classes")
    print(f"Val   : {len(val_pairs)} exemples, {len(val_classes)} classes")
    print(f"Classes val ∉ train : {len(val_classes - train_classes)} (doit être 0)")
    print()

    # --- Training ---
    train_ds = Dataset.from_dict(
        {"text": [t for t, _ in train_pairs], "label": [f for _, f in train_pairs]}
    )
    val_ds = Dataset.from_dict(
        {"text": [t for t, _ in val_pairs], "label": [f for _, f in val_pairs]}
    )

    print(f"Chargement du modèle {MODEL_NAME!r}...")
    t0 = time.time()
    model = SetFitModel.from_pretrained(MODEL_NAME)
    print(f"  chargé en {time.time() - t0:.1f}s")

    training_args = TrainingArguments(
        batch_size=16,
        num_epochs=1,
        num_iterations=20,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
    )

    print("\nTraining SetFit...")
    t0 = time.time()
    trainer.train()
    training_time = time.time() - t0
    print(f"  terminé en {training_time:.1f}s ({training_time/60:.1f} min)")
    print()

    # --- Évaluation ---
    val_texts = [t for t, _ in val_pairs]
    val_expected = [f for _, f in val_pairs]

    t0 = time.time()
    probas = model.predict_proba(val_texts)
    latency_ms = (time.time() - t0) / len(val_texts) * 1000
    probas_np = np.asarray(probas)

    labels_idx = model.labels if hasattr(model, "labels") else None
    if labels_idx is None:
        labels_idx = sorted(train_classes)

    top1 = top_k_accuracy(probas_np, val_expected, labels_idx, 1)
    top3 = top_k_accuracy(probas_np, val_expected, labels_idx, 3)
    top5 = top_k_accuracy(probas_np, val_expected, labels_idx, 5)
    top10 = top_k_accuracy(probas_np, val_expected, labels_idx, 10)

    # Sweet spot analysis
    top1_scores = probas_np.max(axis=1)
    top1_preds = [labels_idx[i] for i in probas_np.argmax(axis=1)]
    correct = np.array([p == e for p, e in zip(top1_preds, val_expected)])

    thresholds = np.arange(0.1, 1.01, 0.05)
    coverage_at = []
    accuracy_at = []
    for th in thresholds:
        mask = top1_scores >= th
        coverage_at.append(float(mask.sum() / len(top1_scores)))
        accuracy_at.append(float(correct[mask].mean()) if mask.sum() > 0 else 0.0)

    sweet_90_30 = any(a >= 0.90 and c >= 0.30 for a, c in zip(accuracy_at, coverage_at))
    sweet_85_40 = any(a >= 0.85 and c >= 0.40 for a, c in zip(accuracy_at, coverage_at))

    # --- Verdict ---
    print("=" * 60)
    print("RÉSULTATS ÉTAPE 0")
    print("=" * 60)
    print(f"Top-1 accuracy     : {top1:.1%}")
    print(f"Top-3 accuracy     : {top3:.1%}")
    print(f"Top-5 accuracy     : {top5:.1%}")
    print(f"Top-10 accuracy    : {top10:.1%}")
    print(f"Latence / call     : {latency_ms:.1f} ms")
    print(f"Training time      : {training_time:.1f}s")
    print()
    print(f"Sweet spot @ 90/30 : {'oui' if sweet_90_30 else 'non'}")
    print(f"Sweet spot @ 85/40 : {'oui' if sweet_85_40 else 'non'}")
    print()

    v1_top1 = 0.432  # reference from notebook v1
    print(f"vs baseline v1 (single-word input, random split): {top1 - v1_top1:+.1%} pts")
    print()

    if top1 >= 0.70:
        verdict = "GO"
        msg = "fast-path viable — continuer vers production"
    elif top1 >= 0.50:
        verdict = "PIVOT"
        msg = f"fast-path insuffisant mais pre-filter top-5 ({top5:.1%}) utilisable"
    elif top1 >= 0.55:
        verdict = "CONTINUE"
        msg = "améliorations data supplémentaires justifiées (étape 1)"
    else:
        verdict = "NO-GO"
        msg = "enrichissement trivial insuffisant — étape 1 probablement nécessaire"

    print(f"VERDICT ÉTAPE 0 : {verdict}")
    print(f"  → {msg}")
    print("=" * 60)

    # --- Persist results for downstream steps ---
    results_path = repo_root / "review" / "setfit-step0-results.json"
    results_path.parent.mkdir(exist_ok=True)
    results_path.write_text(
        json.dumps(
            {
                "profile": profile,
                "model": MODEL_NAME,
                "random_seed": RANDOM_SEED,
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
                "ghost_folders_count": len(ghost_folders),
                "verdict": verdict,
            },
            indent=2,
        )
    )
    print(f"\nRésultats écrits → {results_path.relative_to(repo_root)}")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default", help="Profil à utiliser (default: default)")
    args = parser.parse_args()
    sys.exit(main(args.profile))
