#!/bin/bash
# Maakt dist/Local Transcribe for Iconik.dmg: de app, een snelkoppeling naar Programma's
# en de handleidingen (NL en EN). Draai eerst build_bundle.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="Local Transcribe for Iconik"
APP="build/$NAME.app"
STAGE="build/dmg-stage"
OUT="dist/$NAME.dmg"

[ -d "$APP" ] || { echo "Geen $APP, draai eerst scripts/build_bundle.sh"; exit 1; }
for f in "docs/Handleiding Local Transcribe.pdf" "docs/Guide Local Transcribe.pdf"; do
  [ -f "$f" ] || { echo "Ontbreekt: $f"; exit 1; }
done
codesign --verify --deep --strict "$APP" || { echo "Handtekening van de app klopt niet"; exit 1; }

rm -rf "$STAGE"
mkdir -p "$STAGE" dist
cp -Rc "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp "docs/Handleiding Local Transcribe.pdf" "docs/Guide Local Transcribe.pdf" "$STAGE/"

rm -f "$OUT"
hdiutil create -volname "$NAME" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$OUT"
rm -rf "$STAGE"

hdiutil verify "$OUT" >/dev/null && echo "dmg gecontroleerd"
echo "Klaar: $OUT"
du -h "$OUT"
shasum -a 256 "$OUT"
