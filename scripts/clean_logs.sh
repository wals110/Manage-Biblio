#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# clean_logs.sh — Supprime les rapports CSV et le checkpoint de logs/
# ═══════════════════════════════════════════════════════════════════════════
#
# Usage :
#   ./scripts/clean_logs.sh              # dry-run (affiche ce qui sera supprimé)
#   ./scripts/clean_logs.sh --execute    # suppression effective
#
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Couleurs ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# ── Répertoire logs ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOGS_DIR="$SCRIPT_DIR/../logs"

if [[ ! -d "$LOGS_DIR" ]]; then
    echo -e "${GREEN}Rien à nettoyer — le dossier logs/ n'existe pas.${NC}"
    exit 0
fi

# ── Arguments ──
EXECUTE=false
if [[ "${1:-}" == "--execute" ]]; then
    EXECUTE=true
fi

# ── Collecte des fichiers à supprimer ──
FILES=()
while IFS= read -r f; do
    FILES+=("$f")
done < <(find "$LOGS_DIR" -maxdepth 1 -name "rapport_*.csv" | sort)

echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  clean_logs — Nettoyage du dossier logs/${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Mode : $( $EXECUTE && echo -e "${RED}EXÉCUTION${NC}" || echo -e "${GREEN}DRY-RUN${NC}" )"
echo ""

if [[ ${#FILES[@]} -eq 0 ]]; then
    echo -e "${GREEN}Rien à supprimer — logs/ est déjà propre.${NC}"
    exit 0
fi

for f in "${FILES[@]}"; do
    name=$(basename "$f")
    size=$(du -h "$f" | cut -f1 | tr -d ' ')
    if $EXECUTE; then
        rm "$f"
        echo -e "  ${RED}✗${NC} $name ($size)"
    else
        echo -e "  ${BLUE}[DRY-RUN]${NC} $name ($size)"
    fi
done

echo ""
if $EXECUTE; then
    echo -e "${GREEN}  ${#FILES[@]} fichier(s) supprimé(s).${NC}"
else
    echo -e "  ${YELLOW}${#FILES[@]} fichier(s) à supprimer.${NC}"
    echo -e "  ${YELLOW}Relancer avec --execute pour appliquer.${NC}"
fi
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"