#!/usr/bin/env bash
# Run the job matching pipeline on one job description.
# Usage: ./match.sh target_jobs/devops_role.md [--verbose] [--dry-run] [...]
# Equivalent: python3 pipeline.py target_jobs/devops_role.md
set -u

if [ "$#" -lt 1 ]; then
  echo "usage: ./match.sh <job_description.md|.txt> [--verbose] [--dry-run] [--help]" >&2
  echo "       (same as: python3 pipeline.py <job_description> ...)" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
python_bin="${PYTHON:-}"
if [ -z "$python_bin" ]; then
  if command -v python3 >/dev/null 2>&1; then
    python_bin="python3"
  elif command -v python >/dev/null 2>&1; then
    python_bin="python"
  else
    echo "error: python3 is not installed or not on PATH" >&2
    exit 4
  fi
fi

exec "$python_bin" "$script_dir/pipeline.py" "$@"
