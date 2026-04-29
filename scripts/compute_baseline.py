"""Compute the classification baseline report from validated samples.

Reads the two JSONL files produced by the validation UI:
- profiles/<profile>/.cache/validation_sample.jsonl       (Mesure A)
- profiles/<profile>/.cache/validation_atrier_sample.jsonl (Mesure B)

Produces review/baseline-classification.md with:
- Overall accuracy + per-class precision / recall / F1
- Top confusion pairs (predicted X → actually Y)
- A-TRIER analysis (which folders Klodo should have picked)
- Recommendations on where to focus accuracy improvements

Usage:
    uv run python scripts/compute_baseline.py [--profile default]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ════════════════════════════════════════════════════════════════════════
#  Mesure A — confusion matrix on classified files
# ════════════════════════════════════════════════════════════════════════


def compute_classified_metrics(records: list[dict]) -> dict:
    """Compute accuracy + per-class P/R/F1 + confusion pairs from validated rows."""
    validated = [r for r in records if r.get("verdict") in ("correct", "wrong")]
    if not validated:
        return {"validated": 0, "skipped": 0}

    # Pairs (predicted, ground_truth)
    pairs = [
        (r["predicted_folder"], r.get("ground_truth") or r["predicted_folder"])
        for r in validated
    ]

    correct = sum(1 for p, t in pairs if p == t)
    wrong = sum(1 for p, t in pairs if p != t)
    accuracy = correct / len(pairs)

    # Per-class TP/FP/FN
    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    for predicted, truth in pairs:
        if predicted == truth:
            tp[truth] += 1
        else:
            fp[predicted] += 1   # predicted wrongly to this folder
            fn[truth] += 1       # truth was missed

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

    # Confusion pairs (predicted, truth) where they differ
    confusion: Counter[tuple[str, str]] = Counter()
    for predicted, truth in pairs:
        if predicted != truth:
            confusion[(predicted, truth)] += 1

    # Skip count for context
    skipped = sum(1 for r in records if r.get("verdict") == "skip")
    pending = sum(1 for r in records if not r.get("verdict"))

    return {
        "validated": len(pairs),
        "correct": correct,
        "wrong": wrong,
        "skipped": skipped,
        "pending": pending,
        "accuracy": accuracy,
        "per_class": per_class,
        "confusion_pairs": confusion.most_common(20),
    }


# ════════════════════════════════════════════════════════════════════════
#  Mesure B — diagnostic of A-TRIER abandons
# ════════════════════════════════════════════════════════════════════════


def compute_atrier_metrics(records: list[dict]) -> dict:
    """Analyze where A-TRIER files should have gone."""
    classified = [r for r in records if r.get("verdict") == "classified"]
    skipped = sum(1 for r in records if r.get("verdict") == "skip")
    pending = sum(1 for r in records if not r.get("verdict"))

    if not classified:
        return {"validated": 0, "skipped": skipped, "pending": pending}

    # Distribution: which folders were the "right answer" for A-TRIER files
    distribution = Counter(r["ground_truth"] for r in classified if r.get("ground_truth"))

    # Top-level section breakdown (e.g., 02-INFORMATIQUE)
    sections: Counter[str] = Counter()
    for folder in distribution:
        section = folder.split("/")[0]
        sections[section] += distribution[folder]

    return {
        "validated": len(classified) + skipped,
        "classified_count": len(classified),
        "skipped": skipped,
        "pending": pending,
        "distribution": distribution.most_common(),
        "by_section": sections.most_common(),
    }


# ════════════════════════════════════════════════════════════════════════
#  Report generation
# ════════════════════════════════════════════════════════════════════════


def render_report(profile: str, classified: dict, atrier: dict) -> str:
    """Render the markdown report."""
    lines: list[str] = []
    lines.append("# Baseline classification — rapport")
    lines.append("")
    lines.append(f"**Profil** : `{profile}`")
    lines.append(f"**Source** : `profiles/{profile}/.cache/validation_sample.jsonl` "
                 f"+ `validation_atrier_sample.jsonl`")
    lines.append("")

    # ─── TL;DR ────────────────────────────────────────────────────────
    lines.append("## TL;DR")
    lines.append("")
    if classified.get("validated", 0) > 0:
        acc = classified["accuracy"]
        emoji = "✅" if acc >= 0.85 else "⚠️" if acc >= 0.70 else "❌"
        lines.append(f"- {emoji} **Précision globale (Mesure A)** : "
                     f"{acc:.1%} sur {classified['validated']} fichiers validés "
                     f"(corrects: {classified['correct']}, incorrects: {classified['wrong']})")
    else:
        lines.append("- ⏳ **Mesure A** : aucune validation effectuée")

    if atrier.get("validated", 0) > 0:
        cls = atrier["classified_count"]
        skp = atrier["skipped"]
        lines.append(f"- 📥 **Diagnostic A-TRIER (Mesure B)** : "
                     f"{cls} fichiers classifiables, {skp} illisibles "
                     f"sur {atrier['validated']} validés")
    else:
        lines.append("- ⏳ **Mesure B** : aucune validation effectuée")
    lines.append("")

    # ─── Section A ────────────────────────────────────────────────────
    lines.append("## Mesure A — Précision sur les fichiers classés")
    lines.append("")
    if classified.get("validated", 0) == 0:
        lines.append("Aucune donnée. Lance la validation via le dashboard `/baseline`.")
    else:
        lines.append("### Vue d'ensemble")
        lines.append("")
        lines.append("| Métrique | Valeur |")
        lines.append("|---|---|")
        lines.append(f"| Fichiers validés | {classified['validated']} |")
        lines.append(f"| Corrects (Klodo a bien classé) | {classified['correct']} |")
        lines.append(f"| Incorrects | {classified['wrong']} |")
        lines.append(f"| Sautés | {classified['skipped']} |")
        lines.append(f"| Pending | {classified['pending']} |")
        lines.append(f"| **Accuracy** | **{classified['accuracy']:.1%}** |")
        lines.append("")

        # Per-class metrics
        lines.append("### Précision / Rappel par classe")
        lines.append("")
        lines.append("Trié par F1 ascendant (les classes en bas méritent l'attention).")
        lines.append("")
        lines.append("| Classe | Support | Précision | Rappel | F1 | TP | FP | FN |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for row in sorted(classified["per_class"], key=lambda r: r["f1"]):
            if row["support"] == 0 and row["fp"] == 0:
                continue
            lines.append(
                f"| `{row['class']}` | {row['support']} | "
                f"{row['precision']:.1%} | {row['recall']:.1%} | {row['f1']:.2f} | "
                f"{row['tp']} | {row['fp']} | {row['fn']} |"
            )
        lines.append("")

        # Confusion pairs
        if classified["confusion_pairs"]:
            lines.append("### Top confusions (predicted → actually)")
            lines.append("")
            lines.append("Quand Klodo se trompe, vers quoi il dérive le plus souvent.")
            lines.append("")
            lines.append("| # | Klodo a dit | C'était en fait | Cas |")
            lines.append("|---|---|---|---:|")
            for i, ((predicted, truth), count) in enumerate(classified["confusion_pairs"], 1):
                lines.append(f"| {i} | `{predicted}` | `{truth}` | {count} |")
            lines.append("")

    lines.append("")

    # ─── Section B ────────────────────────────────────────────────────
    lines.append("## Mesure B — Diagnostic A-TRIER")
    lines.append("")
    if atrier.get("validated", 0) == 0:
        lines.append("Aucune donnée. Lance la validation via le dashboard `/baseline?mode=atrier`.")
    else:
        lines.append(f"Sur {atrier['validated']} fichiers de `_A-TRIER` validés :")
        lines.append("")
        lines.append(f"- {atrier['classified_count']} **auraient pu être classés** "
                     f"(le classifier a abandonné à tort)")
        lines.append(f"- {atrier['skipped']} **vraiment illisibles** "
                     f"(couverture absente, scan corrompu, etc.)")
        lines.append("")

        if atrier["by_section"]:
            lines.append("### Répartition par section")
            lines.append("")
            lines.append("Section où ces fichiers auraient dû atterrir :")
            lines.append("")
            lines.append("| Section | Nombre |")
            lines.append("|---|---:|")
            for section, count in atrier["by_section"]:
                lines.append(f"| `{section}` | {count} |")
            lines.append("")

        if atrier["distribution"]:
            lines.append("### Détail par dossier")
            lines.append("")
            lines.append("| Dossier | Nombre |")
            lines.append("|---|---:|")
            for folder, count in atrier["distribution"][:20]:
                lines.append(f"| `{folder}` | {count} |")
            lines.append("")

    lines.append("")

    # ─── Recommandations ──────────────────────────────────────────────
    lines.append("## Recommandations")
    lines.append("")
    recs: list[str] = []

    if classified.get("validated", 0) > 0:
        # Find worst classes
        worst = sorted(
            (r for r in classified["per_class"] if r["support"] >= 2),
            key=lambda r: r["recall"],
        )[:5]
        if worst:
            worst_list = ", ".join(f"`{r['class']}`" for r in worst[:3])
            recs.append(f"Améliorer la classification sur les classes faibles : {worst_list}")

        # Find frequent confusion targets
        if classified["confusion_pairs"]:
            top_confusion = classified["confusion_pairs"][0]
            (pred, truth), cnt = top_confusion
            if cnt >= 3:
                recs.append(
                    f"Investiguer la confusion fréquente `{pred}` ↔ `{truth}` ({cnt} cas) — "
                    "peut-être ajouter une règle de désambiguïsation ou un mot-clé spécifique"
                )

    if atrier.get("classified_count", 0) > 0 and atrier.get("by_section"):
        top_sec, top_count = atrier["by_section"][0]
        recs.append(
            f"Klodo abandonne sur {top_count} fichiers qui auraient dû aller dans `{top_sec}` — "
            "renforcer le mapping ou le keyword classifier sur cette section"
        )

    if not recs:
        recs.append("Pas assez de données pour des recommandations spécifiques. "
                    "Termine la validation, puis relance ce script.")

    for i, rec in enumerate(recs, 1):
        lines.append(f"{i}. {rec}")

    lines.append("")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════
#  Entrypoint
# ════════════════════════════════════════════════════════════════════════


def main(profile: str, output: Path | None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    cache_dir = repo_root / "profiles" / profile / ".cache"
    classified_path = cache_dir / "validation_sample.jsonl"
    atrier_path = cache_dir / "validation_atrier_sample.jsonl"

    classified_records = load_jsonl(classified_path)
    atrier_records = load_jsonl(atrier_path)

    if not classified_records and not atrier_records:
        print("ERROR: no JSONL data found. Run build_validation_sample.py "
              "and build_atrier_sample.py first.", file=sys.stderr)
        return 2

    print(f"Profile         : {profile}")
    print(f"Mesure A source : {classified_path.relative_to(repo_root)} "
          f"({len(classified_records)} records)")
    print(f"Mesure B source : {atrier_path.relative_to(repo_root)} "
          f"({len(atrier_records)} records)")
    print()

    classified = compute_classified_metrics(classified_records)
    atrier = compute_atrier_metrics(atrier_records)

    report = render_report(profile, classified, atrier)

    if output is None:
        output = repo_root / "review" / "baseline-classification.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report)

    print(f"Rapport écrit → {output.relative_to(repo_root)}")
    if classified.get("accuracy") is not None:
        print(f"  Précision Mesure A : {classified['accuracy']:.1%} "
              f"sur {classified['validated']} validés")
    if atrier.get("classified_count", 0) > 0:
        print(f"  A-TRIER classifiables : {atrier['classified_count']} / {atrier['validated']}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--output", type=Path, default=None,
                        help="Override output path")
    args = parser.parse_args()
    sys.exit(main(args.profile, args.output))
