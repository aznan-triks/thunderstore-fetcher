#!/usr/bin/env bash
# ============================================================
#  Thunderstore Archive — Container update
#  Rebuilds the Docker image and restarts it with the new code.
#  Usage: double-click (Git Bash / WSL) or  ./update.sh
# ============================================================
set -euo pipefail

# Move into the script's folder, wherever it was launched from
cd "$(dirname "$0")"

echo
echo "=== Updating the Thunderstore Archive container ==="
echo

if docker compose up -d --build; then
    echo
    echo "✓ Done. Interface: http://localhost:8081"
else
    code=$?
    echo
    echo "✗ FAILED (code ${code}) — see the messages above."
fi

echo
read -r -p "Press Enter to close…" _
