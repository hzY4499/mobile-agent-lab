#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/home/kojoker/.local/android/platform-tools:${PATH}"
export PYTHONUNBUFFERED=1
cd "${repo_dir}"
exec "${repo_dir}/.venv/bin/python" -m benchmarks.gui_v1.cli "$@"
