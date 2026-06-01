#!/usr/bin/env bash
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR/frontend"
npm install
npm run build
cd "$APP_DIR/backend"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
if [ -z "$MP_API_KEY" ]; then
  echo "Warning: MP_API_KEY is not set. Users must input API key in the UI."
fi
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
