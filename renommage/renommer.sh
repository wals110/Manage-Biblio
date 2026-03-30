#!/bin/bash
# =====================================================
#  Biblio Renamer — Lanceur
# =====================================================
#  Renomme les PDFs d'une bibliothèque vers :
#    "Titre - Auteur.pdf"
#
#  Usage :
#    ./renommer.sh /chemin/vers/biblio          # Rapport
#    ./renommer.sh /chemin/vers/biblio --execute # Appliquer
#    ./renommer.sh --undo log_renommage_*.csv   # Annuler
# =====================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$SCRIPT_DIR/biblio_renamer.py"

# Couleurs
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

echo ""
echo "=========================================="
echo "  📚 Biblio Renamer v2.0"
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
python3 -c "import pypdf; import pdfplumber; import requests" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "   Installation de pypdf, pdfplumber, requests..."
    pip3 install pypdf pdfplumber requests
    if [ $? -ne 0 ]; then
        echo -e "${RED}❌ Erreur d'installation.${NC}"
        echo "   Essayez : pip3 install --user pypdf pdfplumber requests"
        exit 1
    fi
fi
echo -e "   ${GREEN}✅ OK${NC}"
echo ""

# Passer tous les arguments au script Python
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
