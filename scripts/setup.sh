#!/usr/bin/env bash
# Installation locale (Linux/macOS). Usage : bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
command -v ffmpeg >/dev/null || { echo "FFmpeg manquant : voir README > Installation FFmpeg"; exit 1; }
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -e "backend[dev,url,llm]"
[ -f .env ] || cp .env.example .env
if command -v npm >/dev/null; then (cd frontend && npm install --no-audit --no-fund); fi
echo "OK. Activez l'environnement : source .venv/bin/activate"
