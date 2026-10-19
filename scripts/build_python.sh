#!/bin/bash
# Maakt build/python: de verplaatsbare Python van uv met alle pakketten uit uv.lock.
# Draai dit eerst, daarna build_ffmpeg.sh en build_bundle.sh.
set -e
cd "$(dirname "$0")/.."
if [ -e build/python ]; then
  echo "build/python bestaat al. Verwijder die map eerst voor een schone opbouw."
  exit 1
fi
mkdir -p build
PY=$(uv python find --no-project 3.12)
SRC=$(dirname "$(dirname "$(realpath "$PY")")")
echo "Python: $SRC"
cp -R "$SRC" build/python
uv export --frozen --no-hashes --no-emit-project -o build/requirements.txt
uv pip install --python build/python/bin/python3.12 --break-system-packages -r build/requirements.txt
build/python/bin/python3.12 -I -c "import mlx_whisper, pyannote.audio, webview, keyring, requests, torch, nacl; print('imports ok')"
