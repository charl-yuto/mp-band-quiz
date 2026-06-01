#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR/frontend"
npm install
npm run build
cd "$APP_DIR/backend"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
python3 -m pip install -r requirements.txt
if [ -z "${MP_API_KEY:-}" ]; then
  echo "MP_API_KEY is not set."
  echo "Run: export MP_API_KEY='your_api_key'"
  exit 1
fi
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000 &
PID=$!
cleanup(){ kill "$PID" 2>/dev/null || true; }
trap cleanup EXIT
sleep 3
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed. Run: ./install_cloudflared_user.sh && source ~/.bashrc"
  exit 1
fi
cloudflared tunnel --url http://127.0.0.1:8000
