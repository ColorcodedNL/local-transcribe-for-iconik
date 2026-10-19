#!/bin/bash
# Maakt de code-update voor de huidige versie in dist/:
#   code-<versie>-shell<N>.zip       de app-code met manifest.json
#   code-<versie>-shell<N>.zip.sig   Ed25519-handtekening (privésleutel uit tools/sign_update.py)
# Beide gaan als losse bestanden bij de release op GitHub, naast de dmg. Draai eerst build_bundle.sh
# als de dmg ook mee moet. Weigert te ondertekenen met een sleutel die niet bij app_shell.py past.
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
PY="${TOOLPY:-$HERE/build/python/bin/python3.12}"
SIGN="${SIGN_CMD:-uv run tools/sign_update.py}"
command -v "$PY" >/dev/null || { echo "$PY ontbreekt, draai eerst scripts/build_python.sh"; exit 1; }

VERSION="$(sed -n 's/^APP_VERSION = "\([0-9.]*\)".*/\1/p' app/transcribe.py)"
SHELLV="$(sed -n 's/^SHELL_VERSION = \([0-9]*\).*/\1/p' app/app_shell.py)"
PUBLIC="$(sed -n 's/^PUBLIC_KEY = "\([^"]*\)".*/\1/p' app/app_shell.py)"
[ -n "$VERSION" ] && [ -n "$SHELLV" ] && [ -n "$PUBLIC" ] || { echo "versie, shell of publieke sleutel niet gevonden"; exit 1; }
git diff --quiet HEAD -- . 2>/dev/null || echo "Let op: er zijn niet-gecommitte wijzigingen. Commit eerst, dan hoort de update bij een commit."

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
for f in $("$PY" tools/make_manifest.py --list); do cp "app/$f" "$STAGE/"; done
"$PY" tools/make_manifest.py "$STAGE"

mkdir -p dist
ZIP="dist/code-$VERSION-shell$SHELLV.zip"
rm -f "$ZIP" "$ZIP.sig"
"$PY" - "$STAGE" "$HERE/$ZIP" <<'PYEOF'
import sys, zipfile
from pathlib import Path
stage, out = Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for path in sorted(stage.iterdir()):
        z.write(path, path.name)
PYEOF

OUTPUT="$($SIGN sign "$ZIP")"
SIGNED_WITH="$(echo "$OUTPUT" | sed -n 's/^Publieke sleutel: //p')"
if [ "$SIGNED_WITH" != "$PUBLIC" ]; then
  rm -f "$ZIP" "$ZIP.sig"
  echo "STOP: de ondertekensleutel hoort niet bij de publieke sleutel in app_shell.py."
  echo "  ondertekend met: $SIGNED_WITH"
  echo "  app verwacht:    $PUBLIC"
  exit 1
fi
$SIGN verify "$ZIP" --pub "$PUBLIC"
echo "Klaar: $ZIP ($(du -h "$ZIP" | cut -f1)) en $ZIP.sig"
echo "Bij de release:  gh release create v$VERSION <dmg> $ZIP $ZIP.sig --notes-file dist/notes-$VERSION.md"
