#!/usr/bin/env bash
# Start Vouch Studio, the local web UI, at http://127.0.0.1:8000 and open the browser.
# Usage: ./run_ui.sh [--port 8000] [--no-open]
# First run creates .venv and installs requirements-ui.txt (FastAPI, uvicorn).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$script_dir"
if [ -x /usr/libexec/path_helper ]; then
  eval "$(/usr/libexec/path_helper)"  # puts /Library/TeX/texbin (pdflatex) on PATH
fi
if [ -f "$script_dir/.env" ]; then
  set -a; . "$script_dir/.env"; set +a  # API keys and DATABASE_URL (gitignored; see .env.example)
fi

venv="$script_dir/.venv"
if [ ! -x "$venv/bin/python" ]; then
  echo "Creating .venv for the web UI..."
  python3 -m venv "$venv"
fi
if ! "$venv/bin/python" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "Installing requirements-ui.txt into .venv..."
  "$venv/bin/python" -m pip install --quiet -r requirements-ui.txt
fi

open_flag="--open"
args=()
for arg in "$@"; do
  if [ "$arg" = "--no-open" ]; then open_flag=""; else args+=("$arg"); fi
done
exec "$venv/bin/python" server.py ${open_flag} "${args[@]+"${args[@]}"}"
