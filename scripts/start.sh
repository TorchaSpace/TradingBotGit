#!/usr/bin/env bash
# Botu başlatır. Kullanım:  bash scripts/start.sh [run|carry-run|report|check|...]
# Mac'te uykuyu engeller (caffeinate). Durdurmak için Ctrl+C ya da proje klasöründe: touch STOP
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then echo "Önce kurulum: bash scripts/setup.sh"; exit 1; fi
# shellcheck disable=SC1091
source .venv/bin/activate
CMD="${1:-run}"; shift || true
if [ -f STOP ] && [[ "$CMD" == run || "$CMD" == carry-run ]]; then
  echo "STOP dosyası var, siliniyor ki bot çalışabilsin."; rm -f STOP
fi
if [ "$CMD" = "report" ]; then exec python -m tradingbot report --open "$@"; fi
if command -v caffeinate >/dev/null 2>&1 && [[ "$CMD" == run || "$CMD" == carry-run ]]; then
  exec caffeinate -i python -m tradingbot "$CMD" "$@"
fi
exec python -m tradingbot "$CMD" "$@"
