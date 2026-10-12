#!/usr/bin/env python3
"""Test het updatemechanisme op deze Mac, zonder GitHub.

    uv run tools/local_update_test.py good      versie 0.3.1: een gewone update
    uv run tools/local_update_test.py syntax    versie 0.3.2: serve.py met een syntaxfout
    uv run tools/local_update_test.py crash     versie 0.3.3: de app stopt direct na de start
    uv run tools/local_update_test.py reset     alle gedownloade code-updates weghalen
    uv run tools/local_update_test.py status    wat er nu staat

Het pakket wordt gemaakt van de bestanden in deze map, ondertekend met je echte sleutel en
via dezelfde updater.install() neergezet als bij een echte update, in de map met code-updates
naast de app. Start de app daarna opnieuw en kijk in het log:
    tail -5 ~/Library/Logs/"Local Transcribe for Iconik"/serve.log
"""
import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app_shell  # noqa: E402
import updater  # noqa: E402

MODES = {"good": "0.3.1", "syntax": "0.3.2", "crash": "0.3.3"}
CODE_FILES = subprocess.run([sys.executable, str(ROOT / "tools" / "make_manifest.py"), "--list"],
                            capture_output=True, text=True, check=True).stdout.split()


def build(mode, version):
    work = Path(tempfile.mkdtemp())
    for name in CODE_FILES:
        shutil.copy(ROOT / name, work / name)
    path = work / "transcribe.py"
    text = path.read_text()
    text, count = re.subn(r'^APP_VERSION = "[\d.]+"', f'APP_VERSION = "{version}"', text, flags=re.M)
    assert count == 1
    path.write_text(text)
    if mode == "syntax":
        (work / "serve.py").write_text((work / "serve.py").read_text() + "\ndef broken(:\n    pass\n")
    if mode == "crash":
        text = (work / "app_main.py").read_text()
        assert "    begin_start()\n" in text
        (work / "app_main.py").write_text(text.replace("    begin_start()\n", "    begin_start()\n    os._exit(3)  # test: stopt zonder venster\n"))
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_manifest.py"), str(work)], check=True, stdout=subprocess.DEVNULL)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as package:
        for entry in sorted(work.iterdir()):
            package.write(entry, entry.name)
    return buf.getvalue()


def sign(data, key_file, allow_other_key):
    from nacl.signing import SigningKey
    key = SigningKey(base64.b64decode(key_file.read_text().strip()))
    public = base64.b64encode(bytes(key.verify_key)).decode()
    if public != app_shell.PUBLIC_KEY and not allow_other_key:
        sys.exit("De sleutel hoort niet bij app_shell.PUBLIC_KEY. Een app zou deze update weigeren.")
    return base64.b64encode(key.sign(data).signature), public


def status(code_dir):
    print(f"Map: {code_dir}")
    if not code_dir.exists():
        print("  (bestaat niet: geen code-updates)")
        return
    for entry in sorted(code_dir.iterdir()):
        if entry.is_dir():
            print(f"  map {entry.name}")
        else:
            text = entry.read_text().strip()
            print(f"  {entry.name}: {text[:100]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=[*MODES, "reset", "status"])
    parser.add_argument("--running", default=None, help="versie van de draaiende app (standaard APP_VERSION)")
    parser.add_argument("--key", default=os.environ.get("LOCALTRANSCRIBE_SIGNING_KEY") or str(Path.home() / ".config" / "local-transcribe" / "update-signing.key"))
    parser.add_argument("--code-dir", default=str(app_shell.CODE_DIR))
    parser.add_argument("--allow-other-key", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    code_dir = Path(args.code_dir)
    if args.mode == "status":
        return status(code_dir)
    if args.mode == "reset":
        shutil.rmtree(code_dir, ignore_errors=True)
        print(f"Weggehaald: {code_dir}")
        return
    version = MODES[args.mode]
    data = build(args.mode, version)
    signature, public = sign(data, Path(args.key).expanduser(), args.allow_other_key)
    store = {"zip": data, "sig": signature}
    running = args.running or subprocess.run(
        [sys.executable, "-c", "import re,sys;print(re.search(r'^APP_VERSION = \"([\\d.]+)\"', open(sys.argv[1]).read(), re.M).group(1))", str(ROOT / "transcribe.py")],
        capture_output=True, text=True).stdout.strip()
    installed = updater.install({"version": version, "zip_url": "zip", "sig_url": "sig"}, running,
                                app_shell.SHELL_VERSION, public, code_dir, get=lambda url, limit: store[url])
    print(f"Geïnstalleerd: {installed} ({args.mode}), draaiende versie {running}")
    status(code_dir)
    print("\nStart de app nu opnieuw en kijk in het log:")
    print('  tail -5 ~/Library/Logs/"Local Transcribe for Iconik"/serve.log')


main()
