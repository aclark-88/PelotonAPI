#!/usr/bin/env bash
#
# One-command local launcher for the Coremont Signal Engine.
#
#   ./run.sh                # set up (if needed), seed, and serve on :8000
#   PORT=9000 ./run.sh      # use a different port
#   SITE_LIVE=1 ./run.sh    # ingest live SEC data instead of the bundled sample
#
# Requires Python 3.11+. Creates an isolated .venv so nothing touches your system.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"
PYTHON="${PYTHON:-python3}"

# --- Python version guard ----------------------------------------------------
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)'; then
  echo "Need Python 3.11+. Found: $("$PYTHON" --version 2>&1)" >&2
  exit 1
fi

# --- Virtualenv + deps -------------------------------------------------------
if [ ! -d ".venv" ]; then
  echo "→ Creating virtualenv (.venv)…"
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "→ Installing dependencies…"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# --- Initialize + seed -------------------------------------------------------
echo "→ Initializing database…"
python -m app.cli initdb

if [ "${SITE_LIVE:-}" = "1" ]; then
  echo "→ Ingesting LIVE SEC Form D data (needs outbound SEC access)…"
  if ! python -m app.cli ingest; then
    echo "  (live ingest failed — falling back to bundled seed)"
    python -m app.cli ingest --seed
  fi
else
  echo "→ Loading bundled sample data (set SITE_LIVE=1 for live SEC)…"
  python -m app.cli ingest --seed
fi

# --- Serve -------------------------------------------------------------------
echo
echo "==================================================================="
echo "  Coremont Signal Engine is running:  http://localhost:${PORT}"
echo "  (Ctrl-C to stop)"
echo "==================================================================="
echo
exec uvicorn app.web.server:app --reload --host 127.0.0.1 --port "${PORT}"
