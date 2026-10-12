#!/usr/bin/env python3
"""Maakt manifest.json voor een map met app-code: de bundel (Resources/app) of een code-update.

    make_manifest.py DIR          schrijft DIR/manifest.json
    make_manifest.py --list       de bestanden die in een code-update zitten
    make_manifest.py --shell-list de bestanden die alleen in de bundel zitten

Alleen de standaardbibliotheek, zodat het met elke Python draait.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

# Een code-update bevat precies deze bestanden. main.py en app_shell.py horen er bewust niet bij:
# die starten de app en controleren de updates, dus die veranderen alleen met een nieuwe dmg.
CODE_FILES = ["app_main.py", "transcribe.py", "serve.py", "updater.py", "ui.html", "setup_actions.py"]
SHELL_FILES = ["main.py", "app_shell.py"]

ROOT = Path(__file__).resolve().parent.parent


def constant(path, pattern, what):
    found = re.search(pattern, Path(path).read_text(), re.M)
    if not found:
        sys.exit(f"{what} niet gevonden in {path}")
    return found.group(1)


def main():
    args = sys.argv[1:]
    if args == ["--list"]:
        print(" ".join(CODE_FILES))
        return
    if args == ["--shell-list"]:
        print(" ".join(SHELL_FILES))
        return
    if len(args) != 1:
        sys.exit(__doc__)
    folder = Path(args[0])
    missing = [name for name in CODE_FILES if not (folder / name).is_file()]
    if missing:
        sys.exit(f"Ontbreekt in {folder}: {', '.join(missing)}")
    manifest = {
        "version": constant(folder / "transcribe.py", r'^APP_VERSION = "(\d+\.\d+\.\d+)"', "APP_VERSION"),
        "requires_shell": int(constant(ROOT / "app_shell.py", r"^SHELL_VERSION = (\d+)", "SHELL_VERSION")),
        "files": {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in CODE_FILES},
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"manifest.json: versie {manifest['version']}, shell {manifest['requires_shell']}, {len(CODE_FILES)} bestanden")


main()
