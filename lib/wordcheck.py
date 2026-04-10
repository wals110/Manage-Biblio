"""
Word validation module — detects gibberish vs real words in filenames.

Uses pyspellchecker (EN + FR) plus a custom technical whitelist.
Loaded once at module level, O(1) lookups.
"""

import re
import unicodedata

from spellchecker import SpellChecker

# ── Spellcheckers (loaded once) ──────────────────────────────────────────
_en = SpellChecker(language="en")
_fr = SpellChecker(language="fr")

# ── Technical whitelist (terms not in standard dictionaries) ─────────────
TECH_WORDS: set[str] = {
    # Programming languages & frameworks
    "kubernetes", "docker", "ansible", "terraform", "django", "fastapi",
    "flask", "pytorch", "tensorflow", "numpy", "pandas", "sklearn",
    "opencv", "matplotlib", "scipy", "keras", "vuejs", "reactjs",
    "nodejs", "nextjs", "nuxtjs", "webpack", "vite", "eslint",
    "typescript", "javascript", "golang", "kotlin", "scala", "clojure",
    "haskell", "erlang", "elixir", "fortran", "matlab", "cmake",
    # Tech concepts
    "devops", "microservices", "blockchain", "cryptocurrency", "defi",
    "restful", "graphql", "websocket", "oauth", "saml", "jwt",
    "cicd", "mlops", "dataops", "nosql", "mongodb", "postgresql",
    "mysql", "sqlite", "duckdb", "redis", "kafka", "rabbitmq",
    "elasticsearch", "kibana", "grafana", "prometheus", "nginx",
    "apache", "ssl", "tls", "tcp", "udp", "http", "https", "ftp",
    "dns", "dhcp", "smtp", "imap", "ldap", "ssh", "vpn",
    # Formats & standards
    "pdf", "html", "css", "xml", "json", "yaml", "toml", "csv",
    "svg", "png", "jpg", "jpeg", "gif", "webp", "avif",
    "utf", "ascii", "unicode", "iso", "ieee", "rfc",
    # Acronyms common in book titles
    "api", "sdk", "cli", "gui", "ide", "orm", "mvc", "mvvm",
    "cpu", "gpu", "ram", "rom", "ssd", "hdd", "bios", "uefi",
    "sql", "uml", "xml", "xsl", "xsd", "dtd",
    "aws", "gcp", "azure", "saas", "paas", "iaas",
    "llm", "nlp", "cnn", "rnn", "gan", "rl", "ai", "ml", "dl",
    "iot", "ar", "vr", "xr",
    # OS & platforms
    "macos", "ios", "ipados", "watchos", "tvos", "freebsd",
    "openbsd", "centos", "ubuntu", "debian", "fedora", "redhat",
    "suse", "gentoo", "archlinux", "kali", "android",
    # Publishers & certifications
    "oreilly", "springer", "wiley", "packt", "apress", "manning",
    "addison", "wesley", "pearson", "mcgraw", "elsevier",
    "cisco", "comptia", "itil", "togaf", "scrum", "agile",
    "mcsa", "mcse", "mcsd", "mcad", "ccna", "ccnp", "ccie",
    "rhce", "rhcsa", "lpic", "cissp", "cism", "ceh",
    # Math & science
    "regex", "regexp", "eigenvector", "eigenvalue", "bayesian",
    "stochastic", "heuristic", "polymorphism", "mutex", "semaphore",
    "quicksort", "mergesort", "heapsort", "hashtable", "hashmap",
    "btree", "trie", "fibonacci", "markov", "fourier", "laplace",
    "lagrange", "euler", "gauss", "riemann", "hilbert", "turing",
    "boolean", "tuple", "struct", "enum", "typedef",
}

# ── Word extraction regex ────────────────────────────────────────────────
_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ]{2,}")


def _normalize(word: str) -> str:
    """Normalize a word for dictionary lookup: lowercase, strip accents."""
    word = word.lower()
    # Remove accents for matching (but the dictionaries handle accented words too)
    nfkd = unicodedata.normalize("NFKD", word)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _is_known_word(word: str) -> bool:
    """Check if a word is in any dictionary or whitelist."""
    lower = word.lower()
    normalized = _normalize(word)

    # Technical whitelist
    if lower in TECH_WORDS or normalized in TECH_WORDS:
        return True

    # Spellchecker lookup (checks lowercase)
    if lower in _en or lower in _fr:
        return True

    # Try without accents
    if normalized != lower and (normalized in _en or normalized in _fr):
        return True

    return False


def _is_acronym_or_name(word: str) -> bool:
    """Heuristic: word looks like an acronym or proper name.

    - ALL CAPS and 2-5 chars → acronym (API, SQL, LLM)
    - Title case (Einstein, LaMothe) → proper name
    - CamelCase (JavaScript, PowerShell) → tech term
    """
    if len(word) <= 1:
        return False
    # All uppercase, 2-5 chars → acronym
    if word.isupper() and 2 <= len(word) <= 6:
        return True
    # Title case with 3+ chars → proper name
    if word[0].isupper() and word[1:].islower() and len(word) >= 3:
        return True
    # CamelCase → tech term
    if word[0].isupper() and any(c.isupper() for c in word[1:]) and any(c.islower() for c in word):
        return True
    return False


def contains_real_words(text: str, min_ratio: float = 0.4) -> bool:
    """Check if text contains enough real words to be a legitimate title.

    Args:
        text: The filename stem (without extension).
        min_ratio: Minimum ratio of recognized words (0.0-1.0).
                   Default 0.4 = at least 40% of words must be real.

    Returns:
        True if the text looks like a real title, False if gibberish.
    """
    words = _WORD_RE.findall(text)

    # Filter out very short words (< 3 chars) — too ambiguous
    words = [w for w in words if len(w) >= 3]

    if not words:
        return False

    recognized = 0
    for word in words:
        if _is_known_word(word):
            recognized += 1
        elif _is_acronym_or_name(word):
            recognized += 1

    ratio = recognized / len(words)
    return ratio >= min_ratio


def word_score(text: str) -> float:
    """Return the ratio of recognized words in text (0.0 to 1.0).

    Useful for comparing two candidate names — higher is better.
    """
    words = _WORD_RE.findall(text)
    words = [w for w in words if len(w) >= 3]
    if not words:
        return 0.0

    recognized = sum(
        1 for w in words
        if _is_known_word(w) or _is_acronym_or_name(w)
    )
    return recognized / len(words)
