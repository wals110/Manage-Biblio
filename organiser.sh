#!/bin/bash
# =====================================================
#  Biblio Organizer — Lanceur
# =====================================================
#  Classe les PDFs par thème dans une arborescence.
#
#  Usage :
#    ./organiser.sh                                   # Rapport
#    ./organiser.sh --execute                         # Appliquer
#    ./organiser.sh --undo logs/log_classement_*.csv  # Annuler
# =====================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$SCRIPT_DIR/organiser/biblio_organizer.py"
CONFIG="$SCRIPT_DIR/organiser/categories.yaml"
BIBLIO_PATH="/Volumes/ExtSSD/BIBLIO"

# Couleurs
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

echo ""
echo "=========================================="
echo "  📂 Biblio Organizer v1.0"
echo "=========================================="
echo ""

# Vérifier Python 3
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python3 requis.${NC}"
    echo "   brew install python3  (macOS)"
    echo "   sudo apt install python3  (Linux)"
    exit 1
fi

# Installer les dépendances si nécessaire
echo "📦 Vérification des dépendances..."
python3 -c "import pypdf; import pdfplumber; import yaml" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "   Installation de pypdf, pdfplumber, pyyaml..."
    pip3 install pypdf pdfplumber pyyaml
    if [ $? -ne 0 ]; then
        echo -e "${RED}❌ Erreur d'installation.${NC}"
        echo "   Essayez : pip3 install --user pypdf pdfplumber pyyaml"
        exit 1
    fi
fi
echo -e "   ${GREEN}✅ OK${NC}"
echo ""

# Vérifier que le SSD est monté
if [ ! -d "$BIBLIO_PATH" ]; then
    echo -e "${RED}❌ Dossier BIBLIO introuvable : $BIBLIO_PATH${NC}"
    echo "   Vérifiez que le SSD externe est branché et monté."
    exit 1
fi
echo "📁 Bibliothèque : $BIBLIO_PATH"
echo ""

# Passer tous les arguments au script Python
cd "$SCRIPT_DIR"
python3 "$SCRIPT" "$BIBLIO_PATH" --config "$CONFIG" "$@"
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
