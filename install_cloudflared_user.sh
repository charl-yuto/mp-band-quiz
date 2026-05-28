#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$HOME/.local/bin"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" ;;
  aarch64|arm64) URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64" ;;
  *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac
curl -L "$URL" -o "$HOME/.local/bin/cloudflared"
chmod +x "$HOME/.local/bin/cloudflared"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
echo "Installed: $HOME/.local/bin/cloudflared"
echo "Run: source ~/.bashrc && cloudflared --version"
