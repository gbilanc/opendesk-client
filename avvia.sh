#!/usr/bin/env bash
# avvia.sh — avvia rapidamente OpenDesk dal repository.
# Uso: ./avvia.sh [opzioni opendesk]
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -x "$DIR/.venv/bin/opendesk" ]]; then
    exec "$DIR/.venv/bin/opendesk" "$@"
elif command -v uv >/dev/null 2>&1; then
    exec uv run --directory "$DIR" opendesk "$@"
else
    exec python3 -m opendesk "$@"
fi
