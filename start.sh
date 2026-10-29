#!/usr/bin/env bash
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/ and try again." >&2
  exit 127
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Keep the dependency environment in uv's cache, not in the source checkout.
# No connection details or credentials belong in this launcher.
export PYTHONDONTWRITEBYTECODE=1
exec uv run --no-project --directory "$SCRIPT_DIR" --with "$SCRIPT_DIR" \
  python -B -m opencode_ci serve "$@"
