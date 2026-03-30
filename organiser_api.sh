#!/bin/bash
# =====================================================
#  Biblio Organizer API — Lanceur
# =====================================================
#  Classe les PDFs par thème via Google Books / Open Library.
#
#  Usage :
#    ./organiser_api.sh /chemin/vers/biblio                  # Rapport
#    ./organiser_api.sh /chemin/vers/biblio --execute          # Appliquer
#    ./organiser_api.sh /chemin/vers/biblio --max-api 50       # Limiter les requêtes
#    ./organiser_api.sh --undo logs/log_classement_api_*.csv   # Annuler
# =====================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$SCRIPT_DIR/organiser/biblio_organizer_api.py"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

echo ""
echo "=========================================="
echo "  🌐 Biblio Organizer API v1.0"
echo "=========================================="
echo ""

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python3 requis.${NC}"
    exit 1
fi

echo "📦 Vérification des dépendances..."
python3 -c "import requests; import yaml; import pypdf" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "   Installation..."
    pip3 install requests pyyaml pypdf pdfplumber
fi
echo -e "   ${GREEN}✅ OK${NC}"
echo ""

cd "$SCRIPT_DIR"
python3 "$SCRIPT" "$@"
EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}=========================================="
    echo "  ✅ Terminé !"
    echo "==========================================${NC}"
else
    echo -e "${RED}=========================================="
    echo "  ⚠️  Erreur (code $EXIT_CODE)"
    echo "==========================================${NC}"
fi
