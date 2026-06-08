"""Outils write-only-to-side-YAMLs pour l'agent Refonte — Phase B.

Le tool principal `propose_changes(...)` reçoit du LLM une description structurée
des changements à appliquer (créations / fusions / renommages / mappings ajoutés)
et produit 3 artefacts dans `profile/<name>/.cache/refonte/<run_id>/proposed/` :

  - tree-proposed.yaml          (arborescence cible)
  - theme_mapping-proposed.yaml (mapping enrichi)
  - refonte-rationale.md        (récap des décisions, par catégorie)

Aucun de ces 3 fichiers ne touche aux YAMLs de production. Phase B reste
strictement "side YAMLs" — c'est Phase C qui appliquera (avec backup) si
l'utilisateur valide.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from dashboard import data
from dashboard import taxonomy as tax

# ─── Schémas Pydantic exposés au LLM via StructuredTool ───────────────────


class _Creation(BaseModel):
    path: str = Field(description="Nouveau chemin relatif (ex: '02-INFORMATIQUE/03-Langages-Programmation/Rust')")
    rationale: str = Field(description="Pourquoi créer ce dossier (1-2 phrases)")


class _Fusion(BaseModel):
    sources: list[str] = Field(description="Liste des dossiers actuels à fusionner (chemins relatifs)")
    target: str = Field(description="Nouveau chemin cible (existant ou créé)")
    rationale: str = Field(description="Pourquoi fusionner (1-2 phrases)")


class _Renaming(BaseModel):
    old_path: str = Field(description="Ancien chemin relatif")
    new_path: str = Field(description="Nouveau chemin relatif")
    rationale: str = Field(description="Pourquoi renommer (1-2 phrases)")


class _Deletion(BaseModel):
    path: str = Field(description="Chemin relatif du dossier à supprimer (sans le fusionner)")
    rationale: str = Field(
        description=(
            "Pourquoi supprimer ce dossier (vide depuis longtemps, doublon "
            "tranchant, obsolète, etc.). Préférer une `fusion` si les fichiers "
            "doivent migrer ailleurs."
        ),
    )


class _MappingAdded(BaseModel):
    theme: str = Field(description="Thème LLM observé (ex: 'rust programming')")
    folder: str = Field(description="Dossier cible (existant ou créé dans les créations)")
    rationale: str = Field(description="Pourquoi mapper ce thème ici (1-2 phrases)")


class _ProposeChangesInput(BaseModel):
    """Arguments du tool propose_changes — structure des changements proposés."""

    creations: list[_Creation] = Field(default_factory=list, description="Nouveaux dossiers à créer")
    fusions: list[_Fusion] = Field(default_factory=list, description="Dossiers à fusionner")
    renamings: list[_Renaming] = Field(default_factory=list, description="Dossiers à renommer")
    deletions: list[_Deletion] = Field(
        default_factory=list,
        description="Dossiers à supprimer purement (sans fusion)",
    )
    mappings_added: list[_MappingAdded] = Field(
        default_factory=list,
        description="Nouveaux mappings thème → dossier à ajouter dans theme_mapping.yaml",
    )


# ─── Helpers privés ────────────────────────────────────────────────────────


def _groupe_from_path_prefix(
    path: str,
    existing_categories: dict[str, list[dict]],
) -> str:
    """Infère le groupe d'un nouveau chemin à partir des préfixes des
    entries existantes. Si un préfixe path correspond à un groupe (le plus
    représenté en cas d'ambiguïté), retourne ce groupe. Sinon "autres".
    """
    # Compte, pour chaque groupe, combien d'entries partagent un préfixe
    # avec `path` (par segment, du plus long au plus court).
    parts = path.split("/")
    for n_segments in range(len(parts), 0, -1):
        prefix = "/".join(parts[:n_segments])
        scores: dict[str, int] = {}
        for groupe, entries in existing_categories.items():
            for entry in entries:
                chemin = entry.get("chemin", "")
                if chemin.startswith(prefix + "/") or chemin == prefix:
                    scores[groupe] = scores.get(groupe, 0) + 1
        if scores:
            # Groupe le plus représenté à ce niveau de préfixe
            return max(scores.items(), key=lambda kv: kv[1])[0]
    return "autres"


def _proposal_dir(profile: str, run_id: str) -> Path:
    """Renvoie le dossier proposed/ pour ce run (créé si absent)."""
    base = data.get_project_root() / "profiles" / profile / ".cache" / "refonte" / run_id / "proposed"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _apply_changes_to_tree(
    current_folders: list[str],
    creations: list[dict],
    fusions: list[dict],
    renamings: list[dict],
    deletions: list[dict] | None = None,
) -> list[str]:
    """Construit la liste des dossiers du tree proposé.

    Ordre d'application : creations → renamings → fusions → deletions
    (deletions en dernier pour pouvoir cibler un path qui aurait été créé
    ou renommé juste avant ; cas d'usage rare mais cohérent).
    """
    folders = set(current_folders)
    # Créations
    for c in creations:
        folders.add(c["path"])
    # Renommages (remplace l'ancien par le nouveau)
    for r in renamings:
        folders.discard(r["old_path"])
        folders.add(r["new_path"])
    # Fusions : enlève les sources, ajoute la cible (déjà ajoutée si c'était une création)
    for f in fusions:
        for src in f["sources"]:
            folders.discard(src)
        folders.add(f["target"])
    # Suppressions sèches (sans réaffectation des fichiers)
    for d in (deletions or []):
        folders.discard(d["path"])
    return sorted(folders)


def _apply_changes_to_mapping(
    current_mapping: dict[str, str],
    mappings_added: list[dict],
    renamings: list[dict],
    fusions: list[dict],
    deletions: list[dict] | None = None,
) -> dict[str, str]:
    """Construit le mapping proposé : ajoute les nouveaux mappings + ajuste les
    cibles existantes pour les renommages et les fusions + drop les mappings
    qui pointent sur un dossier supprimé.
    """
    mapping = dict(current_mapping)  # copy
    # Renommages : tous les thèmes qui pointaient sur old_path pointent maintenant sur new_path
    rename_map = {r["old_path"]: r["new_path"] for r in renamings}
    # Fusions : tous les thèmes qui pointaient sur une source pointent maintenant sur la target
    fusion_map: dict[str, str] = {}
    for f in fusions:
        for src in f["sources"]:
            fusion_map[src] = f["target"]
    # Suppressions : les thèmes pointant dessus deviennent orphelins (drop)
    deleted_paths = {d["path"] for d in (deletions or [])}
    for theme, folder in list(mapping.items()):
        if folder in rename_map:
            mapping[theme] = rename_map[folder]
        elif folder in fusion_map:
            mapping[theme] = fusion_map[folder]
        elif folder in deleted_paths:
            del mapping[theme]
    # Nouveaux mappings (s'ajoutent en dernier, écrasent un éventuel mapping existant)
    for m in mappings_added:
        mapping[m["theme"]] = m["folder"]
    return mapping


def _render_rationale_markdown(
    profile: str,
    changes: _ProposeChangesInput,
) -> str:
    """Render le markdown récapitulatif des changements proposés."""
    lines: list[str] = []
    lines.append(f"# Refonte taxonomy — profil `{profile}` — proposition")
    lines.append("")
    n_creations = len(changes.creations)
    n_fusions = len(changes.fusions)
    n_renamings = len(changes.renamings)
    n_deletions = len(changes.deletions)
    n_mappings = len(changes.mappings_added)
    lines.append("## Résumé des changements")
    lines.append(f"- **Créations** : {n_creations}")
    lines.append(f"- **Fusions** : {n_fusions}")
    lines.append(f"- **Renommages** : {n_renamings}")
    lines.append(f"- **Suppressions** : {n_deletions}")
    lines.append(f"- **Mappings ajoutés** : {n_mappings}")
    lines.append("")
    if changes.creations:
        lines.append(f"## CRÉATIONS ({n_creations})")
        for c in changes.creations:
            lines.append(f"- `{c.path}` — {c.rationale}")
        lines.append("")
    if changes.fusions:
        lines.append(f"## FUSIONS ({n_fusions})")
        for f in changes.fusions:
            srcs = " + ".join(f"`{s}`" for s in f.sources)
            lines.append(f"- {srcs} → `{f.target}`")
            lines.append(f"  - {f.rationale}")
        lines.append("")
    if changes.renamings:
        lines.append(f"## RENOMMAGES ({n_renamings})")
        for r in changes.renamings:
            lines.append(f"- `{r.old_path}` → `{r.new_path}` — {r.rationale}")
        lines.append("")
    if changes.deletions:
        lines.append(f"## SUPPRESSIONS ({n_deletions})")
        for d in changes.deletions:
            lines.append(f"- `{d.path}` — {d.rationale}")
        lines.append("")
    if changes.mappings_added:
        lines.append(f"## MAPPINGS AJOUTÉS ({n_mappings})")
        for m in changes.mappings_added:
            lines.append(f"- `{m.theme}` → `{m.folder}` — {m.rationale}")
        lines.append("")
    return "\n".join(lines)


# ─── Tool principal ────────────────────────────────────────────────────────


def propose_changes(
    profile: str,
    run_id: str,
    creations: list[dict] | None = None,
    fusions: list[dict] | None = None,
    renamings: list[dict] | None = None,
    deletions: list[dict] | None = None,
    mappings_added: list[dict] | None = None,
) -> dict[str, Any]:
    """Écrit les 3 artefacts de proposition sur disque (read tree+mapping
    actuels, applique les changements à un copy, écrit les YAMLs proposés).

    À appeler **une seule fois** par run de Phase B. Le tool valide la structure
    via Pydantic et retourne les chemins absolus écrits + un récap.

    Args:
        profile: Nom du profil cible (ex. "default").
        run_id: UUID du run de Phase B (déterminé par le caller, pas par le LLM).
        creations: Liste de {path, rationale}.
        fusions: Liste de {sources: list, target, rationale}.
        renamings: Liste de {old_path, new_path, rationale}.
        mappings_added: Liste de {theme, folder, rationale}.

    Returns:
        Dict avec les chemins écrits + métriques :
            {
                "tree_proposed_path": str,
                "mapping_proposed_path": str,
                "rationale_path": str,
                "n_creations": int,
                "n_fusions": int,
                "n_renamings": int,
                "n_mappings_added": int,
                "n_folders_after": int,
                "n_mappings_after": int,
            }
    """
    # Validation Pydantic (raise ValidationError si malformé)
    changes = _ProposeChangesInput(
        creations=creations or [],
        fusions=fusions or [],
        renamings=renamings or [],
        deletions=deletions or [],
        mappings_added=mappings_added or [],
    )
    out_dir = _proposal_dir(profile, run_id)

    # Lit l'existant + applique
    current_folders = tax._load_tree(profile)
    current_mapping = tax._load_mapping(profile)

    new_folders = _apply_changes_to_tree(
        current_folders,
        [c.model_dump() for c in changes.creations],
        [f.model_dump() for f in changes.fusions],
        [r.model_dump() for r in changes.renamings],
        deletions=[d.model_dump() for d in changes.deletions],
    )
    new_mapping = _apply_changes_to_mapping(
        current_mapping,
        [m.model_dump() for m in changes.mappings_added],
        [r.model_dump() for r in changes.renamings],
        [f.model_dump() for f in changes.fusions],
        deletions=[d.model_dump() for d in changes.deletions],
    )

    # Écritures
    tree_path = out_dir / "tree-proposed.yaml"
    mapping_path = out_dir / "theme_mapping-proposed.yaml"
    rationale_path = out_dir / "refonte-rationale.md"
    changes_json_path = out_dir / "changes.json"

    tree_path.write_text(
        yaml.safe_dump({"folders": new_folders}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    mapping_path.write_text(
        yaml.safe_dump(new_mapping, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )
    rationale_path.write_text(
        _render_rationale_markdown(profile, changes),
        encoding="utf-8",
    )
    # Persist aussi la structure raw — utile pour B.2 (simulateur) et debug
    changes_json_path.write_text(
        json.dumps(changes.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "tree_proposed_path": str(tree_path),
        "mapping_proposed_path": str(mapping_path),
        "rationale_path": str(rationale_path),
        "n_creations": len(changes.creations),
        "n_fusions": len(changes.fusions),
        "n_renamings": len(changes.renamings),
        "n_deletions": len(changes.deletions),
        "n_mappings_added": len(changes.mappings_added),
        "n_folders_after": len(new_folders),
        "n_mappings_after": len(new_mapping),
    }


# ─── LangChain binding ────────────────────────────────────────────────────


def make_proposition_tools(profile: str, run_id: str) -> list[StructuredTool]:
    """Factory : construit les tools mutables Phase B bindés à un run précis.

    Pattern factory pour cacher `profile` et `run_id` au LLM (qui pourrait
    sinon halluciner de mauvaises valeurs ou écraser un autre run). La graph
    builder appelle cette factory en début de run pour générer les tools
    spécifiques à cette session.

    Args:
        profile: Nom du profil ciblé.
        run_id: UUID du run de Phase B en cours.

    Returns:
        Liste de StructuredTool prêts à `llm.bind_tools()`. Pour l'instant un
        seul tool — `propose_changes` — mais on garde la liste pour évolution.
    """

    def _propose_changes_bound(
        creations: list[dict] | None = None,
        fusions: list[dict] | None = None,
        renamings: list[dict] | None = None,
        deletions: list[dict] | None = None,
        mappings_added: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Écrit les artefacts de proposition (tree-proposed.yaml +
        theme_mapping-proposed.yaml + refonte-rationale.md) à partir des
        changements proposés. À appeler **une seule fois** par run.

        Args:
            creations: Liste de {path, rationale} — dossiers à créer.
            fusions: Liste de {sources, target, rationale} — dossiers à fusionner.
            renamings: Liste de {old_path, new_path, rationale} — dossiers à renommer.
            deletions: Liste de {path, rationale} — dossiers à supprimer SANS fusion.
                       Utiliser pour les dossiers vides obsolètes ou les doublons
                       tranchants. Préférer `fusions` si les fichiers doivent migrer.
            mappings_added: Liste de {theme, folder, rationale} — nouveaux mappings.
        """
        return propose_changes(
            profile=profile,
            run_id=run_id,
            creations=creations,
            fusions=fusions,
            renamings=renamings,
            deletions=deletions,
            mappings_added=mappings_added,
        )

    # Pour que LangChain expose le bon name + doc au LLM
    _propose_changes_bound.__name__ = "propose_changes"
    return [StructuredTool.from_function(_propose_changes_bound)]
