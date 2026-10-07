#!/usr/bin/env bash
# Tek komutla kurulum:  bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "❌ Python 3.10+ bulunamadı."
  echo "   Mac:   brew install python@3.12   (Homebrew yoksa: https://brew.sh)"
  echo "   Linux: sudo apt install python3 python3-venv"
  exit 1
fi
echo "✓ Python: $($PY --version)"

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
  echo "✓ Sanal ortam oluşturuldu (.venv)"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -e ".[dev]"
echo "✓ Paketler kuruldu"

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "✓ .env oluşturuldu (MODE=paper: gerçek para YOK). Ayarlar için .env dosyasını düzenle."
else
  echo "✓ .env zaten var, dokunulmadı"
fi

echo "… testler çalışıyor"
python -m pytest -q
echo
echo "Hazır. Sonraki adımlar:"
echo "  bash scripts/start.sh            # trend botu başlat (paper)"
echo "  bash scripts/start.sh report     # raporu oluştur ve aç"
echo "  bash scripts/start.sh carry-run  # funding carry motoru (.env'de CARRY_CAPITAL gerekir)"
