#!/bin/bash
# Bouwt de zelfstandige app in build/: één bundel, één proces (nog niet in /Applications).
# Vereist: build/python met alle packages (zie stappen in de note).
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HERE/build/Local Transcribe for Iconik.app"
PLIST="$OUT/Contents/Info.plist"
RES="$OUT/Contents/Resources"

[ -x "$HERE/build/python/bin/python3.12" ] || { echo "build/python ontbreekt, draai eerst scripts/build_python.sh"; exit 1; }
[ -x "$HERE/build/ffmpeg-out/bin/ffmpeg" ] || { echo "build/ffmpeg-out ontbreekt, draai eerst scripts/build_ffmpeg.sh"; exit 1; }

rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$RES/app"
VERSION="$(sed -n 's/^APP_VERSION = "\([0-9.]*\)".*/\1/p' "$HERE/app/transcribe.py")"
[ -n "$VERSION" ] || { echo "APP_VERSION niet gevonden in transcribe.py"; exit 1; }

# Hoofdprogramma: laadt de meegeleverde Python in dit proces en start main.py. Zo is de app
# één proces met de naam en het icoon van de app, in het Dock en in Activity Monitor.
clang -O2 -arch arm64 -mmacosx-version-min=13.0 -o "$OUT/Contents/MacOS/LocalTranscribe" "$HERE/scripts/launcher.c"
cp "$HERE/scripts/LocalTranscribe.icns" "$RES/LocalTranscribe.icns"
cat > "$PLIST" <<INFO
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>LocalTranscribe</string>
  <key>CFBundleIdentifier</key><string>nl.colorcoded.local-transcribe</string>
  <key>CFBundleName</key><string>Local Transcribe for Iconik</string>
  <key>CFBundleDisplayName</key><string>Local Transcribe for Iconik</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>LocalTranscribe</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>CFBundleURLTypes</key>
  <array>
    <dict>
      <key>CFBundleURLName</key><string>Local Transcribe for Iconik</string>
      <key>CFBundleURLSchemes</key><array><string>iconiktranscribe</string></array>
    </dict>
  </array>
</dict>
</plist>
INFO

# Eigen code
# De bestanden en het manifest komen uit tools/make_manifest.py, zodat de bundel en een
# code-update (build_update.sh) uit dezelfde lijst bouwen.
TOOLPY="$HERE/build/python/bin/python3.12"
for f in $("$TOOLPY" "$HERE/tools/make_manifest.py" --shell-list) $("$TOOLPY" "$HERE/tools/make_manifest.py" --list); do
  cp "$HERE/app/$f" "$RES/app/$f"
done
"$TOOLPY" "$HERE/tools/make_manifest.py" "$RES/app"   # vóór het ondertekenen, anders klopt de handtekening niet

# Python met packages (cp -c maakt clones op APFS, dus snel en zonder extra ruimte)
cp -Rc "$HERE/build/python" "$RES/python"

# ffmpeg (eigen LGPL-build) en sprekermodel
mkdir -p "$RES/bin" "$RES/models" "$RES/licenses"
cp "$HERE/build/ffmpeg-out/bin/ffmpeg" "$HERE/build/ffmpeg-out/bin/ffprobe" "$RES/bin/"
cp "$HERE/docs/licenses/ffmpeg-COPYING.LGPLv2.1.txt" "$RES/licenses/ffmpeg-COPYING.LGPLv2.1.txt"
cat > "$RES/licenses/ffmpeg-README.txt" <<'TXT'
FFmpeg 9.0.2 (ffmpeg and ffprobe), licensed under the LGPL version 2.1 or later.
Source: https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz
Built unmodified with: ./configure --disable-gpl --disable-nonfree --disable-autodetect --enable-zlib --disable-doc --disable-ffplay --disable-debug --disable-network
TXT
MODEL_SRC="$HOME/Library/Application Support/Local Transcribe for Iconik/models/speaker-diarization-community-1"
[ -f "$MODEL_SRC/config.yaml" ] || { echo "sprekermodel ontbreekt: $MODEL_SRC"; exit 1; }
cp -RL "$MODEL_SRC" "$RES/models/"

# Onderdelen die de app nooit gebruikt eruit: testmappen van pakketten, C-headers om
# uitbreidingen te bouwen, en Tk/IDLE. Scheelt ongeveer 250 MB. Alleen mappen met de
# naam "tests": een module tests.py (jinja2) en torch/testing blijven staan.
PYLIB="$RES/python/lib"
SP="$PYLIB/python3.12/site-packages"
find "$SP" -type d \( -name tests -o -name PyObjCTest \) -prune -exec rm -rf {} +
rm -rf "$SP/torch/include" "$SP/torch/share" \
       "$PYLIB/python3.12/idlelib" "$PYLIB/python3.12/tkinter" "$PYLIB/python3.12/turtledemo" \
       "$PYLIB"/python3.12/lib-dynload/_tkinter.*.so \
       "$PYLIB/tcl9" "$PYLIB/tcl9.0" "$PYLIB/tk9.0" "$PYLIB/itcl4.3.8" "$PYLIB/thread3.0.6" \
       "$PYLIB/libtcl9.0.dylib" "$PYLIB/libtcl9tk9.0.dylib"

# Telemetrie van pyannote ook in de standaardinstelling uit, niet alleen via de code.
TELEMETRY="$SP/pyannote/audio/telemetry/config.yaml"
if [ -f "$TELEMETRY" ]; then
  sed -i '' 's/^metrics_enabled: true/metrics_enabled: false/' "$TELEMETRY"
  grep -q '^metrics_enabled: false' "$TELEMETRY" || { echo "Telemetrie van pyannote kon niet uit"; exit 1; }
fi

# Bytecode vooraf maken, zodat de eerste start niet traag is en de bundel na ondertekenen niet meer verandert
"$RES/python/bin/python3.12" -m compileall -q -j 0 "$RES/python/lib" "$RES/app" || true

touch "$OUT"
codesign --force --deep -s - "$OUT"
echo "Gebouwd: $OUT (versie $VERSION)"
du -sh "$OUT"
