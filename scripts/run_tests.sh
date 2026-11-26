#!/bin/bash
# Draait alle tests, ook de browsertest van het venster (tests/test_e2e.py).
# build_dmg.sh en build_update.sh weigeren te bouwen als er een faalt.
# Eenmalig voor de browsertest: uv run --no-project --with playwright playwright install chromium
set -u
cd "$(dirname "$0")/.."
WITH=(--no-project --with pynacl --with requests --with numpy --with huggingface_hub --with playwright --with jsonschema)
failed=0
# Eerst de code zelf: een naam die niet bestaat of een import die nergens heen gaat (pyflakes via ruff).
RUFF=ruff@0.17.5
if out="$(uv tool run "$RUFF" check --quiet --output-format concise app tests tools scripts 2>&1)"; then
  printf "%-28s %s\n" "ruff" "OK"
else
  failed=1
  printf "%-28s %s\n" "ruff" "FOUT"
  printf '%s\n' "$out" | head -10 | sed 's/^/    /'
fi
for t in tests/test_*.py; do
  out="$(uv run "${WITH[@]}" python -B "$t" 2>&1)"
  code=$?
  printf "%-28s %s\n" "$t" "$(printf '%s\n' "$out" | tail -1)"
  if [ $code -ne 0 ]; then
    failed=1
    printf '%s\n' "$out" | grep -E '^FOUT|Error|error:' | head -10 | sed 's/^/    /'
  fi
done
if [ $failed -ne 0 ]; then
  echo "TESTS MISLUKT"
  exit 1
fi
echo "Alle tests geslaagd"
