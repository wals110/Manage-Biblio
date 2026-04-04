#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# flatten_to_inbox.sh — Déplace tous les PDFs de l'arborescence vers _INBOX
# ═══════════════════════════════════════════════════════════════════════════
#
# Usage :
#   ./scripts/flatten_to_inbox.sh /chemin/vers/BIBLIO              # dry-run
#   ./scripts/flatten_to_inbox.sh /chemin/vers/BIBLIO --execute     # exécution
#   ./scripts/flatten_to_inbox.sh /chemin/vers/BIBLIO --max 500     # limiter
#   ./scripts/flatten_to_inbox.sh /chemin/vers/BIBLIO --max 500 --execute
#
# Ce script :
#   1. Scanne tous les PDFs dans les dossiers classifiés (01-*, 02-*, ..., _A-TRIER)
#   2. Les déplace vers _INBOX (avec gestion des doublons : ajout de suffixe)
#   3. NE TOUCHE PAS aux fichiers déjà dans _INBOX
#   4. Préserve les dossiers vides (l'arborescence reste intacte)
#
# ⚠  Conçu pour une copie de test, PAS pour la bibliothèque de production !
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Couleurs ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ── Arguments ──
BIBLIO="${1:-}"
EXECUTE=false
MAX=0

shift || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --execute) EXECUTE=true ;;
        --max)
            shift
            MAX="${1:-0}"
            ;;
        *) echo -e "${RED}Option inconnue : $1${NC}"; exit 1 ;;
    esac
    shift
done

if [[ -z "$BIBLIO" ]]; then
    echo -e "${RED}Usage : $0 /chemin/vers/BIBLIO [--execute] [--max N]${NC}"
    exit 1
fi

if [[ ! -d "$BIBLIO" ]]; then
    echo -e "${RED}Erreur : $BIBLIO n'existe pas${NC}"
    exit 1
fi

INBOX="$BIBLIO/_INBOX"

if [[ ! -d "$INBOX" ]]; then
    if $EXECUTE; then
        echo -e "${YELLOW}Création de $INBOX${NC}"
        mkdir -p "$INBOX"
    fi
fi

# ── Sécurité : vérifier que c'est bien une copie ──
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  flatten_to_inbox — Remise à plat vers _INBOX${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Bibliothèque : ${YELLOW}$BIBLIO${NC}"
echo -e "  Destination  : ${YELLOW}$INBOX${NC}"
echo -e "  Mode         : $( $EXECUTE && echo -e "${RED}EXÉCUTION${NC}" || echo -e "${GREEN}DRY-RUN${NC}" )"
[[ $MAX -gt 0 ]] && echo -e "  Limite       : ${YELLOW}$MAX fichiers${NC}"
echo ""

# ── Comptage initial ──
TOTAL_BEFORE=$(find "$BIBLIO" -name "*.pdf" -not -path "*/_INBOX/*" | wc -l | tr -d ' ')
if [[ -d "$INBOX" ]]; then
    INBOX_BEFORE=$(find "$INBOX" -name "*.pdf" 2>/dev/null | wc -l | tr -d ' ')
else
    INBOX_BEFORE=0
fi

echo -e "  PDFs hors INBOX : ${GREEN}$TOTAL_BEFORE${NC}"
echo -e "  PDFs dans INBOX : ${GREEN}$INBOX_BEFORE${NC}"
echo ""

if [[ "$TOTAL_BEFORE" -eq 0 ]]; then
    echo -e "${GREEN}Rien à déplacer — tous les PDFs sont déjà dans _INBOX !${NC}"
    exit 0
fi

# ── Confirmation en mode exécution ──
if $EXECUTE; then
    echo -e "${RED}⚠  ATTENTION : $TOTAL_BEFORE fichiers vont être déplacés vers _INBOX${NC}"
    echo -e "${RED}   Les dossiers resteront vides mais intacts.${NC}"
    read -p "   Confirmer ? (oui/non) : " CONFIRM
    if [[ "$CONFIRM" != "oui" ]]; then
        echo -e "${YELLOW}Annulé.${NC}"
        exit 0
    fi
    echo ""
fi

# ── Déplacement ──
MOVED=0
SKIPPED=0
COLLISIONS=0

# Trouver tous les PDFs hors _INBOX, triés pour reproductibilité
while IFS= read -r pdf_path; do
    # Limite max
    if [[ $MAX -gt 0 && $MOVED -ge $MAX ]]; then
        break
    fi

    filename=$(basename "$pdf_path")
    dest="$INBOX/$filename"

    # Gestion des doublons : ajouter un suffixe numérique
    if [[ -e "$dest" ]]; then
        base="${filename%.pdf}"
        counter=1
        while [[ -e "$INBOX/${base} ($counter).pdf" ]]; do
            ((counter++))
        done
        dest="$INBOX/${base} ($counter).pdf"
        ((COLLISIONS++))
    fi

    # Dossier source (relatif pour affichage)
    src_rel="${pdf_path#$BIBLIO/}"

    if $EXECUTE; then
        mv "$pdf_path" "$dest"
        ((MOVED++))
        # Progression tous les 500 fichiers
        if (( MOVED % 500 == 0 )); then
            echo -e "  ${GREEN}$MOVED${NC} / $TOTAL_BEFORE déplacés..."
        fi
    else
        echo -e "  ${BLUE}[DRY-RUN]${NC} $src_rel → _INBOX/$(basename "$dest")"
        ((MOVED++))
        # En dry-run, limiter l'affichage à 30 lignes
        if [[ $MOVED -eq 30 && $TOTAL_BEFORE -gt 30 && $MAX -eq 0 ]]; then
            echo -e "  ${YELLOW}... et $(( TOTAL_BEFORE - 30 )) autres fichiers${NC}"
            MOVED=$TOTAL_BEFORE
            break
        fi
    fi

done < <(find "$BIBLIO" -name "*.pdf" -not -path "*/_INBOX/*" | sort)

# ── Bilan ──
echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
echo -e "  ${GREEN}Bilan :${NC}"
if $EXECUTE; then
    INBOX_AFTER=$(find "$INBOX" -name "*.pdf" 2>/dev/null | wc -l | tr -d ' ')
    echo -e "    Déplacés      : ${GREEN}$MOVED${NC}"
    echo -e "    Collisions    : ${YELLOW}$COLLISIONS${NC} (renommés avec suffixe)"
    echo -e "    INBOX avant   : $INBOX_BEFORE"
    echo -e "    INBOX après   : ${GREEN}$INBOX_AFTER${NC}"
else
    echo -e "    À déplacer    : ${GREEN}$MOVED${NC}"
    echo -e "    Collisions    : ${YELLOW}$COLLISIONS${NC}"
    echo -e ""
    echo -e "  ${YELLOW}Relancer avec --execute pour appliquer.${NC}"
fi
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
