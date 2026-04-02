#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  Biblio — Suite de tests fonctionnels
# ═══════════════════════════════════════════════════════════
#
#  Usage :
#    ./tests/run_all.sh           # Lancer tous les tests
#    ./tests/run_all.sh -v        # Mode verbeux (détail par test)
#    ./tests/run_all.sh test_copy # Lancer un seul module
#
# ═══════════════════════════════════════════════════════════

set -e

# Se placer à la racine du projet
cd "$(dirname "$0")/.."

VERBOSE=""
MODULE=""

for arg in "$@"; do
    case "$arg" in
        -v|--verbose) VERBOSE="-v" ;;
        *) MODULE="$arg" ;;
    esac
done

echo "═══════════════════════════════════════════════════"
echo "  📚 Biblio — Tests fonctionnels"
echo "═══════════════════════════════════════════════════"
echo ""

if [ -n "$MODULE" ]; then
    # Lancer un seul module
    echo "🧪 Module : $MODULE"
    echo ""
    python3 -m pytest "tests/${MODULE}.py" $VERBOSE -x 2>/dev/null \
        || python3 -m unittest "tests.${MODULE}" $VERBOSE
else
    # Lancer tous les tests
    # Essayer pytest d'abord (meilleur affichage), sinon unittest
    if python3 -c "import pytest" 2>/dev/null; then
        python3 -m pytest tests/ $VERBOSE -x --tb=short
    else
        python3 -m unittest discover -s tests -p "test_*.py" $VERBOSE
    fi
fi

echo ""
echo "✅ Tous les tests passent !"
