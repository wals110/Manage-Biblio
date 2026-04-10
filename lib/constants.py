"""
Constantes centralisées pour Klodo.

Regroupe les valeurs par défaut (timeouts, seuils, retries, etc.)
utilisées dans plusieurs modules. Modifier ici plutôt que dans chaque fichier.
"""

# ── HTTP / LLM ───────────────────────────────────────────────
LLM_TIMEOUT = 120           # Timeout par défaut pour les appels LLM (secondes) — 120s pour modeles locaux
LLM_MAX_RETRIES = 3         # Nombre de tentatives avant abandon
LLM_MAX_TOKENS = 150        # Tokens max par défaut pour les réponses LLM
LLM_VISION_MAX_TOKENS = 800 # Tokens max pour l'analyse vision (inclut le reasoning des modeles thinking)
LLM_TEMPERATURE = 0.1       # Température par défaut (déterministe)
LLM_RETRY_DELAY = 2         # Délai entre retries sur erreur (secondes)
LLM_BACKOFF_MAX = 30        # Plafond du backoff exponentiel sur 429 (secondes)

# ── Classification ───────────────────────────────────────────
CONFIDENCE_THRESHOLD = 0.5      # Seuil de confiance pour accepter une classification
MAPPER_MIN_CONFIDENCE = 0.6     # Seuil minimum pour le LLM Mapper
MAPPER_PENALTY = 0.9            # Pénalité appliquée au score du mapper
KEYWORD_DEFAULT_SCORE = 0.5     # Score par défaut pour les résultats keyword

# ── Vision / PDF ─────────────────────────────────────────────
PDF_DPI = 150               # Résolution d'extraction des pages PDF
PDF_EXTRACT_THREADS = 2     # Threads pour pdf2image
PDF_MAX_PAGES = 5           # Nombre max de pages envoyées au LLM
JPEG_QUALITY = 80           # Qualité de compression JPEG pour les images envoyées

# ── Renommage ────────────────────────────────────────────────
PDF_PROCESS_TIMEOUT = 15    # Timeout extraction PDF dans le renamer (secondes)
ISBN_API_TIMEOUT = 10       # Timeout appels Google Books / OpenLibrary
ISBN_AUTHOR_TIMEOUT = 5     # Timeout lookup auteur OpenLibrary
REQUEST_DELAY = 0.2         # Délai entre requêtes API externes (secondes)
RENAME_DELAY = 0.15         # Délai après opération de renommage (secondes)

# ── Valeurs spéciales ────────────────────────────────────────
NO_FOLDER_MARKER = "_AUCUN"     # Réponse LLM mapper : aucun dossier adapté
UNSORTED_FOLDER = "_A-TRIER"    # Dossier par défaut pour les non-classifiés
