#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR/frontend"
echo "Cleaning frontend dependencies..."
rm -rf node_modules package-lock.json
echo "Installing frontend dependencies..."
npm install
echo "Done."
