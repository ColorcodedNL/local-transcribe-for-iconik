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
  <key>LSEnvironment</key>
  <dict>
    <key>MallocLargeCache</key><string>0</string>
  </dict>
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
# uitbreidingen te bouwen, Tk/IDLE, en pip met wat erbij hoort (de app installeert niets).
# Scheelt ongeveer 260 MB. Alleen mappen met de naam "tests": een module tests.py (jinja2)
# en torch/testing blijven staan. mlx/include bevat ook een script dat bij compileall een
# SyntaxWarning gaf. setuptools blijft: torch laadt het. site-packages/build is de bouwmap van
# mlx-whisper 0.4.3, per ongeluk in het wiel meegekomen (drie lagen build/lib met kopieën).
PYLIB="$RES/python/lib"
SP="$PYLIB/python3.12/site-packages"
find "$SP" -type d \( -name tests -o -name PyObjCTest \) -prune -exec rm -rf {} +
rm -rf "$SP/torch/include" "$SP/torch/share" "$SP/mlx/include" \
       "$SP/pip" "$SP"/pip-*.dist-info "$PYLIB/python3.12/ensurepip" "$PYLIB/python3.12/lib2to3" \
       "$SP/build" \
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

# Geen paden van deze Mac in de bundel: iedereen die de dmg opent, kan ze lezen (gebruikersnaam en mappen).
# - Scripts van pakketten in python/bin (hf, numba, pip, ...): de app gebruikt ze niet, en hun eerste regel
#   is het volledige pad van de bouwmap. Alleen de Python zelf blijft.
# - De naam waaronder libpython zichzelf kent, was het pad in de uv-map van deze gebruiker.
# - _sysconfigdata onthoudt waar uv deze Python had neergezet.
# - Bytecode onthoudt het pad van elk bronbestand: oude bytecode uit build/python weg, en de nieuwe krijgt
#   het pad dat klopt als de app in Programma's staat (compileall -s en -p).
find "$RES/python/bin" -mindepth 1 ! -name python ! -name python3 ! -name python3.12 -delete
install_name_tool -id @rpath/libpython3.12.dylib "$PYLIB/libpython3.12.dylib"
sed -i '' "s|$HOME|/Users/build|g" "$PYLIB"/python3.12/_sysconfigdata*.py
find "$RES" -type d -name __pycache__ -prune -exec rm -rf {} +

# Bytecode vooraf maken, zodat de eerste start niet traag is en de bundel na ondertekenen niet meer verandert
INSTALLED_RES="/Applications/Local Transcribe for Iconik.app/Contents/Resources"
# -B: de Python die dit draait, schrijft zelf geen bytecode voor wat hij daarbij laadt (die kreeg anders het
# echte pad van de bouwmap, en compileall sloeg die bestanden daarna over). -f: alles opnieuw, wat er ook staat.
"$RES/python/bin/python3.12" -B -m compileall -q -f -j 0 -s "$RES" -p "$INSTALLED_RES" "$RES/python/lib" "$RES/app" || true

# Controle: staat er toch nog een pad van deze Mac in de bundel, dan niet verder. Zo komt een nieuw lek
# (een pakket dat iets anders bewaart, een eigen build met een pad erin) bij het bouwen aan het licht.
# Alleen bij nood overslaan met ALLOW_LOCAL_PATHS=1.
LEAKS="$(grep -rlaF -e "$HOME" -e "/Users/$(id -un)/" "$RES" | head -20 || true)"
if [ -n "$LEAKS" ] && [ "${ALLOW_LOCAL_PATHS:-}" != 1 ]; then
  echo "STOP: een pad van deze Mac ($HOME) staat in de bundel, in onder meer:"
  echo "$LEAKS" | sed "s|^$RES/|  |"
  echo "Staat ffmpeg erbij: bouw die opnieuw met scripts/build_ffmpeg.sh (verwijder eerst build/ffmpeg-out)."
  exit 1
fi

touch "$OUT"
# Ad hoc, net als de release: testbuilds gedragen zich zo hetzelfde als wat gebruikers krijgen.
codesign --force --deep -s - "$OUT"
echo "Gebouwd: $OUT (versie $VERSION)"
if pgrep -qf "$OUT/Contents/MacOS/"; then
  echo "Let op: deze app draait nog met de oude code. Sluit hem (Cmd+Q) en open hem opnieuw."
fi
du -sh "$OUT"
