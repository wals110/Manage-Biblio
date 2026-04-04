"""
LLM Mapper — Résolution intelligente des thèmes inconnus.
==========================================================
Quand theme_mapping.yaml ne contient pas de correspondance pour un thème
détecté par le LLM Vision, ce module fait un appel LLM texte (léger, pas
d'image) pour trouver le meilleur dossier dans l'arborescence existante.

Le résultat est ensuite auto-ajouté dans theme_mapping.yaml pour que le
même thème soit résolu instantanément la prochaine fois (apprentissage).

Si aucun dossier existant ne convient, le mapper peut proposer la création
d'un nouveau dossier via un fichier suggestions.yaml, que l'utilisateur
review et valide avec `./klodo.sh suggest --apply`.

Usage:
    from lib.llm_mapper import LLMMapper

    mapper = LLMMapper(
        folders=['01-SCIENCES/PHYSIQUE', '02-INFORMATIQUE/05-IA-ML', ...],
        api_key='sk-xxx',
        endpoint='https://api.siliconflow.com/v1/chat/completions',
        model='Qwen/Qwen3-VL-8B-Instruct',
    )

    # Résoudre un thème inconnu
    path = mapper.resolve('Mechatronics', title='Introduction to Mechatronics')
    # → '03-INGENIERIE/ROBOTIQUE'

    # Auto-apprentissage : sauvegarder les nouveaux mappings
    mapper.save_learned(theme_mapping_path)

    # Sauvegarder les suggestions de nouveaux dossiers
    mapper.save_suggestions(logs_dir)
"""

import json
import os
from datetime import datetime

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

from lib.constants import (
    LLM_MAX_RETRIES,
    LLM_MAX_TOKENS,
    LLM_TIMEOUT,
    MAPPER_MIN_CONFIDENCE,
    NO_FOLDER_MARKER,
    UNSORTED_FOLDER,
)
from lib.llm_client import LLMClient
from lib.logger import get_logger
from lib.utils import sanitize_for_prompt

log = get_logger()


# ── Prompt pour le LLM Mapper (dossier existant) ──
MAPPER_PROMPT_TEMPLATE = """Tu es un bibliothécaire expert chargé de classer des livres PDF.

LIVRE À CLASSER :
- Thème détecté : "{theme}"
- Titre : "{title}"
- Fichier : "{filename}"

DOSSIERS DISPONIBLES (liste exhaustive) :
{folders_list}

TÂCHE : Choisis le dossier LE PLUS PRÉCIS (le plus profond dans l'arborescence) pour ce livre.

RÈGLES STRICTES :
1. TOUJOURS préférer un sous-dossier spécifique à un dossier parent.
   Exemple : "01-SCIENCES/PHYSIQUE/05-Relativite-Quantique" plutôt que "01-SCIENCES/PHYSIQUE"
2. Le "folder" DOIT être une copie EXACTE d'un dossier listé ci-dessus.
3. Si aucun dossier ne convient, mets "folder": "{no_folder_marker}".
4. "confidence" entre 0.0 et 1.0 — mets > 0.8 seulement si le match est évident.

Réponds UNIQUEMENT avec un objet JSON :
{{"folder": "chemin/exact/du/dossier", "confidence": 0.85, "reason": "explication courte"}}
"""

# ── Prompt vision pour le LLM Mapper (escalade) ──
MAPPER_VISION_PROMPT = """Tu es un bibliothécaire expert. Regarde cette couverture de livre PDF.

DOSSIERS DISPONIBLES (liste exhaustive) :
{folders_list}

TÂCHE : En te basant sur la couverture, choisis le dossier LE PLUS PRÉCIS pour ce livre.

RÈGLES STRICTES :
1. TOUJOURS préférer un sous-dossier spécifique à un dossier parent.
2. Le "folder" DOIT être une copie EXACTE d'un dossier listé ci-dessus.
3. Si aucun dossier ne convient, mets "folder": "{no_folder_marker}".
4. "confidence" entre 0.0 et 1.0.

Réponds UNIQUEMENT avec un objet JSON :
{{"folder": "chemin/exact/du/dossier", "confidence": 0.85, "reason": "explication courte"}}
"""

# ── Prompt pour proposer un nouveau dossier ──
SUGGEST_PROMPT_TEMPLATE = """Tu es un bibliothécaire expert. Le thème "{theme}" (livre : "{title}") ne correspond à aucun dossier existant dans cette bibliothèque :

{folders_list}

Propose un NOUVEAU dossier qui s'intégrerait bien dans l'arborescence existante.

Réponds UNIQUEMENT avec un objet JSON :
{{"folder": "SECTION/NOUVEAU-DOSSIER", "parent": "SECTION", "reason": "justification courte"}}

Règles :
- Le "parent" DOIT être une section existante (01-SCIENCES, 02-INFORMATIQUE, 03-INGENIERIE, 04-SHS, 05-RELIGIONS, 06-MEDECINE, 07-LANGUES, 08-LOISIRS, 09-BUSINESS)
- Le nom du dossier doit suivre le style existant : MAJUSCULES, tirets, pas d'accents
- Sois conservateur : propose des dossiers assez larges pour accueillir plusieurs livres
- Exemples de bon format : "03-INGENIERIE/MATERIAUX", "01-SCIENCES/GEOLOGIE"
"""


class LLMMapper:
    """Résout les thèmes inconnus via appel LLM texte + auto-apprentissage."""

    def __init__(self, folders, api_key='', endpoint='', model='',
                 min_confidence=MAPPER_MIN_CONFIDENCE, verbose=False, vision=False,
                 client=None):
        # type: (list[str], str, str, str, float, bool, bool, LLMClient | None) -> None
        self.folders = folders
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model
        self.min_confidence = min_confidence
        self.verbose = verbose
        self.vision = vision
        self.learned = {}  # type: dict[str, str]
        self.suggestions = []  # type: list[dict]
        self._folders_text = "\n".join("- {}".format(f) for f in folders)
        # Client LLM : fourni ou créé à la demande
        self._client = client
        # Stats
        self.calls = 0
        self.successes = 0
        self.suggest_count = 0
        self.vision_calls = 0
        self.vision_successes = 0

    def resolve(self, theme, title='', filename='', pdf_path=None):
        # type: (str, str, str, str | None) -> str | None
        """
        Demande au LLM de mapper un thème inconnu vers un dossier existant.
        Si aucun dossier ne convient et que vision est activée, escalade
        avec la couverture PDF.
        En dernier recours, génère une suggestion de nouveau dossier.

        Args:
            theme: Thème détecté par LLM Vision.
            title: Titre du livre (optionnel).
            filename: Nom du fichier PDF (optionnel).
            pdf_path: Chemin complet du PDF (pour escalade vision, optionnel).

        Returns:
            Chemin du dossier ou None si échec / suggestion générée.
        """
        if not self._get_client():
            return None
        if not self._client.api_key:
            return None

        result = self._call_mapper(theme, title, filename)
        validated = self._process_mapper_result(result, theme)

        if validated:
            # Succès text-mapper
            self.successes += 1
            self.learned[theme] = validated
            if self.verbose:
                reason = result.get('reason', '') if result else ''
                confidence = result.get('confidence', 0.0) if result else 0.0
                log.info("  🧠 Mapper: '{}' → {} (conf: {}, {})".format(
                    theme, validated, confidence, reason))
            return validated

        # Escalade vision si activée et pdf_path fourni
        if self.vision and pdf_path:
            vision_result = self._try_vision_mapper(pdf_path)
            if vision_result:
                vision_validated = self._process_mapper_result(vision_result, theme)
                if vision_validated:
                    self.vision_successes += 1
                    self.successes += 1
                    self.learned[theme] = vision_validated
                    if self.verbose:
                        reason = vision_result.get('reason', '')
                        confidence = vision_result.get('confidence', 0.0)
                        log.info("  👁 Mapper vision: '{}' → {} (conf: {}, {})".format(
                            theme, vision_validated, confidence, reason))
                    return vision_validated

        # Tout a échoué → suggérer un nouveau dossier
        self._suggest_new_folder(theme, title, filename)
        return None

    def _process_mapper_result(self, result, theme):
        # type: (dict | None, str) -> str | None
        """Valide le résultat du mapper (texte ou vision). Retourne le chemin validé ou None."""
        if result is None:
            return None

        folder = result.get('folder', '')
        confidence = result.get('confidence', 0.0)

        # Le LLM dit qu'aucun dossier ne convient
        if folder == NO_FOLDER_MARKER or folder == UNSORTED_FOLDER:
            return None

        # Valider que le dossier existe dans la liste
        validated = self._validate_folder(folder)
        if not validated:
            if self.verbose:
                log.warning("  ⚠ Mapper: dossier invalide '{}' pour thème '{}'".format(
                    folder, theme))
            return None

        if confidence < self.min_confidence:
            if self.verbose:
                log.warning("  ⚠ Mapper: confiance trop basse ({}) pour '{}'".format(
                    confidence, theme))
            return None

        return validated

    def _try_vision_mapper(self, pdf_path):
        # type: (str) -> dict | None
        """Escalade vision : envoie la couverture PDF au LLM Vision pour classification."""
        try:
            from lib.vision import extract_cover_image, image_to_base64
        except ImportError:
            if self.verbose:
                log.warning("  ⚠ Mapper vision: lib.vision non disponible")
            return None

        images = extract_cover_image(pdf_path, dpi=150, n_pages=1)
        if not images:
            if self.verbose:
                log.warning("  ⚠ Mapper vision: extraction couverture échouée")
            return None

        b64 = image_to_base64(images[0])
        if not b64:
            return None

        self.vision_calls += 1

        prompt = MAPPER_VISION_PROMPT.format(
            folders_list=self._folders_text,
            no_folder_marker=NO_FOLDER_MARKER,
        )

        client = self._get_client()
        if not client:
            return None

        content = client.call(
            prompt=prompt,
            images_b64=[b64],
            max_tokens=LLM_MAX_TOKENS,
        )

        if content is None:
            return None

        return self._parse_response(content)

    def _call_mapper(self, theme, title, filename):
        # type: (str, str, str) -> dict | None
        """Appel LLM pour mapper un thème vers un dossier existant."""
        prompt = MAPPER_PROMPT_TEMPLATE.format(
            theme=sanitize_for_prompt(theme),
            title=sanitize_for_prompt(title),
            filename=sanitize_for_prompt(filename),
            folders_list=self._folders_text,
            no_folder_marker=NO_FOLDER_MARKER,
        )
        self.calls += 1
        return self._call_llm(prompt)

    def _suggest_new_folder(self, theme, title, filename):
        # type: (str, str, str) -> None

        """Demande au LLM de proposer un nouveau dossier pour ce thème."""
        # Éviter les doublons de suggestion pour le même thème
        existing_themes = {s['theme'].lower() for s in self.suggestions}
        if theme.lower() in existing_themes:
            return

        prompt = SUGGEST_PROMPT_TEMPLATE.format(
            theme=sanitize_for_prompt(theme),
            title=sanitize_for_prompt(title),
            folders_list=self._folders_text,
        )

        result = self._call_llm(prompt)
        if result and result.get('folder'):
            suggestion = {
                'theme': theme,
                'title': title,
                'filename': filename,
                'folder': result['folder'],
                'parent': result.get('parent', ''),
                'reason': result.get('reason', ''),
                'status': 'pending',
            }
            self.suggestions.append(suggestion)
            self.suggest_count += 1

            if self.verbose:
                log.info("  💡 Suggestion: '{}' → NOUVEAU {} ({})".format(
                    theme, result['folder'], result.get('reason', '')))

    def _get_client(self):
        # type: () -> LLMClient | None
        """Retourne le client LLM, en le créant à la demande si nécessaire."""
        if self._client is None:
            if self.api_key and self.endpoint:
                self._client = LLMClient(
                    api_key=self.api_key,
                    endpoint=self.endpoint,
                    model=self.model,
                    timeout=LLM_TIMEOUT,
                    max_retries=LLM_MAX_RETRIES,
                    verbose=self.verbose,
                )
            else:
                return None
        return self._client

    def _call_llm(self, prompt):
        # type: (str) -> dict | None
        """Appel LLM générique via le client unifié, retourne le JSON parsé."""
        client = self._get_client()
        if not client:
            return None

        content = client.call(prompt=prompt, max_tokens=LLM_MAX_TOKENS)
        if content is None:
            return None

        return self._parse_response(content)

    def _validate_folder(self, folder):
        # type: (str) -> str | None
        """Valide et normalise un chemin de dossier. Retourne le chemin exact ou None."""
        if folder in self.folders:
            return folder
        # Match insensible à la casse
        for f in self.folders:
            if f.lower() == folder.lower():
                return f
        return None

    def _parse_response(self, content):
        # type: (str) -> dict | None
        """Parse la réponse JSON du LLM, avec tolérance."""
        # Nettoyer les balises markdown ```json ... ```
        if '```' in content:
            lines = content.split('\n')
            json_lines = []
            in_block = False
            for line in lines:
                if line.strip().startswith('```'):
                    in_block = not in_block
                    continue
                if in_block or (not in_block and line.strip().startswith('{')):
                    json_lines.append(line)
            content = '\n'.join(json_lines)

        # Extraire le premier objet JSON
        start = content.find('{')
        end = content.rfind('}')
        if start == -1 or end == -1:
            return None

        try:
            return json.loads(content[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None

    def save_learned(self, theme_mapping_path):
        # type: (str) -> int
        """
        Sauvegarde les thèmes appris dans theme_mapping.yaml.
        Ne modifie pas les entrées existantes.

        Returns:
            Nombre de thèmes ajoutés.
        """
        if not self.learned:
            return 0

        if not HAS_YAML:
            log.warning("  ⚠ PyYAML requis pour la sauvegarde du mapping")
            return 0

        # Charger le mapping existant
        existing = {}  # type: dict[str, str]
        if os.path.exists(theme_mapping_path):
            with open(theme_mapping_path, 'r', encoding='utf-8') as f:
                existing = yaml.safe_load(f) or {}

        # Ajouter les nouveaux thèmes (sans écraser)
        added = 0
        for theme, folder in self.learned.items():
            if theme.lower() not in {k.lower() for k in existing.keys()}:
                existing[theme] = folder
                added += 1

        if added > 0:
            _write_theme_mapping(theme_mapping_path, existing)
            log.info("  💾 {} nouveaux thèmes ajoutés dans theme_mapping.yaml ({} total)".format(
                added, len(existing)))

        return added

    def save_suggestions(self, logs_dir):
        # type: (str) -> str | None
        """
        Sauvegarde les suggestions de nouveaux dossiers dans suggestions.yaml.
        Fusionne avec les suggestions existantes (ne supprime rien).

        Returns:
            Chemin du fichier ou None si pas de suggestions.
        """
        if not self.suggestions:
            return None

        if not HAS_YAML:
            log.warning("  ⚠ PyYAML requis pour les suggestions")
            return None

        path = os.path.join(logs_dir, 'suggestions.yaml')

        # Charger les suggestions existantes
        existing = []  # type: list[dict]
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                existing = yaml.safe_load(f) or []

        # Fusionner (éviter doublons par thème)
        existing_themes = {s['theme'].lower() for s in existing}
        for s in self.suggestions:
            if s['theme'].lower() not in existing_themes:
                existing.append(s)
                existing_themes.add(s['theme'].lower())

        # Écrire le fichier avec un format lisible
        with open(path, 'w', encoding='utf-8') as f:
            f.write("# " + "=" * 65 + "\n")
            f.write("# Suggestions de nouveaux dossiers\n")
            f.write("# Généré le {}\n".format(datetime.now().strftime('%Y-%m-%d %H:%M')))
            f.write("#\n")
            f.write("# INSTRUCTIONS :\n")
            f.write("#   1. Reviewez chaque suggestion ci-dessous\n")
            f.write("#   2. Supprimez les lignes que vous ne voulez pas\n")
            f.write("#   3. Modifiez les chemins si nécessaire\n")
            f.write("#   4. Lancez : ./klodo.sh suggest --apply\n")
            f.write("#\n")
            f.write("# STATUS : pending = à valider, applied = déjà appliqué\n")
            f.write("# " + "=" * 65 + "\n\n")

            for s in existing:
                f.write("- theme: \"{}\"\n".format(s['theme']))
                f.write("  title: \"{}\"\n".format(s.get('title', '')))
                f.write("  filename: \"{}\"\n".format(s.get('filename', '')))
                f.write("  folder: \"{}\"\n".format(s['folder']))
                f.write("  parent: \"{}\"\n".format(s.get('parent', '')))
                f.write("  reason: \"{}\"\n".format(s.get('reason', '')))
                f.write("  status: {}\n".format(s.get('status', 'pending')))
                f.write("\n")

        count = sum(1 for s in existing if s.get('status') == 'pending')
        log.info("  💡 {} suggestions sauvegardées dans {}".format(count, path))
        return path

    def print_stats(self):
        # type: () -> None
        """Affiche les statistiques du mapper."""
        if self.calls > 0:
            parts = ["{} appels".format(self.calls),
                     "{} résolus".format(self.successes),
                     "{} appris".format(len(self.learned))]
            if self.vision_calls > 0:
                parts.append("👁 {} vision ({} résolus)".format(
                    self.vision_calls, self.vision_successes))
            if self.suggest_count > 0:
                parts.append("{} suggestions".format(self.suggest_count))
            log.info("\n🧠 LLM Mapper : {}".format(", ".join(parts)))


# ════════════════════════════════════════════════════════════════════════════
# FONCTIONS UTILITAIRES (utilisées par cmd_suggest)
# ════════════════════════════════════════════════════════════════════════════

def load_suggestions(logs_dir):
    # type: (str) -> list[dict]
    """Charge les suggestions depuis suggestions.yaml."""
    path = os.path.join(logs_dir, 'suggestions.yaml')
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or []
    return [s for s in data if isinstance(s, dict)]


def apply_suggestions(suggestions, profile_dir, target_base):
    # type: (list[dict], str, str) -> dict[str, int]
    """
    Applique les suggestions validées :
    1. Crée les nouveaux dossiers dans la bibliothèque
    2. Met à jour tree.yaml
    3. Met à jour theme_mapping.yaml

    Returns:
        Dict avec compteurs : folders_created, themes_added, skipped
    """
    if not HAS_YAML:
        log.warning("  ⚠ PyYAML requis")
        return {'folders_created': 0, 'themes_added': 0, 'skipped': 0}

    pending = [s for s in suggestions if s.get('status') == 'pending']
    if not pending:
        log.info("  ✅ Aucune suggestion en attente.")
        return {'folders_created': 0, 'themes_added': 0, 'skipped': 0}

    # ── 1. Charger tree.yaml ──
    tree_path = os.path.join(profile_dir, 'tree.yaml')
    with open(tree_path, 'r', encoding='utf-8') as f:
        tree_data = yaml.safe_load(f) or {}
    existing_folders = set(tree_data.get('folders', []))

    # ── 2. Charger theme_mapping.yaml ──
    mapping_path = os.path.join(profile_dir, 'theme_mapping.yaml')
    if os.path.exists(mapping_path):
        with open(mapping_path, 'r', encoding='utf-8') as f:
            theme_mapping = yaml.safe_load(f) or {}
    else:
        theme_mapping = {}

    folders_created = 0
    themes_added = 0
    skipped = 0

    for s in pending:
        folder = s.get('folder', '').strip()
        theme = s.get('theme', '').strip()

        if not folder or not theme:
            skipped += 1
            continue

        # Créer le dossier physique
        full_path = os.path.join(target_base, folder)
        if not os.path.exists(full_path):
            os.makedirs(full_path, exist_ok=True)
            folders_created += 1
            log.info("  📁 Créé : {}".format(folder))

        # Ajouter dans tree.yaml
        if folder not in existing_folders:
            existing_folders.add(folder)

        # Ajouter dans theme_mapping.yaml
        if theme.lower() not in {k.lower() for k in theme_mapping.keys()}:
            theme_mapping[theme] = folder
            themes_added += 1

        # Marquer comme appliqué
        s['status'] = 'applied'

    # ── 3. Sauvegarder tree.yaml ──
    sorted_folders = sorted(existing_folders)
    tree_data['folders'] = sorted_folders
    with open(tree_path, 'w', encoding='utf-8') as f:
        f.write("# " + "=" * 75 + "\n")
        f.write("# Arborescence cible\n")
        f.write("# {} dossiers\n".format(len(sorted_folders)))
        f.write("# " + "=" * 75 + "\n\n")
        f.write("folders:\n")
        current_section = ''
        for folder in sorted_folders:
            section = folder.split('/')[0]
            if section != current_section:
                if current_section:
                    f.write("\n")
                f.write("  # ── {} ──\n".format(section))
                current_section = section
            f.write("  - {}\n".format(folder))

    # ── 4. Sauvegarder theme_mapping.yaml ──
    _write_theme_mapping(mapping_path, theme_mapping)

    log.info("\n  ✅ {} dossiers créés, {} thèmes ajoutés, {} ignorés".format(
        folders_created, themes_added, skipped))

    return {
        'folders_created': folders_created,
        'themes_added': themes_added,
        'skipped': skipped,
    }


def save_suggestions_file(suggestions, logs_dir):
    # type: (list[dict], str) -> None
    """Réécrit le fichier suggestions.yaml (après apply ou modification)."""
    path = os.path.join(logs_dir, 'suggestions.yaml')
    with open(path, 'w', encoding='utf-8') as f:
        f.write("# " + "=" * 65 + "\n")
        f.write("# Suggestions de nouveaux dossiers\n")
        f.write("# Mis à jour le {}\n".format(datetime.now().strftime('%Y-%m-%d %H:%M')))
        f.write("# " + "=" * 65 + "\n\n")

        for s in suggestions:
            f.write("- theme: \"{}\"\n".format(s.get('theme', '')))
            f.write("  title: \"{}\"\n".format(s.get('title', '')))
            f.write("  filename: \"{}\"\n".format(s.get('filename', '')))
            f.write("  folder: \"{}\"\n".format(s.get('folder', '')))
            f.write("  parent: \"{}\"\n".format(s.get('parent', '')))
            f.write("  reason: \"{}\"\n".format(s.get('reason', '')))
            f.write("  status: {}\n".format(s.get('status', 'pending')))
            f.write("\n")


def _write_theme_mapping(path, mapping):
    # type: (str, dict[str, str]) -> None
    """Écrit le fichier theme_mapping.yaml trié par section."""
    with open(path, 'w', encoding='utf-8') as f:
        f.write("# " + "=" * 75 + "\n")
        f.write("# Mapping thème → chemin cible\n")
        f.write("# Auto-enrichi par LLM Mapper ({} entrées)\n".format(len(mapping)))
        f.write("# " + "=" * 75 + "\n\n")

        sorted_items = sorted(mapping.items(), key=lambda x: (x[1], x[0].lower()))
        current_section = ''
        for theme_key, dest_path in sorted_items:
            section = dest_path.split('/')[0] if '/' in dest_path else dest_path
            if section != current_section:
                if current_section:
                    f.write("\n")
                f.write("# ── {} ──\n".format(section))
                current_section = section
            if ':' in theme_key or theme_key.startswith('{') or theme_key.startswith('['):
                f.write('"{}": "{}"\n'.format(theme_key, dest_path))
            else:
                f.write('{}: "{}"\n'.format(theme_key, dest_path))
