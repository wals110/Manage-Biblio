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

from agents.refonte.categories_llm import propose_keywords_for_new_folders
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
            # Skip les blocs de config (ex. `apprentissage` = dict, pas une
            # liste d'entries). Cf. lib/keyword_classifier.py:225.
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                chemin = entry.get("chemin", "")
                if chemin.startswith(prefix + "/") or chemin == prefix:
                    scores[groupe] = scores.get(groupe, 0) + 1
        if scores:
            # Groupe le plus représenté à ce niveau de préfixe
            return max(scores.items(), key=lambda kv: kv[1])[0]
    return "autres"


def _cascade_categories_changes(
    current_categories: dict[str, list[dict]],
    renamings: list[dict],
    fusions: list[dict],
    deletions: list[dict],
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Applique de manière déterministe renames/fusions/deletions sur les
    entries existantes de categories.yaml. Pas d'appel LLM.

    Retourne :
        - new_categories : structure YAML mise à jour
        - log_modifications : liste de dicts pour _render_rationale_markdown
    """
    # Copy défensive. Le vrai categories.yaml mélange des groupes-catégories
    # (list[dict]) et des blocs de config (ex. `apprentissage` = dict). On ne
    # cascade que les groupes-catégories ; les blocs config sont préservés
    # verbatim et ré-injectés à la fin. Cf. lib/keyword_classifier.py:223-225.
    preserved: dict[str, object] = {}
    new_categories: dict[str, list[dict]] = {}
    for groupe, entries in current_categories.items():
        if not isinstance(entries, list):
            preserved[groupe] = entries
            continue
        new_categories[groupe] = [dict(e) for e in entries if isinstance(e, dict)]
    log: list[dict] = []

    # Apply fusions FIRST : transformer chaque source en target dans les
    # chemins, le mécanisme de collision merge ensuite naturellement.
    fusion_map: dict[str, str] = {}
    for f in fusions:
        for src in f["sources"]:
            fusion_map[src] = f["target"]

    if fusion_map:
        for groupe, entries in new_categories.items():
            for entry in entries:
                chemin = entry.get("chemin", "")
                if chemin in fusion_map:
                    entry["chemin"] = fusion_map[chemin]

    rename_map = {r["old_path"]: r["new_path"] for r in renamings}

    for groupe, entries in new_categories.items():
        for entry in entries:
            chemin = entry.get("chemin", "")
            if chemin in rename_map:
                entry["chemin"] = rename_map[chemin]
                continue
            # Rename par préfixe : remplace old_path/* par new_path/*
            for old, new in rename_map.items():
                if chemin.startswith(old + "/"):
                    entry["chemin"] = new + chemin[len(old):]
                    entry["_was_prefix_renamed"] = (old, new)  # marqueur temp
                    break

    # Détection de collisions après rename : si 2 entries du même groupe ont
    # le même chemin, fusionner (dedup mots_cles case-insensitive + min
    # priorite). Log la collision.
    n_collisions_by_target: dict[str, int] = {}
    for groupe, entries in new_categories.items():
        by_chemin: dict[str, list[dict]] = {}
        for entry in entries:
            by_chemin.setdefault(entry.get("chemin", ""), []).append(entry)
        merged_entries: list[dict] = []
        for chemin, group in by_chemin.items():
            if len(group) == 1:
                merged_entries.append(group[0])
                continue
            # Collision : merge
            n_collisions_by_target[chemin] = (
                n_collisions_by_target.get(chemin, 0) + len(group) - 1
            )
            seen: dict[str, str] = {}  # lower → original
            for e in group:
                for k in e.get("mots_cles", []) or []:
                    if isinstance(k, str) and k.lower() not in seen:
                        seen[k.lower()] = k
            # Preserve internal marker '_was_prefix_renamed' if any of the
            # merged entries had it, so the subsequent log loop still records
            # the prefix-rename event.
            preserved_marker = next(
                (e.get("_was_prefix_renamed") for e in group
                 if e.get("_was_prefix_renamed") is not None),
                None,
            )
            merged = {
                "chemin": chemin,
                "priorite": min(int(e.get("priorite", 99)) for e in group),
                "mots_cles": list(seen.values()),
            }
            if preserved_marker is not None:
                merged["_was_prefix_renamed"] = preserved_marker
            merged_entries.append(merged)
        new_categories[groupe] = merged_entries

    for r in renamings:
        n_exact = sum(
            1
            for entries in new_categories.values()
            for e in entries
            if e.get("chemin") == r["new_path"]
        )
        if n_exact > 0:
            log.append({
                "type": "rename",
                "old": r["old_path"],
                "new": r["new_path"],
                "n_entries": n_exact,
            })

    # Enrichir le log : annoter n_collisions sur les entries rename
    for le in log:
        if le["type"] == "rename" and le["new"] in n_collisions_by_target:
            le["n_collisions"] = n_collisions_by_target[le["new"]]

    # Log fusions
    for f in fusions:
        n = sum(
            1
            for entries in new_categories.values()
            for e in entries
            if e.get("chemin") == f["target"]
        )
        if n > 0:
            log.append({
                "type": "fusion",
                "old": f["sources"],
                "new": f["target"],
                "n_entries": n,
            })

    for groupe, entries in new_categories.items():
        for entry in entries:
            marker = entry.pop("_was_prefix_renamed", None)
            if marker:
                old, new = marker
                log.append({
                    "type": "rename_prefix",
                    "old": old,
                    "new": new,
                    "chemin_renamed": entry["chemin"],
                    "n_entries": 1,
                })

    # Apply deletions : drop entries dont le chemin est dans la liste
    deletion_set = {d["path"] for d in deletions}
    if deletion_set:
        for groupe in list(new_categories.keys()):
            before = new_categories[groupe]
            after = [e for e in before if e.get("chemin") not in deletion_set]
            new_categories[groupe] = after

        for d in deletions:
            n = sum(
                1
                for entries in current_categories.values()
                for e in entries
                if e.get("chemin") == d["path"]
            )
            if n > 0:
                log.append({
                    "type": "deletion",
                    "old": d["path"],
                    "n_entries": n,
                })

    # Ré-injecte les blocs de config préservés (ex. `apprentissage`).
    new_categories.update(preserved)
    return new_categories, log


def _merge_categories_changes(
    intermediate: dict[str, list[dict]],
    new_entries: list[dict],
) -> dict[str, list[dict]]:
    """Combine la structure post-cascade avec les entries proposées par
    le LLM. Les nouvelles entries sont ajoutées dans leur groupe (créé si
    absent). Les champs `groupe` des new_entries sont consommés (le groupe
    est utilisé comme clé, pas conservé dans l'entry).
    """
    # Préserve les blocs de config non-liste (ex. `apprentissage`) verbatim.
    merged: dict[str, object] = {}
    for g, entries in intermediate.items():
        if not isinstance(entries, list):
            merged[g] = entries
            continue
        merged[g] = [dict(e) for e in entries if isinstance(e, dict)]
    for entry in new_entries:
        groupe = entry.get("groupe", "autres")
        # Ne jamais écraser un bloc config : si la cible n'est pas une liste,
        # bascule sur "autres" (cas théorique — l'inférence de groupe skippe
        # déjà les blocs config).
        if not isinstance(merged.get(groupe), list):
            if groupe in merged:
                groupe = "autres"
            merged.setdefault(groupe, [])
        merged[groupe].append({
            "chemin": entry["chemin"],
            "priorite": int(entry.get("priorite", 5)),
            "mots_cles": list(entry.get("mots_cles", [])),
        })
    return merged


def _render_categories_section(
    cascade_log: list[dict],
    new_entries: list[dict],
) -> str:
    """Render la section ## CATÉGORIES du rationale markdown."""
    if not cascade_log and not new_entries:
        return ""

    n_total = len(cascade_log) + len(new_entries)
    lines: list[str] = []
    lines.append(f"## CATÉGORIES ({n_total} entries modifiées)")
    lines.append("")

    if cascade_log:
        lines.append(f"### Cascades automatiques ({len(cascade_log)})")
        for entry in cascade_log:
            t = entry["type"]
            if t == "rename":
                extra = (f", {entry['n_collisions']} collision(s) mergée(s)"
                         if entry.get("n_collisions") else "")
                lines.append(
                    f"- **rename** : `{entry['old']}` → `{entry['new']}` "
                    f"({entry['n_entries']} entry remappée{extra})"
                )
            elif t == "rename_prefix":
                lines.append(
                    f"- **rename par préfixe** : `{entry['old']}/*` → "
                    f"`{entry['new']}/*` (entry : `{entry.get('chemin_renamed', '')}`)"
                )
            elif t == "fusion":
                srcs = " + ".join(f"`{s}`" for s in entry["old"])
                lines.append(
                    f"- **fusion** : {srcs} → `{entry['new']}` "
                    f"({entry['n_entries']} entry mergée, mots_cles dédupliqués)"
                )
            elif t == "deletion":
                lines.append(
                    f"- **deletion** : `{entry['old']}` "
                    f"({entry['n_entries']} entry supprimée)"
                )
        lines.append("")

    if new_entries:
        lines.append(f"### Nouveaux folders (mots-clés générés par LLM) ({len(new_entries)})")
        for entry in new_entries:
            chemin = entry["chemin"]
            groupe = entry["groupe"]
            priorite = entry["priorite"]
            mots = entry.get("mots_cles", [])
            if not mots:
                lines.append(
                    f"- `{chemin}` (groupe `{groupe}`, priorité {priorite}) "
                    "⚠ Mots-clés indisponibles (LLM) — à compléter manuellement"
                )
            else:
                lines.append(f"- `{chemin}` (groupe `{groupe}`, priorité {priorite})")
                lines.append(f"  - mots-clés : {', '.join(mots)}")
        lines.append("")

    return "\n".join(lines)


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

    # ─── Steps 2-5 : categories.yaml proposition ──────────────────────────
    cat_path_prod = tax._profile_dir(profile) / "categories.yaml"
    has_cascade_change = bool(
        changes.renamings or changes.fusions or changes.deletions
    )
    has_creation_change = bool(changes.creations)

    if cat_path_prod.exists() and (has_cascade_change or has_creation_change):
        try:
            current_cats = yaml.safe_load(
                cat_path_prod.read_text(encoding="utf-8")
            ) or {}
        except yaml.YAMLError:
            current_cats = {}

        # Step 2 : cascade déterministe
        new_cats, cascade_log = _cascade_categories_changes(
            current_cats,
            renamings=[r.model_dump() for r in changes.renamings],
            fusions=[f.model_dump() for f in changes.fusions],
            deletions=[d.model_dump() for d in changes.deletions],
        )

        # Step 3 : LLM mots-clés pour créations
        new_llm_entries: list[dict] = []
        if changes.creations:
            from agents.llm import get_agent_llm
            llm = get_agent_llm()
            existing_groupes = list(current_cats.keys())
            groupe_inference = {
                c.path: _groupe_from_path_prefix(c.path, current_cats)
                for c in changes.creations
            }
            sample_entries = {
                g: current_cats[g][:2] for g in existing_groupes
            }
            new_llm_entries = propose_keywords_for_new_folders(
                llm=llm,
                creations=[c.model_dump() for c in changes.creations],
                existing_groupes=existing_groupes,
                groupe_inference=groupe_inference,
                sample_entries=sample_entries,
            )

        # Step 4 : merge + écriture
        merged_cats = _merge_categories_changes(new_cats, new_llm_entries)
        cat_proposed_path = out_dir / "categories-proposed.yaml"
        cat_proposed_path.write_text(
            yaml.safe_dump(merged_cats, allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )

        # Step 5 : extend rationale markdown
        section = _render_categories_section(cascade_log, new_llm_entries)
        if section:
            rationale_path.write_text(
                rationale_path.read_text(encoding="utf-8") + "\n" + section,
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
