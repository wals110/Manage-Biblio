"""Compute the classification baseline report for a given run.

Reads:
    profiles/<profile>/.cache/baseline/run-<id>/predictions.jsonl
    profiles/<profile>/.cache/baseline/run-<id>/disagreements.jsonl

Computes:
    - Overall accuracy = (agreements + adjudication-confirmed)
    - Per-class precision / recall / F1
    - Top confusion pairs
    - Disagreement breakdown by verdict
    - Comparison with previous run if available

Outputs:
    profiles/<profile>/.cache/baseline/run-<id>/report.md

Usage:
    uv run python scripts/compute_baseline.py --profile default
        [--run-id <id>] [--output <path>]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# ─── I/O helpers ──────────────────────────────────────────────────────────


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def list_runs(profile_dir: Path) -> list[Path]:
    """Return run directories newest-first."""
    base = profile_dir / ".cache" / "baseline"
    if not base.exists():
        return []
    return sorted(
        (d for d in base.iterdir() if d.is_dir() and d.name.startswith("run-")),
        reverse=True,
    )


# ─── Metric computation ───────────────────────────────────────────────────


def compute_metrics(predictions: list[dict], disagreements: list[dict]) -> dict:
    """Compute accuracy + per-class metrics.

    For each prediction:
    - If current == predicted (agreement) → counted as correct
    - If disagreement and verdict == klodo_right → predicted folder is truth
    - If disagreement and verdict == actual_right → current folder is truth
    - If disagreement and verdict == neither_right → ground_truth is truth
    - If disagreement and verdict == skip OR no verdict → excluded
    - If disagreement and not in sample → excluded (when sampling active)

    Returns dict with totals + per_class + confusion_pairs.
    """
    sample_active = any("in_sample" in d for d in disagreements)
    if sample_active:
        in_sample_ids = {d["file_id"] for d in disagreements if d.get("in_sample")}
    else:
        in_sample_ids = {d["file_id"] for d in disagreements}

    # Build verdict lookup (only sample-included records when sampling active)
    verdicts = {
        d["file_id"]: d
        for d in disagreements
        if d["file_id"] in in_sample_ids
    }

    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    confusion: Counter[tuple[str, str]] = Counter()

    correct = 0
    wrong = 0
    skipped = 0
    pending = 0

    for p in predictions:
        current = p["current_folder"]
        predicted = p["predicted_folder"]
        if current == predicted:
            # Agreement → considered correct
            correct += 1
            tp[predicted] += 1
            continue
        # Disagreement
        verdict_rec = verdicts.get(p["file_id"])
        if verdict_rec is None:
            pending += 1
            continue
        verdict = verdict_rec.get("verdict")
        if not verdict:
            pending += 1
            continue
        if verdict == "skip":
            skipped += 1
            continue
        truth = verdict_rec.get("ground_truth")
        if not truth:
            pending += 1
            continue

        if predicted == truth:
            correct += 1
            tp[predicted] += 1
        else:
            wrong += 1
            fp[predicted] += 1
            fn[truth] += 1
            confusion[(predicted, truth)] += 1

    classes = set(tp) | set(fp) | set(fn)
    per_class: list[dict] = []
    for c in sorted(classes):
        precision = tp[c] / (tp[c] + fp[c]) if (tp[c] + fp[c]) > 0 else 0.0
        recall = tp[c] / (tp[c] + fn[c]) if (tp[c] + fn[c]) > 0 else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )
        per_class.append({
            "class": c, "tp": tp[c], "fp": fp[c], "fn": fn[c],
            "precision": precision, "recall": recall, "f1": f1,
            "support": tp[c] + fn[c],
        })

    total_evaluated = correct + wrong
    accuracy = correct / total_evaluated if total_evaluated > 0 else 0.0

    # Verdict breakdown
    breakdown = Counter()
    for d in disagreements:
        v = d.get("verdict")
        if v:
            breakdown[v] += 1

    return {
        "n_files": len(predictions),
        "n_disagreements": len(disagreements),
        "n_agreements": len(predictions) - len(disagreements),
        "correct": correct,
        "wrong": wrong,
        "skipped": skipped,
        "pending": pending,
        "accuracy": accuracy,
        "verdict_breakdown": dict(breakdown),
        "per_class": per_class,
        "confusion_pairs": confusion.most_common(20),
    }


# ─── Report rendering ─────────────────────────────────────────────────────


def render_report(profile: str, run_meta: dict, metrics: dict, prev_metrics: dict | None) -> str:
    lines: list[str] = []
    lines.append("# Baseline classification — rapport")
    lines.append("")
    lines.append(f"**Profil** : `{profile}`  ")
    lines.append(f"**Run** : `{run_meta.get('run_id', '?')}`  ")
    lines.append(f"**Klodo version** : `{run_meta.get('klodo_version', '?')}`  ")
    lines.append(f"**Date du run** : {run_meta.get('created_at', '?')}  ")
    lines.append(f"**Fichiers évalués** : {metrics['n_files']} "
                 f"({metrics['n_agreements']} accords + {metrics['n_disagreements']} désaccords)  ")
    lines.append("")

    # ─── TL;DR ────────────────────────────────────────────────────
    lines.append("## TL;DR")
    lines.append("")
    if metrics["correct"] + metrics["wrong"] > 0:
        acc = metrics["accuracy"]
        emoji = "✅" if acc >= 0.85 else "⚠️" if acc >= 0.70 else "❌"
        delta = ""
        if prev_metrics and prev_metrics.get("accuracy") is not None:
            d = (acc - prev_metrics["accuracy"]) * 100
            sign = "+" if d >= 0 else ""
            delta = f" (Δ {sign}{d:.1f} pts vs run précédent)"
        lines.append(f"- {emoji} **Précision globale** : {acc:.1%}{delta}")
        lines.append(f"- **Corrects** : {metrics['correct']} "
                     f"(dont {metrics['n_agreements']} accords + "
                     f"{metrics['correct'] - metrics['n_agreements']} désaccords confirmés)")
        lines.append(f"- **Incorrects** : {metrics['wrong']}")
        if metrics["pending"] > 0:
            lines.append(f"- ⏳ **Désaccords non arbitrés** : {metrics['pending']}")
    else:
        lines.append("- ⏳ Aucun désaccord arbitré pour l'instant — précision non calculable.")
    lines.append("")

    # ─── Verdict breakdown ────────────────────────────────────────
    if metrics["verdict_breakdown"]:
        lines.append("## Répartition des arbitrages")
        lines.append("")
        lines.append("| Verdict | Nombre |")
        lines.append("|---|---:|")
        for v in ("klodo_right", "actual_right", "neither_right", "skip"):
            n = metrics["verdict_breakdown"].get(v, 0)
            label = {
                "klodo_right": "Klodo a raison (l'emplacement actuel est faux)",
                "actual_right": "Actuel a raison (Klodo se trompe)",
                "neither_right": "Aucun des deux (ground-truth saisie)",
                "skip": "Sauté (illisible / hors-scope)",
            }[v]
            lines.append(f"| {label} | {n} |")
        lines.append("")

    # ─── Per-class ────────────────────────────────────────────────
    if metrics["per_class"]:
        lines.append("## Précision / Rappel par classe")
        lines.append("")
        lines.append("Trié par F1 ascendant (les classes en haut méritent l'attention).")
        lines.append("")
        lines.append("| Classe | Support | Précision | Rappel | F1 | TP | FP | FN |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        rows = sorted(metrics["per_class"], key=lambda r: r["f1"])
        for row in rows:
            if row["support"] == 0 and row["fp"] == 0:
                continue
            lines.append(
                f"| `{row['class']}` | {row['support']} | "
                f"{row['precision']:.1%} | {row['recall']:.1%} | {row['f1']:.2f} | "
                f"{row['tp']} | {row['fp']} | {row['fn']} |"
            )
        lines.append("")

    # ─── Confusion pairs ──────────────────────────────────────────
    if metrics["confusion_pairs"]:
        lines.append("## Top confusions (Klodo prédit → en fait)")
        lines.append("")
        lines.append("| # | Klodo a dit | C'était en fait | Cas |")
        lines.append("|---|---|---|---:|")
        for i, ((pred, truth), count) in enumerate(metrics["confusion_pairs"], 1):
            lines.append(f"| {i} | `{pred}` | `{truth}` | {count} |")
        lines.append("")

    # ─── Recommandations ──────────────────────────────────────────
    lines.append("## Recommandations")
    lines.append("")
    recs: list[str] = []

    if metrics["per_class"]:
        worst = sorted(
            (r for r in metrics["per_class"] if r["support"] >= 2),
            key=lambda r: r["recall"],
        )[:3]
        if worst and worst[0]["recall"] < 0.7:
            classes = ", ".join(f"`{r['class']}`" for r in worst)
            recs.append(f"Améliorer le rappel sur les classes faibles : {classes}")

    if metrics["confusion_pairs"]:
        (pred, truth), cnt = metrics["confusion_pairs"][0]
        if cnt >= 3:
            recs.append(
                f"Investiguer la confusion fréquente `{pred}` → `{truth}` ({cnt} cas) — "
                "ajouter une règle de désambiguïsation ou un mot-clé spécifique"
            )

    if metrics["pending"] > 0:
        recs.append(f"Terminer l'arbitrage des {metrics['pending']} désaccords pendants "
                    "pour avoir des chiffres complets")

    if not recs:
        recs.append("Pas de recommandation forte. Si l'accuracy est satisfaisante, ce run sert "
                    "de baseline pour les prochaines améliorations de Klodo.")

    for i, rec in enumerate(recs, 1):
        lines.append(f"{i}. {rec}")
    lines.append("")

    # ─── Comparison ───────────────────────────────────────────────
    if prev_metrics is not None:
        lines.append("## Comparaison avec le run précédent")
        lines.append("")
        lines.append("| Métrique | Précédent | Actuel | Δ |")
        lines.append("|---|---:|---:|---:|")
        rows = [
            ("Précision globale", "accuracy", "{:.1%}", lambda a, b: f"{(b-a)*100:+.1f} pts"),
            ("Désaccords", "n_disagreements", "{}", lambda a, b: f"{b-a:+d}"),
            ("Corrects", "correct", "{}", lambda a, b: f"{b-a:+d}"),
            ("Incorrects", "wrong", "{}", lambda a, b: f"{b-a:+d}"),
        ]
        for label, key, fmt, dfn in rows:
            a = prev_metrics.get(key)
            b = metrics.get(key)
            if a is None or b is None:
                continue
            lines.append(f"| {label} | {fmt.format(a)} | {fmt.format(b)} | {dfn(a, b)} |")
        lines.append("")

    return "\n".join(lines)


# ─── Entrypoint ───────────────────────────────────────────────────────────


def main(profile: str, run_id: str | None, output: Path | None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    profile_dir = repo_root / "profiles" / profile

    runs = list_runs(profile_dir)
    if not runs:
        print(f"ERROR: no baseline runs for profile {profile!r}. "
              f"Run scripts/baseline_run.py first.", file=sys.stderr)
        return 2

    if run_id:
        run_dir = next((r for r in runs if r.name == f"run-{run_id}"), None)
        if run_dir is None:
            print(f"ERROR: run-{run_id} not found", file=sys.stderr)
            return 2
    else:
        run_dir = runs[0]

    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        print(f"ERROR: meta.json missing in {run_dir}", file=sys.stderr)
        return 2
    run_meta = json.loads(meta_path.read_text())

    predictions = load_jsonl(run_dir / "predictions.jsonl")
    disagreements = load_jsonl(run_dir / "disagreements.jsonl")

    print(f"Profile      : {profile}")
    print(f"Run          : {run_meta.get('run_id')}")
    print(f"Predictions  : {len(predictions)}")
    print(f"Disagreements: {len(disagreements)}")
    print()

    metrics = compute_metrics(predictions, disagreements)

    # Find a previous run for comparison
    prev_metrics = None
    if len(runs) > 1:
        prev_run_dir = next((r for r in runs if r != run_dir), None)
        if prev_run_dir:
            prev_predictions = load_jsonl(prev_run_dir / "predictions.jsonl")
            prev_disagreements = load_jsonl(prev_run_dir / "disagreements.jsonl")
            if prev_predictions:
                prev_metrics = compute_metrics(prev_predictions, prev_disagreements)

    report = render_report(profile, run_meta, metrics, prev_metrics)

    if output is None:
        output = run_dir / "report.md"
    output.write_text(report)

    print(f"Report écrit → {output.relative_to(repo_root)}")
    if metrics["correct"] + metrics["wrong"] > 0:
        print(f"  Précision : {metrics['accuracy']:.1%} ({metrics['correct']}/{metrics['correct']+metrics['wrong']})")
    if metrics["pending"] > 0:
        print(f"  Désaccords non arbitrés : {metrics['pending']}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--run-id", default=None,
                        help="Run ID (without 'run-' prefix). Default: latest.")
    parser.add_argument("--output", type=Path, default=None,
                        help="Override output path (default: run-dir/report.md)")
    args = parser.parse_args()
    sys.exit(main(args.profile, args.run_id, args.output))
