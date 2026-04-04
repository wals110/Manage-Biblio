#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
#  Klodo — Lanceur unifié
# ═══════════════════════════════════════════════════════════════════════
#
#  Usage :
#    ./klodo.sh process /chemin/vers/pdfs              # Pipeline complet (dry-run)
#    ./klodo.sh process --execute                      # Pipeline + appliquer
#    ./klodo.sh classify /chemin --workers 10           # LLM Vision
#    ./klodo.sh rename /chemin                          # Renommage seul
#    ./klodo.sh refine                                  # Raffinement sous-catégories
#    ./klodo.sh profiles                                # Lister les profils
#    ./klodo.sh init mon-profil --target /chemin        # Nouveau profil
#
#  Options :
#    --profile NAME    Profil à utiliser (défaut: default)
#    --execute         Appliquer (sinon dry-run)
#    --workers N       Threads parallèles
#    --max N           Limiter à N fichiers
#    --retry-errors    Retraiter les erreurs
#    --reclassify      Re-mapper les thèmes sans appel LLM
#    --verbose         Mode détaillé
#
#  Prérequis :
#    brew install poppler uv
#    uv sync                     # Installe Python + dépendances
#
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KLODO_PY="$SCRIPT_DIR/klodo.py"

# Couleurs
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ── Vérifications ──

# uv
if ! command -v uv &> /dev/null; then
    echo -e "${RED}❌ uv requis.${NC}"
    echo "   curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# klodo.py existe
if [[ ! -f "$KLODO_PY" ]]; then
    echo -e "${RED}❌ klodo.py introuvable : $KLODO_PY${NC}"
    exit 1
fi

# .venv existe (sinon proposer uv sync)
if [[ ! -d "$SCRIPT_DIR/.venv" ]]; then
    echo -e "${YELLOW}⚠  Environnement virtuel absent.${NC}"
    echo "   Lancement de 'uv sync' pour installer les dépendances..."
    echo ""
    (cd "$SCRIPT_DIR" && uv sync)
fi

# poppler (pour pdf2image)
if ! command -v pdftoppm &> /dev/null; then
    echo -e "${YELLOW}⚠  poppler non installé (requis pour l'extraction de couvertures)${NC}"
    echo "   brew install poppler"
fi

# Clé API (avertissement si absente, pas bloquant)
COMMAND="${1:-}"
if [[ "$COMMAND" == "process" || "$COMMAND" == "classify" ]]; then
    if [[ -z "${SILICONFLOW_API_KEY:-}" ]]; then
        echo -e "${YELLOW}⚠  SILICONFLOW_API_KEY non définie${NC}"
        echo "   export SILICONFLOW_API_KEY=sk-xxx"
        echo ""
    fi
fi

# ── SSD monté (vérification optionnelle) ──
PROFILE="default"
prev_was_profile="false"
for arg in "$@"; do
    if [[ "$prev_was_profile" == "true" ]]; then
        PROFILE="$arg"
        break
    fi
    prev_was_profile="false"
    if [[ "$arg" == "--profile" ]]; then
        prev_was_profile="true"
    fi
done

PROFILE_YAML="$SCRIPT_DIR/profiles/$PROFILE/profile.yaml"
if [[ -f "$PROFILE_YAML" ]]; then
    TARGET=$(uv run python -c "import yaml; print(yaml.safe_load(open('$PROFILE_YAML'))['target'])" 2>/dev/null || echo "")
    if [[ -n "$TARGET" && ! -d "$TARGET" ]]; then
        echo -e "${YELLOW}⚠  Cible non accessible : $TARGET${NC}"
        echo "   Vérifier que le SSD est connecté."
        echo ""
    fi
fi

# ── Lancement ──
echo -e "${CYAN}📚 Klodo v1.0.0-dev${NC}"
echo ""

exec uv run python "$KLODO_PY" "$@"
