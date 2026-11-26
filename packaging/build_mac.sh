#!/usr/bin/env bash
# Gera "Mega Sinc.app" e MegaSinc-macOS-<arch>.dmg em dist/
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
$PY -m venv .venv-build
source .venv-build/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller pillow certifi
# evita erro de certificado SSL no Python do python.org ao baixar o FFmpeg
export SSL_CERT_FILE="$(python -c 'import certifi; print(certifi.where())')"

python packaging/fetch_ffmpeg.py
pyinstaller packaging/megasinc.spec --noconfirm --clean

APP="dist/Mega Sinc.app"
# assinatura ad-hoc (necessária em Apple Silicon; para distribuir sem avisos use
# um Developer ID: codesign --deep -s "Developer ID Application: ..." + notarização)
codesign --force --deep --sign - "$APP" || true

ARCH=$(uname -m)
DMG="dist/MegaSinc-macOS-${ARCH}.dmg"
rm -f "$DMG"
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Mega Sinc" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
echo "Pronto: $DMG"
