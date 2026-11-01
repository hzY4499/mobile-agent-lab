#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/home/kojoker/.local/android/platform-tools:${PATH}"

# Keep the service credential out of the YAML and command-line arguments.
if [[ -z "${API_KEY:-}${MODEL_API_KEY:-}${OPENAI_API_KEY:-}" ]]; then
  API_KEY="$(ssh -o BatchMode=yes -o ConnectTimeout=10 h20 'cat /data3/yhz/qwen38-deploy/api_key')"
  if [[ -z "${API_KEY//[[:space:]]/}" ]]; then
    printf '%s\n' 'H20 Qwen API key is empty.' >&2
    exit 1
  fi
  export API_KEY
fi

cd "${repo_dir}/Venus_framework/Venus_framework_mobile"
exec "${repo_dir}/.venv/bin/python" main.py \
  --config config/local_ui_venus_2.yaml \
  "$@"
