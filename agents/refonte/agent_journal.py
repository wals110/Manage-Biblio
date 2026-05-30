"""Journal append-only des mutations de l'agent Refonte Phase C.

Pattern : `lib/rename_journal.py` (rename-journal.jsonl) — append-only,
1 JSON par ligne, jamais d'écriture en place. Sert d'audit + de référence
pour les rollbacks.

Fichier : `profiles/<p>/.cache/refonte/agent-journal.jsonl`

Chaque entrée :
    {
      "ts": "2026-05-30T...",           # ISO UTC du moment de l'action
      "tool": "add_folder",             # nom de l'outil mutable invoqué
      "args": {"path": "..."},          # arguments de l'appel
      "result": "ok" | "error",
      "error": "...",                   # présent uniquement si result=error
      "batch_id": "<uuid4>",            # groupe les mutations d'un même tour
      "human_validated": true,          # confirmation explicite humaine
      "backup_dir": "agent-<batch_id>-<ts>",  # référence vers le snapshot
    }

Les entrées de rollback héritent du même format avec `tool="undo"` et
`args.target_batch_id` pointant vers le batch annulé.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dashboard import data


def _journal_path(profile: str) -> Path:
    return (
        data.get_project_root()
        / "profiles" / profile / ".cache" / "refonte" / "agent-journal.jsonl"
    )


def generate_batch_id() -> str:
    """UUID4 pour identifier un batch (= un tour de conversation user)."""
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def append_entry(
    profile: str,
    *,
    tool: str,
    args: dict[str, Any],
    result: str,
    batch_id: str,
    human_validated: bool = True,
    backup_dir: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Append une entrée au journal et retourne la struct écrite.

    Crée le dossier si absent. Atomique au niveau ligne (1 write).

    Args:
        profile: Nom du profil.
        tool: Identifiant de l'outil ("add_folder", "merge_folders", "undo"…).
        args: Arguments de l'appel — sérialisés JSON tel quel.
        result: "ok" ou "error".
        batch_id: UUID4 du batch (cf. generate_batch_id).
        human_validated: True si confirmation explicite humaine.
        backup_dir: Nom du dossier de snapshot pré-mutation (ou None pour undo).
        error: Message d'erreur si result == "error".

    Returns:
        L'entrée écrite (avec ts ajouté).

    Raises:
        ValueError: result invalide ou batch_id vide.
    """
    if result not in ("ok", "error"):
        raise ValueError(f"result must be 'ok' or 'error', got {result!r}")
    if not batch_id:
        raise ValueError("batch_id is required")
    entry: dict[str, Any] = {
        "ts": _now_iso(),
        "tool": tool,
        "args": args,
        "result": result,
        "batch_id": batch_id,
        "human_validated": human_validated,
    }
    if backup_dir is not None:
        entry["backup_dir"] = backup_dir
    if error is not None:
        entry["error"] = error
    path = _journal_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_journal(profile: str) -> list[dict[str, Any]]:
    """Lit toutes les entrées du journal (ordre d'écriture).

    Returns:
        Liste de dicts. Liste vide si pas de journal.
    """
    path = _journal_path(profile)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return entries


def list_entries(
    profile: str,
    *,
    batch_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Liste les N entrées les plus récentes, optionnellement filtrées
    par batch_id (utile pour afficher les mutations d'un batch précis).

    Returns:
        Entrées du plus récent au plus ancien, plafonnées à limit.
    """
    entries = read_journal(profile)
    if batch_id is not None:
        entries = [e for e in entries if e.get("batch_id") == batch_id]
    return list(reversed(entries[-limit:]))


def list_batches(profile: str) -> list[dict[str, Any]]:
    """Regroupe les entrées par batch_id et retourne un résumé par batch.

    Returns:
        Liste de dicts triée du batch le plus récent au plus ancien :
        ```
        [{
            "batch_id": "...",
            "ts_first": "...",          # ISO de la 1re entrée du batch
            "ts_last": "...",           # ISO de la dernière entrée du batch
            "n_entries": 3,             # total entrées du batch
            "n_ok": 3,                  # entrées result=ok
            "n_error": 0,               # entrées result=error
            "tools": ["add_folder",     # liste ordonnée des tools utilisés
                      "add_theme_mapping"],
            "backup_dir": "agent-...",  # 1er backup_dir trouvé dans le batch
            "rolled_back": false,       # True si un undo a déjà ciblé ce batch
        }, ...]
        ```
    """
    entries = read_journal(profile)
    by_batch: dict[str, dict[str, Any]] = {}
    rolled_back_ids: set[str] = set()

    for e in entries:
        # Détection des entrées d'undo : tool=="undo" pointant vers un batch
        if e.get("tool") == "undo":
            target = (e.get("args") or {}).get("target_batch_id")
            if target:
                rolled_back_ids.add(target)
            continue
        bid = e.get("batch_id")
        if not bid:
            continue
        slot = by_batch.setdefault(bid, {
            "batch_id": bid,
            "ts_first": e.get("ts"),
            "ts_last": e.get("ts"),
            "n_entries": 0,
            "n_ok": 0,
            "n_error": 0,
            "tools": [],
            "backup_dir": e.get("backup_dir"),
        })
        slot["ts_last"] = e.get("ts") or slot["ts_last"]
        slot["n_entries"] += 1
        if e.get("result") == "ok":
            slot["n_ok"] += 1
        elif e.get("result") == "error":
            slot["n_error"] += 1
        tool_name = e.get("tool") or "?"
        slot["tools"].append(tool_name)
        # Premier backup_dir non-None gagne (les suivants peuvent être None)
        if slot["backup_dir"] is None and e.get("backup_dir"):
            slot["backup_dir"] = e.get("backup_dir")

    for bid, slot in by_batch.items():
        slot["rolled_back"] = bid in rolled_back_ids

    # Tri : batch le plus récent en tête (par ts_last DESC)
    return sorted(
        by_batch.values(),
        key=lambda b: b.get("ts_last") or "",
        reverse=True,
    )


def record_undo(
    profile: str,
    target_batch_id: str,
    *,
    result: str = "ok",
    error: str | None = None,
) -> dict[str, Any]:
    """Helper pour journaliser un rollback (réutilise append_entry).

    Le batch_id de l'entrée undo est un NOUVEAU UUID4 — il ne réutilise
    pas le batch_id ciblé. Le lien se fait via args.target_batch_id.
    """
    return append_entry(
        profile,
        tool="undo",
        args={"target_batch_id": target_batch_id},
        result=result,
        batch_id=generate_batch_id(),
        human_validated=True,
        error=error,
    )
