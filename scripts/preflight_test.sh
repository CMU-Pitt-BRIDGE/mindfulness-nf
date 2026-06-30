#!/usr/bin/env bash
# Pre-scanner-session preflight.
#
# Runs the Layer 3 end-to-end test suite.  If any test fails, prints a
# large red banner and exits 1 so the operator does not proceed to a
# real scanner session with broken code.
#
# Usage:  bash scripts/preflight_test.sh

set -u

cd "$(dirname "$0")/.." || exit 1

# Colors (ANSI)
RED='\033[1;31m'
GREEN='\033[1;32m'
RESET='\033[0m'

echo "Running preflight test suite (Layer 3 end-to-end)…"
echo

if uv run pytest tests/test_e2e_session.py -v; then
    echo
    echo -e "${GREEN}============================================================${RESET}"
    echo -e "${GREEN}  PREFLIGHT PASSED — safe to proceed to scanner session.${RESET}"
    echo -e "${GREEN}============================================================${RESET}"
    exit 0
else
    echo
    echo -e "${RED}============================================================${RESET}"
    echo -e "${RED}  DO NOT PROCEED TO SCANNER — integration tests failed.${RESET}"
    echo -e "${RED}  Fix the failing tests before attempting a scanner run.${RESET}"
    echo -e "${RED}============================================================${RESET}"
    exit 1
fi
