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

# --- Locate a suitable Python (try python3, then python, then 3.11/3.12) -----
find_python() {
  for cand in "${PYTHON:-}" python3 python python3.12 python3.11; do
    [ -z "$cand" ] && continue
    if command -v "$cand" >/dev/null 2>&1 && \
       "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      echo "$cand"; return 0
    fi
  done
  return 1
}

if ! PYTHON="$(find_python)"; then
  echo "ERROR: Need Python 3.11+ but couldn't find it on PATH." >&2
  echo "  Installed: $(python3 --version 2>&1 || echo none)" >&2
  echo "  Install 3.11+ (macOS: 'brew install python@3.12'; Ubuntu: 'sudo apt install python3.12 python3.12-venv')," >&2
  echo "  or run with an explicit interpreter:  PYTHON=/path/to/python3.12 ./run.sh" >&2
  exit 1
fi
echo "→ Using $("$PYTHON" --version 2>&1) ($PYTHON)"

# --- Virtualenv + deps -------------------------------------------------------
if [ ! -d ".venv" ]; then
  echo "→ Creating virtualenv (.venv)…"
  if ! "$PYTHON" -m venv .venv 2>/tmp/venv_err; then
    echo "ERROR: 'python -m venv' failed:" >&2; cat /tmp/venv_err >&2
    echo "  On Debian/Ubuntu install the venv package, e.g.:  sudo apt install python3-venv" >&2
    exit 1
  fi
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "→ Installing dependencies…"
python -m pip install --quiet --upgrade pip
if ! python -m pip install --quiet -r requirements.txt; then
  echo "ERROR: dependency install failed. Re-run without --quiet to see details:" >&2
  echo "  source .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

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
