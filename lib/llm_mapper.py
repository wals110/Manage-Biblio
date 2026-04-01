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
review et valide avec `./biblio.sh suggest --apply`.

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

import os
import json
import time
from datetime import datetime
from typing import Optional, List, Dict

try:
    import requests as req_lib
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# ── Prompt pour le LLM Mapper (dossier existant) ──
MAPPER_PROMPT_TEMPLATE = """Tu es un bibliothécaire expert. On te donne :
- Un THÈME détecté pour un livre : "{theme}"
- Le TITRE du livre : "{title}"
- Le NOM DU FICHIER : "{filename}"

Voici la liste des dossiers disponibles dans la bibliothèque :
{folders_list}

Ta tâche : choisir LE MEILLEUR dossier pour ce livre.

Réponds UNIQUEMENT avec un objet JSON (pas de texte avant/après) :
{{"folder": "chemin/exact/du/dossier", "confidence": 0.95, "reason": "explication courte"}}

Règles :
- Le "folder" DOIT être un des dossiers listés ci-dessus (copie exacte)
- Si aucun dossier ne convient vraiment, mets "folder": "_AUCUN"
- "confidence" entre 0.0 et 1.0
- Sois précis : préfère les sous-dossiers spécifiques aux dossiers parents
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

    def __init__(self, folders, api_key, endpoint, model,
                 min_confidence=0.6, verbose=False):
        # type: (List[str], str, str, str, float, bool) -> None
        self.folders = folders
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model
        self.min_confidence = min_confidence
        self.verbose = verbose
        self.learned = {}  # type: Dict[str, str]
        self.suggestions = []  # type: List[Dict]
        self._folders_text = "\n".join("- {}".format(f) for f in folders)
        # Stats
        self.calls = 0
        self.successes = 0
        self.suggest_count = 0

    def resolve(self, theme, title='', filename=''):
        # type: (str, str, str) -> Optional[str]
        """
        Demande au LLM de mapper un thème inconnu vers un dossier existant.
        Si aucun dossier ne convient, génère une suggestion de nouveau dossier.

        Returns:
            Chemin du dossier ou None si échec / suggestion générée.
        """
        if not HAS_REQUESTS:
            return None
        if not self.api_key:
            return None

        result = self._call_mapper(theme, title, filename)
        if result is None:
            return None

        folder = result.get('folder', '')
        confidence = result.get('confidence', 0.0)
        reason = result.get('reason', '')

        # Le LLM dit qu'aucun dossier ne convient → proposer un nouveau
        if folder == '_AUCUN' or folder == '_A-TRIER':
            self._suggest_new_folder(theme, title, filename)
            return None

        # Valider que le dossier existe dans la liste
        validated = self._validate_folder(folder)
        if not validated:
            # Dossier invalide → tenter une suggestion
            if self.verbose:
                print("  ⚠ Mapper: dossier invalide '{}' pour thème '{}'".format(
                    folder, theme))
            self._suggest_new_folder(theme, title, filename)
            return None

        if confidence < self.min_confidence:
            if self.verbose:
                print("  ⚠ Mapper: confiance trop basse ({}) pour '{}'".format(
                    confidence, theme))
            # Confiance basse → suggérer quand même un nouveau dossier
            self._suggest_new_folder(theme, title, filename)
            return None

        # Succès
        self.successes += 1
        self.learned[theme] = validated

        if self.verbose:
            print("  🧠 Mapper: '{}' → {} (conf: {}, {})".format(
                theme, validated, confidence, reason))

        return validated

    def _call_mapper(self, theme, title, filename):
        # type: (str, str, str) -> Optional[Dict]
        """Appel LLM pour mapper un thème vers un dossier existant."""
        prompt = MAPPER_PROMPT_TEMPLATE.format(
            theme=theme,
            title=title or '(inconnu)',
            filename=filename or '(inconnu)',
            folders_list=self._folders_text,
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
            theme=theme,
            title=title or '(inconnu)',
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
                print("  💡 Suggestion: '{}' → NOUVEAU {} ({})".format(
                    theme, result['folder'], result.get('reason', '')))

    def _call_llm(self, prompt):
        # type: (str) -> Optional[Dict]
        """Appel LLM générique, retourne le JSON parsé."""
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = "Bearer {}".format(self.api_key)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 150,
            "temperature": 0.1,
        }

        for attempt in range(3):
            try:
                resp = req_lib.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                    timeout=20,
                )

                if resp.status_code == 429:
                    wait = min(2 ** attempt * 2, 15)
                    if self.verbose:
                        print("  ⏳ Mapper rate limit, attente {}s...".format(wait))
                    time.sleep(wait)
                    continue

                if resp.status_code != 200:
                    if self.verbose:
                        print("  ⚠ Mapper API erreur {}: {}".format(
                            resp.status_code, resp.text[:150]))
                    return None

                data = resp.json()
                content = data['choices'][0]['message']['content'].strip()
                return self._parse_response(content)

            except Exception as e:
                if self.verbose:
                    print("  ⚠ Mapper exception: {}".format(e))
                if attempt < 2:
                    time.sleep(1)
                    continue
                return None

        return None

    def _validate_folder(self, folder):
        # type: (str) -> Optional[str]
        """Valide et normalise un chemin de dossier. Retourne le chemin exact ou None."""
        if folder in self.folders:
            return folder
        # Match insensible à la casse
        for f in self.folders:
            if f.lower() == folder.lower():
                return f
        return None

    def _parse_response(self, content):
        # type: (str) -> Optional[Dict]
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
            print("  ⚠ PyYAML requis pour la sauvegarde du mapping")
            return 0

        # Charger le mapping existant
        existing = {}  # type: Dict[str, str]
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
            print("  💾 {} nouveaux thèmes ajoutés dans theme_mapping.yaml ({} total)".format(
                added, len(existing)))

        return added

    def save_suggestions(self, logs_dir):
        # type: (str) -> Optional[str]
        """
        Sauvegarde les suggestions de nouveaux dossiers dans suggestions.yaml.
        Fusionne avec les suggestions existantes (ne supprime rien).

        Returns:
            Chemin du fichier ou None si pas de suggestions.
        """
        if not self.suggestions:
            return None

        if not HAS_YAML:
            print("  ⚠ PyYAML requis pour les suggestions")
            return None

        path = os.path.join(logs_dir, 'suggestions.yaml')

        # Charger les suggestions existantes
        existing = []  # type: List[Dict]
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
            f.write("#   4. Lancez : ./biblio.sh suggest --apply\n")
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
        print("  💡 {} suggestions sauvegardées dans {}".format(count, path))
        return path

    def print_stats(self):
        # type: () -> None
        """Affiche les statistiques du mapper."""
        if self.calls > 0:
            parts = ["{} appels".format(self.calls),
                     "{} résolus".format(self.successes),
                     "{} appris".format(len(self.learned))]
            if self.suggest_count > 0:
                parts.append("{} suggestions".format(self.suggest_count))
            print("\n🧠 LLM Mapper : {}".format(", ".join(parts)))


# ════════════════════════════════════════════════════════════════════════════
# FONCTIONS UTILITAIRES (utilisées par cmd_suggest)
# ════════════════════════════════════════════════════════════════════════════

def load_suggestions(logs_dir):
    # type: (str) -> List[Dict]
    """Charge les suggestions depuis suggestions.yaml."""
    path = os.path.join(logs_dir, 'suggestions.yaml')
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or []
    return [s for s in data if isinstance(s, dict)]


def apply_suggestions(suggestions, profile_dir, target_base):
    # type: (List[Dict], str, str) -> Dict[str, int]
    """
    Applique les suggestions validées :
    1. Crée les nouveaux dossiers dans la bibliothèque
    2. Met à jour tree.yaml
    3. Met à jour theme_mapping.yaml

    Returns:
        Dict avec compteurs : folders_created, themes_added, skipped
    """
    if not HAS_YAML:
        print("  ⚠ PyYAML requis")
        return {'folders_created': 0, 'themes_added': 0, 'skipped': 0}

    pending = [s for s in suggestions if s.get('status') == 'pending']
    if not pending:
        print("  ✅ Aucune suggestion en attente.")
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
            print("  📁 Créé : {}".format(folder))

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

    print("\n  ✅ {} dossiers créés, {} thèmes ajoutés, {} ignorés".format(
        folders_created, themes_added, skipped))

    return {
        'folders_created': folders_created,
        'themes_added': themes_added,
        'skipped': skipped,
    }


def save_suggestions_file(suggestions, logs_dir):
    # type: (List[Dict], str) -> None
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
    # type: (str, Dict[str, str]) -> None
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
