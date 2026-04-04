#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
#  Klodo v4.1 — Lanceur unifié
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
#    --report          Générer un rapport CSV
#    --workers N       Threads parallèles
#    --max N           Limiter à N fichiers
#    --retry-errors    Retraiter les erreurs
#    --reclassify      Re-mapper les thèmes sans appel LLM
#    --verbose         Mode détaillé
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

# Python 3
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python3 requis.${NC}"
    echo "   brew install python3"
    exit 1
fi

# klodo.py existe
if [[ ! -f "$KLODO_PY" ]]; then
    echo -e "${RED}❌ klodo.py introuvable : $KLODO_PY${NC}"
    exit 1
fi

# Dépendances Python
MISSING=""
python3 -c "import yaml" 2>/dev/null || MISSING="$MISSING pyyaml"
python3 -c "import requests" 2>/dev/null || MISSING="$MISSING requests"
python3 -c "import PIL" 2>/dev/null || MISSING="$MISSING Pillow"
python3 -c "import pdf2image" 2>/dev/null || MISSING="$MISSING pdf2image"

if [[ -n "$MISSING" ]]; then
    echo -e "${YELLOW}⚠  Dépendances manquantes :${MISSING}${NC}"
    echo "   pip3 install${MISSING}"
    # On continue quand même (certaines commandes n'en ont pas besoin)
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
        echo "   (ou utiliser --api-key sk-xxx)"
        echo ""
    fi
fi

# ── SSD monté (vérification optionnelle) ──
# Extraire le --profile si présent pour vérifier le target
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
    TARGET=$(python3 -c "import yaml; print(yaml.safe_load(open('$PROFILE_YAML'))['target'])" 2>/dev/null || echo "")
    if [[ -n "$TARGET" && ! -d "$TARGET" ]]; then
        echo -e "${YELLOW}⚠  Cible non accessible : $TARGET${NC}"
        echo "   Vérifier que le SSD est connecté."
        echo ""
    fi
fi

# ── Lancement ──
echo -e "${CYAN}📚 Klodo v4.1${NC}"
echo ""

exec python3 "$KLODO_PY" "$@"
