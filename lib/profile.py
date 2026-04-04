"""
Profile management module for Klodo project.

Handles loading and managing profile configurations from YAML files.
Each profile contains configuration for target library, LLM settings, and classification rules.
"""

import os
import sys
from pathlib import Path
from typing import Any

import yaml

from lib.logger import setup_logger, get_logger
from lib.exceptions import ConfigError


def get_project_root() -> Path:
    """
    Find the project root directory.

    Searches upwards from the current file location or script location for a directory
    containing both 'profiles/' and 'lib/' subdirectories.

    Returns:
        Path: The project root directory.

    Raises:
        RuntimeError: If project root cannot be found.
    """
    # Start from the directory of this file
    current = Path(__file__).parent.parent.absolute()

    # Search up to 3 levels for project root
    for _ in range(3):
        if (current / "profiles").exists() and (current / "lib").exists():
            return current
        current = current.parent

    # Fallback to cwd if not found
    cwd = Path.cwd()
    if (cwd / "profiles").exists() and (cwd / "lib").exists():
        return cwd

    raise ConfigError(
        "Cannot find project root. Searched for 'profiles/' and 'lib/' directories."
    )


PROFILES_DIR = "profiles"


class Profile:
    """
    Manages a profile configuration for the Klodo project.

    A profile consists of:
    - profile.yaml: Basic config (name, description, target, inbox, LLM settings)
    - tree.yaml: Folder structure (list of paths)
    - theme_mapping.yaml: Theme → folder path mappings
    - refinement.yaml: Refinement rules (parent folder → target folder refinements)
    - categories.yaml: Keyword-based classification config

    Attributes:
        name (str): Profile name.
        description (str): Profile description.
        target (str): Path to the target library (BIBLIO_V2).
        inbox (str): Path to the inbox folder (_A-TRIER).
        llm_provider (str): LLM provider (siliconflow or ollama).
        llm_model (str): LLM model name.
        llm_endpoint (str): LLM API endpoint.
        defaults (Dict): Default settings (workers, min_confidence, etc.).
        theme_mapping (Dict[str, str]): Theme → path mappings.
        refinement_rules (List[Dict]): List of refinement rules.
        tree (List[str]): List of folder paths in the library structure.
        categories (Dict): Raw YAML keyword classification config.
    """

    def __init__(self, name: str = "default") -> None:
        """
        Initialize and load a profile.

        Args:
            name (str): Profile name. Defaults to "default".

        Raises:
            FileNotFoundError: If profile directory does not exist.
            ValueError: If required YAML files are missing or malformed.
        """
        self.name = name
        self._project_root = get_project_root()
        self._profile_dir = self._project_root / PROFILES_DIR / name
        self.profile_dir = self._profile_dir

        if not self._profile_dir.exists():
            raise ConfigError(
                f"Profile directory not found: {self._profile_dir}"
            )

        # Load all YAML files
        self._load_profile_yaml()
        self._load_tree_yaml()
        self._load_theme_mapping_yaml()
        self._load_refinement_yaml()
        self._load_categories_yaml()

    def _load_yaml(self, filename: str) -> dict[str, Any]:
        """
        Load a YAML file from the profile directory.

        Args:
            filename (str): Name of the YAML file (e.g., "profile.yaml").

        Returns:
            Dict[str, Any]: Parsed YAML content.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If YAML is malformed.
        """
        filepath = self._profile_dir / filename

        if not filepath.exists():
            raise ConfigError(
                f"Profile file not found: {filepath}"
            )

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data if data is not None else {}
        except yaml.YAMLError as e:
            raise ConfigError(
                f"Malformed YAML in {filepath}: {e}"
            ) from e

    def _load_profile_yaml(self) -> None:
        """Load profile.yaml and set basic attributes."""
        data = self._load_yaml("profile.yaml")

        self.description = data.get("description", "")
        self.target = data.get("target", "")
        self.inbox = data.get("inbox", "")
        self.fallback = data.get("fallback", "_A-TRIER")

        # LLM config is nested under 'llm:' key
        llm = data.get("llm", {})
        self.llm_provider = llm.get("provider", "siliconflow")
        self.llm_model = llm.get("model", "Qwen3-VL-8B")
        self.llm_endpoint = llm.get("endpoint", "https://api.siliconflow.com/v1/chat/completions")

        self.defaults = data.get("defaults", {})

    def _load_tree_yaml(self) -> None:
        """Load tree.yaml and set tree attribute."""
        data = self._load_yaml("tree.yaml")
        # tree.yaml has key 'folders:', not 'tree:'
        self.tree = data.get("folders", []) if data else []

    def _load_theme_mapping_yaml(self) -> None:
        """Load theme_mapping.yaml and set theme_mapping attribute."""
        data = self._load_yaml("theme_mapping.yaml")
        # theme_mapping.yaml is a flat dict (key: value), not nested
        if isinstance(data, dict):
            self.theme_mapping = data
        else:
            self.theme_mapping = {}

    def _load_refinement_yaml(self) -> None:
        """Load refinement.yaml and set refinement_rules attribute."""
        data = self._load_yaml("refinement.yaml")
        # refinement.yaml has key 'rules:', not 'refinement_rules:'
        self.refinement_rules = data.get("rules", []) if data else []

    def _load_categories_yaml(self) -> None:
        """Load categories.yaml and set categories attribute."""
        self.categories = self._load_yaml("categories.yaml")

    def get_folder_path(self, theme: str) -> str | None:
        """
        Get the target folder path for a given theme.

        Checks theme_mapping first, then falls back to tree lookup.

        Args:
            theme (str): Theme name.

        Returns:
            Optional[str]: Folder path if found, None otherwise.
        """
        if theme in self.theme_mapping:
            return self.theme_mapping[theme]
        return None

    def validate(self, check_dirs=True) -> list[str]:
        """
        Valide la configuration du profil.

        Vérifie que les chemins existent et que la configuration est cohérente.

        Args:
            check_dirs: Si True, vérifie l'existence des répertoires sur le disque.

        Returns:
            list[str]: Liste des erreurs trouvés (vide si tout est OK).
        """
        errors: list[str] = []

        # Vérifications de configuration
        if not self.target:
            errors.append("'target' non défini dans le profil")
        if not self.inbox:
            errors.append("'inbox' non défini dans le profil")
        if not self.llm_model:
            errors.append("'llm.model' non défini dans le profil")
        if not self.llm_endpoint:
            errors.append("'llm.endpoint' non défini dans le profil")

        # Vérifications de cohérence inbox vs target
        if self.target and self.inbox:
            if os.path.realpath(self.inbox) == os.path.realpath(self.target):
                errors.append(
                    "inbox et target pointent vers le même dossier : {}"
                    .format(self.target))
            fallback_path = os.path.join(self.target, self.fallback)
            if os.path.realpath(self.inbox) == os.path.realpath(fallback_path):
                errors.append(
                    "inbox pointe vers le dossier fallback ({}) — "
                    "risque de suppression de fichiers".format(self.fallback))

        # Vérifications sur disque
        if check_dirs:
            if self.target and not os.path.isdir(self.target):
                errors.append(
                    "target introuvable : {} (disque non monté ?)"
                    .format(self.target))
            if self.inbox and not os.path.isdir(self.inbox):
                errors.append(
                    "inbox introuvable : {} (créer le dossier ?)"
                    .format(self.inbox))

        return errors

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize profile to dictionary.

        Returns:
            Dict[str, Any]: Profile configuration as a dictionary.
        """
        return {
            "name": self.name,
            "description": self.description,
            "target": self.target,
            "inbox": self.inbox,
            "fallback": self.fallback,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_endpoint": self.llm_endpoint,
            "defaults": self.defaults,
            "theme_mapping": self.theme_mapping,
            "refinement_rules": self.refinement_rules,
            "tree": self.tree,
            "categories": self.categories,
        }


def list_profiles() -> list[str]:
    """
    List all available profile names.

    Returns:
        List[str]: List of profile directory names in the profiles/ directory.
    """
    try:
        project_root = get_project_root()
        profiles_path = project_root / PROFILES_DIR

        if not profiles_path.exists():
            return []

        profiles = [
            d.name for d in profiles_path.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ]
        return sorted(profiles)
    except ConfigError:
        return []


def init_profile(name: str, target: str) -> Profile:
    """
    Create a new profile with skeleton YAML files.

    Creates the profile directory and populates it with minimal YAML files:
    - profile.yaml: Basic config
    - tree.yaml: Empty folder structure
    - theme_mapping.yaml: Empty theme mappings
    - refinement.yaml: Empty refinement rules
    - categories.yaml: Empty categories

    Args:
        name (str): Profile name.
        target (str): Path to the target library.

    Returns:
        Profile: The newly created Profile instance.

    Raises:
        FileExistsError: If the profile already exists.
        RuntimeError: If project root cannot be found.
    """
    project_root = get_project_root()
    profile_dir = project_root / PROFILES_DIR / name

    if profile_dir.exists():
        raise FileExistsError(
            f"Profile already exists: {profile_dir}"
        )

    # Create profile directory
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Create skeleton files
    skeleton_files = {
        "profile.yaml": {
            "name": name,
            "description": "Profile: {}".format(name),
            "target": target,
            "inbox": os.path.join(target, "_INBOX"),
            "fallback": "_A-TRIER",
            "llm": {
                "provider": "siliconflow",
                "model": "Qwen/Qwen2.5-VL-7B-Instruct",
                "endpoint": "https://api.siliconflow.com/v1/chat/completions",
            },
            "defaults": {
                "workers": 5,
                "delay": 0.2,
                "min_confidence": 0.5,
                "cost_per_call": 0.00034,
                "max_api_errors": 10,
                "max_retries": 3,
            }
        },
        "tree.yaml": {
            "folders": []
        },
        "theme_mapping.yaml": {},
        "refinement.yaml": {
            "rules": []
        },
        "categories.yaml": {}
    }

    for filename, content in skeleton_files.items():
        filepath = profile_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            yaml.safe_dump(content, f, default_flow_style=False, allow_unicode=True)

    # Load and return the new profile
    return Profile(name)


if __name__ == "__main__":
    # Example usage
    from lib.logger import setup_logger
    setup_logger()
    log = get_logger()
    log.info("Project root: %s", get_project_root())
    log.info("Available profiles: %s", list_profiles())

    try:
        default_profile = Profile("default")
        log.info("Loaded profile: %s", default_profile.name)
        log.info("Description: %s", default_profile.description)
        log.info("Target: %s", default_profile.target)
        log.info("LLM Provider: %s", default_profile.llm_provider)
    except ConfigError as e:
        log.error("Error: %s", e)
