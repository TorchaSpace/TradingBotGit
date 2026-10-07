#!/usr/bin/env bash
# Puts a TradingBot shortcut on the Desktop / in Applications (the app itself lives in the project folder).
#   bash scripts/make_app.sh
set -euo pipefail
cd "$(dirname "$0")/.."
chmod +x TradingBot.app/Contents/MacOS/TradingBot
xattr -dr com.apple.quarantine TradingBot.app 2>/dev/null || true
ln -sfn "$PWD/TradingBot.app" "$HOME/Applications/TradingBot.app" 2>/dev/null || \
  { mkdir -p "$HOME/Applications" && ln -sfn "$PWD/TradingBot.app" "$HOME/Applications/TradingBot.app"; }
echo "Hazır: Finder → Uygulamalar (kişisel) → TradingBot, ya da proje klasöründeki TradingBot.app"
