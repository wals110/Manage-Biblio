"""Backup automatique des YAML de production avant chaque mutation
de l'agent Refonte Phase C.

Approche : full snapshot de `tree.yaml` + `theme_mapping.yaml` dans
`profiles/<p>/.cache/taxonomy-backups/agent-<batch_id>-<ts>/`. Pas de
diff incrémental — la simplicité prime sur la compacité (~quelques KB
par snapshot, négligeable).

Rotation : ne garde que les MAX_BACKUPS plus récents (défaut 50). Le
nettoyage cible UNIQUEMENT les snapshots agent-* (les backups manuels
d'autres systèmes restent intacts).

Pattern réutilisé du backup existant `theme_mapping.yaml` côté taxonomy.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dashboard import data

# Nombre maximum de snapshots agent gardés. Au-delà, les plus anciens
# (par ordre de timestamp dans le nom) sont supprimés.
MAX_BACKUPS_DEFAULT = 50

# Fichiers de production à snapshotter. Si un fichier est absent au moment
# du backup, on l'omet silencieusement — le restore le créera/n'écrasera
# que ceux présents dans le snapshot.
PROD_FILES = ("tree.yaml", "theme_mapping.yaml")


class BackupError(Exception):
    """Erreur côté backup/restore agent."""


def _backups_root(profile: str) -> Path:
    return (
        data.get_project_root()
        / "profiles" / profile / ".cache" / "taxonomy-backups"
    )


def _profile_root(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile


def _ts_for_filename() -> str:
    """Horodatage compact safe pour un nom de dossier."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _backup_dir_name(batch_id: str, ts: str | None = None) -> str:
    """Format canonique du nom de dossier agent : `agent-<batch_id>-<ts>`.

    Le batch_id est un UUID4 (cf. agent_journal.generate_batch_id).
    """
    ts = ts or _ts_for_filename()
    return f"agent-{batch_id}-{ts}"


def create_backup(
    profile: str,
    batch_id: str,
    *,
    max_keep: int = MAX_BACKUPS_DEFAULT,
) -> str:
    """Crée un snapshot des YAML de production et applique la rotation.

    Args:
        profile: Nom du profil.
        batch_id: UUID4 du batch courant.
        max_keep: Nombre maximum de backups agent à conserver après l'ajout.

    Returns:
        Le NOM du dossier créé (relatif à `taxonomy-backups/`) — à
        stocker dans le journal pour permettre le restore plus tard.

    Raises:
        BackupError: si aucun YAML de prod n'existe à snapshoter
                     (rien d'utile à backupper, on refuse).
    """
    if not batch_id:
        raise BackupError("batch_id is required")
    src_root = _profile_root(profile)
    dst_root = _backups_root(profile)
    dst_root.mkdir(parents=True, exist_ok=True)

    dir_name = _backup_dir_name(batch_id)
    dst_dir = dst_root / dir_name
    dst_dir.mkdir(parents=True, exist_ok=False)

    copied = 0
    for fname in PROD_FILES:
        src = src_root / fname
        if not src.exists():
            continue
        shutil.copy2(src, dst_dir / fname)
        copied += 1

    if copied == 0:
        # Rien à snapshotter — nettoie le dossier vide et raise
        dst_dir.rmdir()
        raise BackupError(
            f"no production YAML found in {src_root} — nothing to backup"
        )

    rotate_backups(profile, max_keep=max_keep)
    return dir_name


def restore_backup(profile: str, backup_dir_name: str) -> dict[str, Any]:
    """Restaure les YAML de production depuis un snapshot.

    Args:
        profile: Nom du profil.
        backup_dir_name: Nom du dossier de backup (cf. create_backup return).

    Returns:
        {"profile": ..., "backup": dir_name, "restored": [filename...]}

    Raises:
        BackupError: backup_dir absent ou vide.
    """
    src_dir = _backups_root(profile) / backup_dir_name
    if not src_dir.is_dir():
        raise BackupError(f"backup directory not found: {backup_dir_name!r}")
    profile_root = _profile_root(profile)
    restored: list[str] = []
    for fname in PROD_FILES:
        src = src_dir / fname
        if not src.exists():
            continue
        shutil.copy2(src, profile_root / fname)
        restored.append(fname)
    if not restored:
        raise BackupError(
            f"backup directory {backup_dir_name!r} is empty — nothing to restore"
        )
    return {
        "profile": profile,
        "backup": backup_dir_name,
        "restored": restored,
    }


def list_backups(profile: str) -> list[dict[str, Any]]:
    """Liste les backups agent du profil, triés du plus récent au plus ancien.

    Filtre uniquement les dossiers qui matchent le pattern `agent-<...>`.
    Les backups manuels d'autres systèmes (theme_mapping-<ts>.yaml en flat
    files par exemple) sont ignorés.

    Returns:
        Liste de dicts :
        ```
        [{
            "name": "agent-<batch_id>-<ts>",
            "batch_id": "...",          # extrait du nom
            "ts": "YYYYMMDD-HHMMSS",    # extrait du nom
            "files": ["tree.yaml", "theme_mapping.yaml"],
            "size_bytes": 12345,        # somme des fichiers du backup
        }, ...]
        ```
    """
    root = _backups_root(profile)
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in root.iterdir():
        if not entry.is_dir() or not entry.name.startswith("agent-"):
            continue
        parsed = _parse_backup_name(entry.name)
        if parsed is None:
            continue
        batch_id, ts = parsed
        files = sorted(p.name for p in entry.iterdir() if p.is_file())
        size = sum(p.stat().st_size for p in entry.iterdir() if p.is_file())
        out.append({
            "name": entry.name,
            "batch_id": batch_id,
            "ts": ts,
            "files": files,
            "size_bytes": size,
        })
    out.sort(key=lambda b: b["ts"], reverse=True)
    return out


def _parse_backup_name(name: str) -> tuple[str, str] | None:
    """Parse `agent-<batch_id>-<ts>` → (batch_id, ts).

    Le batch_id est un UUID4 contenant 4 tirets, donc on splitte par la
    droite : <ts> est le SUFFIXE après le dernier tiret-précédé-d'un-chiffre.
    En pratique le format de _ts_for_filename() est "YYYYMMDD-HHMMSS" qui
    contient lui aussi un tiret — il faut donc splitter sur le motif
    `-YYYYMMDD-HHMMSS` final.
    """
    if not name.startswith("agent-"):
        return None
    # Le timestamp est de la forme YYYYMMDD-HHMMSS = 15 chars + 1 tiret
    # → on isole les 16 derniers chars comme `-YYYYMMDD-HHMMSS`
    rest = name[len("agent-"):]
    if len(rest) < 17:  # uuid(36) + tiret(1) + ts(15) — au moins 17
        return None
    ts_suffix = rest[-15:]  # "YYYYMMDD-HHMMSS"
    if rest[-16] != "-":
        return None
    batch_id = rest[:-16]
    return batch_id, ts_suffix


def rotate_backups(
    profile: str,
    *,
    max_keep: int = MAX_BACKUPS_DEFAULT,
) -> int:
    """Supprime les backups agent les plus anciens au-delà de max_keep.

    Returns:
        Nombre de dossiers supprimés.
    """
    backups = list_backups(profile)
    if len(backups) <= max_keep:
        return 0
    to_drop = backups[max_keep:]
    root = _backups_root(profile)
    deleted = 0
    for b in to_drop:
        target = root / b["name"]
        try:
            shutil.rmtree(target)
            deleted += 1
        except OSError:
            continue
    return deleted


def find_backup_for_batch(profile: str, batch_id: str) -> str | None:
    """Retrouve le dossier de backup associé à un batch_id.

    Utile au rollback : on a juste le batch_id dans le journal, on doit
    localiser physiquement le snapshot.
    """
    for b in list_backups(profile):
        if b["batch_id"] == batch_id:
            return b["name"]
    return None
