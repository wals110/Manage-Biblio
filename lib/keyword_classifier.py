#!/usr/bin/env python3
"""
Klodo Organizer — Classement thématique de bibliothèques PDF.
==============================================================
Classe automatiquement les fichiers PDF dans une arborescence thématique
en combinant deux approches :

  1. Mots-clés configurables (categories.yaml)
  2. Apprentissage par l'existant (profils TF-IDF des dossiers déjà classés)

Pour les fichiers aux noms vagues, extraction du contenu PDF (métadonnées,
premières pages) pour identifier le sujet.

Usage :
    python3 klodo_organizer.py /chemin/vers/biblio                  # Rapport
    python3 klodo_organizer.py /chemin/vers/biblio --execute         # Appliquer
    python3 klodo_organizer.py /chemin/vers/biblio --learn-only      # Apprendre
    python3 klodo_organizer.py --undo log_classement_*.csv           # Annuler

Options :
    --config FILE     Fichier de config (défaut: categories.yaml)
    --no-pdf          Désactiver l'extraction de contenu PDF
    --no-learn        Désactiver l'apprentissage par l'existant
    --dry-run         Synonyme du mode rapport (pas de --execute)
    --verbose         Afficher les détails de classification
    --min-score N     Score minimum pour accepter (défaut: 0.10)

Dépendances : pip install pypdf pdfplumber pyyaml
"""

__version__ = "1.0.0"

import argparse
import csv
import math
import os
import re
import signal
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime

import yaml

# ── Optionnels ──────────────────────────────────────────────────────────────
try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False


# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════

PDF_TIMEOUT = 10
MAX_PDF_PAGES = 5          # Pages à extraire pour l'analyse
STOPWORDS_FR = {
    'le', 'la', 'les', 'de', 'du', 'des', 'un', 'une', 'et', 'en', 'au',
    'aux', 'ce', 'cette', 'ces', 'par', 'pour', 'dans', 'sur', 'avec',
    'est', 'sont', 'être', 'avoir', 'que', 'qui', 'ne', 'pas', 'plus',
    'ou', 'se', 'sa', 'son', 'ses', 'nous', 'vous', 'ils', 'leur',
    'tout', 'tous', 'très', 'aussi', 'mais', 'donc', 'car', 'si',
}
STOPWORDS_EN = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been',
    'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
    'could', 'should', 'may', 'might', 'shall', 'can', 'not', 'no', 'nor',
    'as', 'if', 'then', 'than', 'so', 'that', 'this', 'these', 'those',
    'it', 'its', 'he', 'she', 'they', 'we', 'you', 'my', 'your', 'his',
    'her', 'our', 'their', 'which', 'what', 'who', 'whom', 'how', 'when',
    'where', 'why', 'all', 'each', 'every', 'both', 'few', 'more', 'most',
    'other', 'some', 'such', 'only', 'own', 'same', 'into', 'over',
    'after', 'before', 'between', 'through', 'about', 'up', 'out', 'off',
    'new', 'first', 'also', 'just', 'one', 'two', 'using', 'based',
}
STOPWORDS = STOPWORDS_FR | STOPWORDS_EN

# Mots génériques PDF à ignorer
PDF_NOISE = {
    'page', 'chapter', 'figure', 'table', 'section', 'contents',
    'copyright', 'press', 'publisher', 'edition', 'isbn', 'doi',
    'springer', 'wiley', 'elsevier', 'academic', 'verlag',
    'preface', 'foreword', 'acknowledgment', 'bibliography',
    'references', 'appendix', 'vol', 'volume', 'part', 'pdf',
    'http', 'https', 'www', 'com', 'org', 'edu',
}


# ════════════════════════════════════════════════════════════════════════════
# UTILITAIRES
# ════════════════════════════════════════════════════════════════════════════

def normalize_text(text: str) -> str:
    """Normalise un texte pour la comparaison."""
    text = unicodedata.normalize('NFKD', text)
    text = text.lower()
    # Garder les lettres, chiffres, espaces, +, #, .
    text = re.sub(r'[^\w\s+#.]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def tokenize(text: str) -> list[str]:
    """Découpe un texte en tokens significatifs."""
    text = normalize_text(text)
    tokens = re.findall(r'[a-zA-Z#+.]{2,}', text)
    tokens = [t for t in tokens if t not in STOPWORDS and t not in PDF_NOISE]
    return tokens


def extract_bigrams(tokens: list[str]) -> list[str]:
    """Extrait les bigrammes d'une liste de tokens."""
    return [f"{tokens[i]} {tokens[i+1]}" for i in range(len(tokens) - 1)]


class TimeoutError(Exception):
    pass

def _timeout_handler(signum, frame):
    raise TimeoutError("PDF extraction timed out")


# ════════════════════════════════════════════════════════════════════════════
# EXTRACTION PDF
# ════════════════════════════════════════════════════════════════════════════

def extract_pdf_metadata(filepath: str) -> dict:
    """Extrait les métadonnées d'un PDF."""
    meta = {'title': '', 'author': '', 'subject': '', 'keywords': ''}
    if not HAS_PYPDF:
        return meta
    try:
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(PDF_TIMEOUT)
        try:
            reader = pypdf.PdfReader(filepath)
            info = reader.metadata
            if info:
                meta['title'] = str(info.get('/Title', '') or '')
                meta['author'] = str(info.get('/Author', '') or '')
                meta['subject'] = str(info.get('/Subject', '') or '')
                meta['keywords'] = str(info.get('/Keywords', '') or '')
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
    except Exception:
        pass
    return meta


def extract_pdf_text(filepath: str, max_pages: int = MAX_PDF_PAGES) -> str:
    """Extrait le texte des premières pages d'un PDF."""
    text = ""

    # Essayer pdfplumber d'abord (meilleur pour les mises en page complexes)
    if HAS_PDFPLUMBER:
        try:
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(PDF_TIMEOUT)
            try:
                with pdfplumber.open(filepath) as pdf:
                    for i, page in enumerate(pdf.pages[:max_pages]):
                        page_text = page.extract_text() or ""
                        text += page_text + "\n"
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
        except Exception:
            text = ""

    # Fallback sur pypdf
    if not text and HAS_PYPDF:
        try:
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(PDF_TIMEOUT)
            try:
                reader = pypdf.PdfReader(filepath)
                for i, page in enumerate(reader.pages[:max_pages]):
                    page_text = page.extract_text() or ""
                    text += page_text + "\n"
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
        except Exception:
            pass

    return text


# ════════════════════════════════════════════════════════════════════════════
# CLASSIFIEUR PAR MOTS-CLÉS (Option 1)
# ════════════════════════════════════════════════════════════════════════════

class KeywordClassifier:
    """Classifie par correspondance de mots-clés depuis le YAML."""

    # Mots-clés courts qui ne doivent matcher que comme mots entiers
    # (pour éviter "bert" dans "Albert", "ode" dans "méthode", etc.)
    WORD_BOUNDARY_KEYWORDS = {
        'bert', 'ode', 'rest', 'api', 'go lang', 'r language',
        'c', 'arm', 'pic', 'lua', 'ios', 'gem', 'git',
        'pascal', 'forth', 'spark', 'hive', 'pig',
        'swift', 'ruby', 'rust', 'scala', 'kotlin', 'perl',
        'logic', 'logique', 'boost', 'index', 'ring', 'field',
        'surface', 'series', 'carbon', 'query', 'schema',
        'christ', 'bible', 'imam', 'dharma', 'karma',
        'solid', 'workshop', 'index', 'series',
    }

    def __init__(self, config: dict):
        self.categories = []
        for section_name, entries in config.items():
            if section_name == 'apprentissage':
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                self.categories.append({
                    'chemin': entry['chemin'],
                    'priorite': entry.get('priorite', 5),
                    'mots_cles': [kw.lower() for kw in entry['mots_cles']],
                })

    def _keyword_matches(self, kw: str, text_lower: str) -> bool:
        """Vérifie si un mot-clé matche dans le texte.
        Mono-mot (sans espace) → match par mot entier (évite tran**sport**→sport,
        per**fusion**→fusion, **Spring**er→spring).
        Multi-mots (phrase) → substring (les espaces forment déjà des bornes)."""
        if ' ' not in kw:
            pattern = r'(?<![a-zà-ÿ])' + re.escape(kw) + r'(?![a-zà-ÿ])'
            return bool(re.search(pattern, text_lower))
        return kw in text_lower

    def classify(self, text: str, current_dir: str = '') -> list[tuple[str, float, str]]:
        """
        Retourne une liste de (chemin, score, mot_clé_matché) triée par score.
        Le score tient compte du nombre de mots-clés matchés et de la priorité.

        current_dir: dossier actuel du fichier, utilisé pour l'isolation de catégorie.
        """
        text_lower = text.lower()
        results = []

        for cat in self.categories:
            # ── Isolation de catégorie ──
            # Un fichier de 05-RELIGIONS ne doit pas aller en 01-SCIENCES, etc.
            if current_dir:
                src_top = current_dir.split('/')[0] if '/' in current_dir else current_dir
                dest_top = cat['chemin'].split('/')[0] if '/' in cat['chemin'] else cat['chemin']
                if src_top != dest_top and src_top in ('05-RELIGIONS', '08-LOISIRS', '01-SCIENCES'):
                    # Pénaliser les déplacements entre catégories de haut niveau
                    cross_category = True
                else:
                    cross_category = False
            else:
                cross_category = False

            matched_keywords = []
            for kw in cat['mots_cles']:
                if self._keyword_matches(kw, text_lower):
                    # Bonus pour les mots-clés plus longs (plus spécifiques)
                    weight = 1.0 + len(kw.split()) * 0.5
                    matched_keywords.append((kw, weight))

            if matched_keywords:
                score = sum(w for _, w in matched_keywords)
                # Normaliser par le nombre total de mots-clés de la catégorie
                score = score / (len(cat['mots_cles']) ** 0.3)
                # Ajuster par priorité (plus petit = plus prioritaire)
                score = score * (10 / (cat['priorite'] + 5))

                # Pénaliser fortement les déplacements inter-catégories
                if cross_category:
                    score = score * 0.1

                best_kw = max(matched_keywords, key=lambda x: x[1])[0]
                results.append((cat['chemin'], score, best_kw))

        results.sort(key=lambda x: -x[1])
        return results


# ════════════════════════════════════════════════════════════════════════════
# CLASSIFIEUR PAR APPRENTISSAGE (Option 2)
# ════════════════════════════════════════════════════════════════════════════

class LearningClassifier:
    """
    Apprend un profil TF-IDF pour chaque dossier existant,
    puis classe les nouveaux fichiers par similarité cosinus.
    """

    def __init__(self, config_apprentissage: dict):
        self.actif = config_apprentissage.get('actif', True)
        self.seuil_min_fichiers = config_apprentissage.get('seuil_minimum_fichiers', 3)
        self.top_n = config_apprentissage.get('top_n_mots', 30)
        self.seuil_confiance = config_apprentissage.get('seuil_confiance', 0.15)

        # Profils : {chemin_dossier: {mot: tf-idf_score}}
        self.profiles = {}
        # IDF global
        self.idf = {}
        # Nombre total de documents
        self.n_docs = 0

    def learn_from_directory(self, base_path: str, use_pdf: bool = False):
        """Parcourt l'arborescence et construit les profils par dossier."""
        if not self.actif:
            return

        print("  [Apprentissage] Analyse des fichiers existants...")

        # Collecter les documents par dossier cible (2 niveaux sous la racine thématique)
        dir_docs = defaultdict(list)  # {dir_path: [list of token lists]}
        doc_count = 0

        for root, dirs, files in os.walk(base_path):
            # Ignorer les dossiers cachés et _A-TRIER
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '_A-TRIER']

            pdf_files = [f for f in files if f.lower().endswith('.pdf')]
            if not pdf_files:
                continue

            rel_root = os.path.relpath(root, base_path)

            for fname in pdf_files:
                # Texte de base : le nom du fichier + le nom du dossier parent
                text = fname + " " + os.path.basename(root)

                # Si extraction PDF activée, ajouter le contenu
                if use_pdf:
                    filepath = os.path.join(root, fname)
                    meta = extract_pdf_metadata(filepath)
                    text += " " + meta.get('title', '') + " " + meta.get('subject', '')
                    # On n'extrait pas le texte complet ici (trop lent pour l'apprentissage)

                tokens = tokenize(text)
                bigrams = extract_bigrams(tokens)
                all_tokens = tokens + bigrams

                if all_tokens:
                    dir_docs[rel_root].append(all_tokens)
                    doc_count += 1

        self.n_docs = doc_count

        # Calculer l'IDF global
        doc_freq = Counter()
        for dir_path, docs in dir_docs.items():
            for doc_tokens in docs:
                unique_tokens = set(doc_tokens)
                for token in unique_tokens:
                    doc_freq[token] += 1

        self.idf = {}
        for token, freq in doc_freq.items():
            self.idf[token] = math.log(1 + self.n_docs / (1 + freq))

        # Construire les profils TF-IDF par dossier
        for dir_path, docs in dir_docs.items():
            if len(docs) < self.seuil_min_fichiers:
                continue

            # Fusionner tous les tokens du dossier
            all_tokens = []
            for doc_tokens in docs:
                all_tokens.extend(doc_tokens)

            tf = Counter(all_tokens)
            total = sum(tf.values())

            # TF-IDF
            tfidf = {}
            for token, count in tf.items():
                tf_val = count / total
                idf_val = self.idf.get(token, 1.0)
                tfidf[token] = tf_val * idf_val

            # Garder les top-N
            top_tokens = sorted(tfidf.items(), key=lambda x: -x[1])[:self.top_n]
            self.profiles[dir_path] = dict(top_tokens)

        print(f"  [Apprentissage] {len(self.profiles)} profils construits "
              f"à partir de {doc_count} fichiers.")

    def classify(self, text: str) -> list[tuple[str, float, str]]:
        """Classe un texte par similarité cosinus avec les profils."""
        if not self.actif or not self.profiles:
            return []

        tokens = tokenize(text)
        bigrams = extract_bigrams(tokens)
        all_tokens = tokens + bigrams

        if not all_tokens:
            return []

        # Construire le vecteur TF-IDF du document
        tf = Counter(all_tokens)
        total = sum(tf.values())
        doc_vec = {}
        for token, count in tf.items():
            tf_val = count / total
            idf_val = self.idf.get(token, 1.0)
            doc_vec[token] = tf_val * idf_val

        # Similarité cosinus avec chaque profil
        results = []
        doc_norm = math.sqrt(sum(v**2 for v in doc_vec.values()))
        if doc_norm == 0:
            return []

        for dir_path, profile in self.profiles.items():
            # Produit scalaire
            dot_product = sum(doc_vec.get(t, 0) * s for t, s in profile.items())
            profile_norm = math.sqrt(sum(v**2 for v in profile.values()))

            if profile_norm == 0:
                continue

            similarity = dot_product / (doc_norm * profile_norm)

            if similarity >= self.seuil_confiance:
                # Trouver le mot-clé le plus contributeur
                contributions = [(t, doc_vec.get(t, 0) * s)
                                 for t, s in profile.items() if doc_vec.get(t, 0) > 0]
                best_match = max(contributions, key=lambda x: x[1])[0] if contributions else "?"
                results.append((dir_path, similarity, best_match))

        results.sort(key=lambda x: -x[1])
        return results


# ════════════════════════════════════════════════════════════════════════════
# MOTEUR PRINCIPAL
# ════════════════════════════════════════════════════════════════════════════

class KlodoOrganizer:
    """Moteur principal de classification."""

    def __init__(self, base_path: str, config_path: str,
                 use_pdf: bool = True, use_learning: bool = True,
                 min_score: float = 0.10, verbose: bool = False):
        self.base_path = os.path.abspath(base_path)
        self.use_pdf = use_pdf
        self.use_learning = use_learning
        self.min_score = min_score
        self.verbose = verbose

        # Charger la config
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        # Initialiser les classifieurs
        self.kw_classifier = KeywordClassifier(self.config)

        learn_config = self.config.get('apprentissage', {})
        if not use_learning:
            learn_config['actif'] = False
        self.learn_classifier = LearningClassifier(learn_config)
        self.learn_weight = learn_config.get('poids_apprentissage', 0.4)

    def learn(self):
        """Phase d'apprentissage depuis les fichiers existants."""
        self.learn_classifier.learn_from_directory(self.base_path, use_pdf=False)

    def _get_text_for_file(self, filepath: str) -> str:
        """Collecte tout le texte disponible pour un fichier."""
        filename = os.path.basename(filepath)
        parent_dir = os.path.basename(os.path.dirname(filepath))
        grandparent_dir = os.path.basename(
            os.path.dirname(os.path.dirname(filepath)))

        # Texte de base
        text = f"{filename} {parent_dir} {grandparent_dir}"

        # Extraction PDF si activée
        if self.use_pdf and filepath.lower().endswith('.pdf'):
            meta = extract_pdf_metadata(filepath)
            meta_text = " ".join(v for v in meta.values() if v)
            if meta_text.strip():
                text += " " + meta_text

            # Si le nom est vague, extraire le contenu
            name_tokens = tokenize(filename)
            is_vague = len(name_tokens) <= 2

            if is_vague:
                pdf_text = extract_pdf_text(filepath, max_pages=MAX_PDF_PAGES)
                if pdf_text:
                    # Limiter à 2000 caractères pour la performance
                    text += " " + pdf_text[:2000]

        return text

    def classify_file(self, filepath: str) -> dict:
        """
        Classe un seul fichier. Retourne un dict avec :
        - destination: chemin relatif du dossier cible
        - score: score de confiance
        - method: 'keywords', 'learning', 'combined', ou 'unclassified'
        - details: explication du classement
        """
        text = self._get_text_for_file(filepath)

        # Déterminer le dossier actuel du fichier (relatif à la racine BIBLIO)
        rel_path = os.path.relpath(filepath, self.base_path)
        current_dir = os.path.dirname(rel_path)  # ex: "05-RELIGIONS/ISLAM"

        # ── Classement par mots-clés ──
        kw_results = self.kw_classifier.classify(text, current_dir=current_dir)

        # ── Classement par apprentissage ──
        learn_results = self.learn_classifier.classify(text)

        # ── Combinaison ──
        if kw_results and learn_results:
            # Normaliser les scores
            kw_max = kw_results[0][1] if kw_results else 1
            learn_max = learn_results[0][1] if learn_results else 1

            combined = {}
            for path, score, kw in kw_results[:5]:
                norm_score = score / kw_max * (1 - self.learn_weight)
                combined[path] = {'score': norm_score, 'kw_match': kw, 'learn_match': ''}

            for path, score, match in learn_results[:5]:
                norm_score = score / learn_max * self.learn_weight
                if path in combined:
                    combined[path]['score'] += norm_score
                    combined[path]['learn_match'] = match
                else:
                    combined[path] = {'score': norm_score, 'kw_match': '', 'learn_match': match}

            best_path = max(combined, key=lambda p: combined[p]['score'])
            best = combined[best_path]

            if best['score'] >= self.min_score:
                details = []
                if best['kw_match']:
                    details.append(f"mot-clé: {best['kw_match']}")
                if best['learn_match']:
                    details.append(f"apprentissage: {best['learn_match']}")
                return {
                    'destination': best_path,
                    'score': best['score'],
                    'method': 'combined',
                    'details': " + ".join(details),
                }

        # ── Mots-clés seuls ──
        if kw_results:
            best_path, best_score, best_kw = kw_results[0]
            if best_score >= self.min_score:
                return {
                    'destination': best_path,
                    'score': best_score,
                    'method': 'keywords',
                    'details': f"mot-clé: {best_kw}",
                }

        # ── Apprentissage seul ──
        if learn_results:
            best_path, best_score, best_match = learn_results[0]
            if best_score >= self.min_score:
                return {
                    'destination': best_path,
                    'score': best_score,
                    'method': 'learning',
                    'details': f"apprentissage: {best_match}",
                }

        # ── Non classé ──
        return {
            'destination': '_A-TRIER',
            'score': 0,
            'method': 'unclassified',
            'details': 'aucune correspondance trouvée',
        }

    def scan(self) -> list[dict]:
        """
        Parcourt toute la bibliothèque et propose un classement.
        Retourne une liste de propositions de déplacement.
        """
        proposals = []

        print(f"\n{'='*70}")
        print(f"  KLODO ORGANIZER v{__version__}")
        print(f"  Racine : {self.base_path}")
        print(f"{'='*70}\n")

        # Phase d'apprentissage
        if self.use_learning:
            self.learn()
            print()

        # Scan des fichiers
        print("  [Scan] Parcours des fichiers...")
        all_files = []
        for root, dirs, files in os.walk(self.base_path):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '_A-TRIER']
            for fname in files:
                if fname.lower().endswith('.pdf'):
                    all_files.append(os.path.join(root, fname))

        print(f"  [Scan] {len(all_files)} fichiers PDF trouvés.\n")

        # Classement
        stats = Counter()
        for i, filepath in enumerate(all_files, 1):
            rel_path = os.path.relpath(filepath, self.base_path)
            current_dir = os.path.dirname(rel_path)

            result = self.classify_file(filepath)
            dest = result['destination']

            # Vérifier si le fichier est déjà au bon endroit
            if current_dir == dest or current_dir.startswith(dest + '/'):
                stats['deja_classé'] += 1
                continue

            result['source'] = filepath
            result['rel_source'] = rel_path
            result['filename'] = os.path.basename(filepath)
            proposals.append(result)
            stats[result['method']] += 1

            if self.verbose:
                marker = '✓' if result['method'] != 'unclassified' else '?'
                print(f"  {marker} {result['filename'][:50]:50s} "
                      f"→ {dest}")
                print(f"    [{result['method']}] score={result['score']:.2f} "
                      f"| {result['details']}")

            if i % 100 == 0:
                print(f"  ... {i}/{len(all_files)} fichiers analysés")

        # Résumé
        print(f"\n{'─'*70}")
        print("  RÉSUMÉ")
        print(f"{'─'*70}")
        print(f"  Fichiers analysés     : {len(all_files)}")
        print(f"  Déjà bien classés     : {stats['deja_classé']}")
        print(f"  À déplacer (mots-clés): {stats['keywords']}")
        print(f"  À déplacer (apprent.) : {stats['learning']}")
        print(f"  À déplacer (combiné)  : {stats['combined']}")
        print(f"  Non classés (→ A-TRIER): {stats['unclassified']}")
        print(f"  Total à déplacer      : {len(proposals)}")
        print(f"{'─'*70}\n")

        return proposals

    def execute(self, proposals: list[dict]) -> str:
        """Exécute les déplacements et génère un log CSV."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = f"log_classement_{timestamp}.csv"

        moved = 0
        errors = 0

        with open(log_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'ancien_chemin', 'nouveau_chemin', 'methode',
                'score', 'details', 'status'
            ])

            for prop in proposals:
                src = prop['source']
                dest_dir = os.path.join(self.base_path, prop['destination'])
                dest = os.path.join(dest_dir, prop['filename'])

                # Éviter les écrasements
                if os.path.exists(dest):
                    base, ext = os.path.splitext(prop['filename'])
                    counter = 2
                    while os.path.exists(dest):
                        dest = os.path.join(dest_dir, f"{base} ({counter}){ext}")
                        counter += 1

                try:
                    os.makedirs(dest_dir, exist_ok=True)
                    os.rename(src, dest)
                    writer.writerow([
                        src, dest, prop['method'],
                        f"{prop['score']:.3f}", prop['details'], 'OK'
                    ])
                    moved += 1
                except Exception as e:
                    writer.writerow([
                        src, dest, prop['method'],
                        f"{prop['score']:.3f}", prop['details'], f'ERREUR: {e}'
                    ])
                    errors += 1

        print(f"  Fichiers déplacés : {moved}")
        print(f"  Erreurs           : {errors}")
        print(f"  Log               : {log_file}")
        return log_file

    def generate_report(self, proposals: list[dict]) -> str:
        """Génère un rapport CSV détaillé sans rien déplacer."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        report_file = f"rapport_classement_{timestamp}.csv"

        with open(report_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'fichier', 'emplacement_actuel', 'destination_proposée',
                'methode', 'score', 'details'
            ])
            for prop in proposals:
                writer.writerow([
                    prop['filename'],
                    os.path.dirname(prop['rel_source']),
                    prop['destination'],
                    prop['method'],
                    f"{prop['score']:.3f}",
                    prop['details'],
                ])

        print(f"  Rapport sauvegardé : {report_file}")
        return report_file


# ════════════════════════════════════════════════════════════════════════════
# ANNULATION
# ════════════════════════════════════════════════════════════════════════════

def undo_from_log(log_file: str):
    """Annule les déplacements depuis un fichier de log."""
    print(f"\n  [Undo] Annulation depuis {log_file}...")
    restored = 0
    errors = 0

    with open(log_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    for row in reversed(rows):
        if row.get('status') != 'OK':
            continue
        src = row['nouveau_chemin']
        dest = row['ancien_chemin']
        try:
            dest_dir = os.path.dirname(dest)
            os.makedirs(dest_dir, exist_ok=True)
            os.rename(src, dest)
            restored += 1
        except Exception as e:
            print(f"  ERREUR: {e}")
            errors += 1

    print(f"  Fichiers restaurés : {restored}")
    print(f"  Erreurs            : {errors}")


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Klodo Organizer — Classement thématique de PDFs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('path', help="Chemin vers la bibliothèque ou fichier de log (avec --undo)")
    parser.add_argument('--execute', action='store_true',
                        help="Appliquer les déplacements (sinon: rapport seulement)")
    parser.add_argument('--undo', action='store_true',
                        help="Annuler les déplacements depuis un log CSV")
    parser.add_argument('--learn-only', action='store_true',
                        help="Exécuter seulement la phase d'apprentissage")
    parser.add_argument('--config', default='categories.yaml',
                        help="Fichier de configuration (défaut: categories.yaml)")
    parser.add_argument('--no-pdf', action='store_true',
                        help="Désactiver l'extraction PDF")
    parser.add_argument('--no-learn', action='store_true',
                        help="Désactiver l'apprentissage")
    parser.add_argument('--verbose', '-v', action='store_true',
                        help="Mode verbeux")
    parser.add_argument('--min-score', type=float, default=0.10,
                        help="Score minimum de confiance (défaut: 0.10)")

    args = parser.parse_args()

    # Mode annulation
    if args.undo:
        undo_from_log(args.path)
        return

    # Vérifications
    if not os.path.isdir(args.path):
        print(f"ERREUR: {args.path} n'est pas un dossier valide.")
        sys.exit(1)

    config_path = args.config
    if not os.path.isfile(config_path):
        # Chercher à côté du script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, args.config)
        if not os.path.isfile(config_path):
            print(f"ERREUR: Fichier de config introuvable: {args.config}")
            sys.exit(1)

    # Vérifier les dépendances
    if not args.no_pdf and not HAS_PYPDF and not HAS_PDFPLUMBER:
        print("ATTENTION: pypdf et pdfplumber non installés. "
              "Extraction PDF désactivée.")
        print("  → pip install pypdf pdfplumber")
        args.no_pdf = True

    # Créer l'organisateur
    organizer = KlodoOrganizer(
        base_path=args.path,
        config_path=config_path,
        use_pdf=not args.no_pdf,
        use_learning=not args.no_learn,
        min_score=args.min_score,
        verbose=args.verbose,
    )

    if args.learn_only:
        organizer.learn()
        print("\n  Phase d'apprentissage terminée.")
        return

    # Scanner
    proposals = organizer.scan()

    if not proposals:
        print("  Rien à déplacer — tout est déjà bien classé !")
        return

    # Rapport ou exécution
    if args.execute:
        print("  [Exécution] Déplacement des fichiers...\n")
        organizer.execute(proposals)
    else:
        organizer.generate_report(proposals)
        print("\n  ℹ Pour appliquer les déplacements, relancez avec --execute")
        print("  ℹ Pour annuler après exécution, utilisez --undo log_classement_*.csv")


if __name__ == '__main__':
    main()
