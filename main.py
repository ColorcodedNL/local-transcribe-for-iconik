#!/usr/bin/env python3
"""Opstartlaag van Local Transcribe for Iconik. De launcher draait dit bestand.

Kiest welke code de app draait: de code in de bundel, of een nieuwere code-update uit
~/Library/Application Support/Local Transcribe for Iconik/code/<versie>/ die eerder is
gedownload en gecontroleerd (zie updater.py). De bundel zelf wordt nooit aangepast, dus de
codehandtekening van de app blijft geldig.

Start een update niet goed, dan komt de versie op de lijst met mislukte versies (bad.json)
en start de app opnieuw met de code uit de bundel.

Dit bestand verandert zo zelden mogelijk. Wijzigingen hier vragen een nieuwe dmg en een
hogere SHELL_VERSION in app_shell.py.
"""
import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent     # Contents/Resources/app
RES = HERE.parent                          # Contents/Resources
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def log(text):
    """Regel in het logbestand, ook als de app zelf nog niet gestart is."""
    try:
        logdir = Path.home() / "Library" / "Logs" / "Local Transcribe for Iconik"
        logdir.mkdir(parents=True, exist_ok=True)
        with open(logdir / "serve.log", "a") as f:
            f.write(f"[{datetime.now():%H:%M:%S}] opstart: {text}\n")
    except OSError:
        pass


def mark_bad(code_dir, version):
    bad = read_json(code_dir / "bad.json")
    bad = bad if isinstance(bad, list) else []
    if version not in bad:
        bad.append(version)
    try:
        (code_dir / "bad.json").write_text(json.dumps(bad))
    except OSError:
        pass


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True  # bestaat, maar is niet van ons


def choose_code(here, code_dir, shell_version, version_tuple):
    """(map, versie) van de code-update die gebruikt moet worden, of None voor de code in de bundel."""
    bundled = read_json(here / "manifest.json")
    if not bundled:
        return None  # geen bundel (draait uit de projectmap): altijd de eigen code
    current = read_json(code_dir / "current.json")
    version = str(current.get("version", "")) if isinstance(current, dict) else ""
    if not VERSION_RE.match(version):
        return None

    # Een vorige start met deze versie is nooit klaargekomen: dan is hij stuk.
    marker = code_dir / "starting"
    if marker.is_file():
        try:
            parts = marker.read_text().split()
        except OSError:
            parts = []
        running = len(parts) > 1 and parts[1].isdigit() and pid_alive(int(parts[1]))
        if not running:  # draait er nog een start, dan laten we hem met rust
            try:
                marker.unlink()
            except OSError:
                pass
            if parts and parts[0] == version:
                mark_bad(code_dir, version)
                log(f"code {version} kwam de vorige keer niet op; terug naar de code in de bundel")

    bad = read_json(code_dir / "bad.json")
    if isinstance(bad, list) and version in bad:
        return None
    folder = code_dir / version
    manifest = read_json(folder / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("version") != version:
        return None
    try:
        if int(manifest.get("requires_shell", 1)) > shell_version:
            return None
    except (TypeError, ValueError):
        return None
    if version_tuple(version) <= version_tuple(str(bundled.get("version", "0"))):
        return None  # de bundel is even nieuw of nieuwer (bijvoorbeeld na een nieuwe dmg)
    if not (folder / "app_main.py").is_file():
        return None
    return folder, version


def fall_back(code_dir, version):
    """De update start niet: op de lijst zetten en opnieuw starten met de code uit de bundel."""
    log(f"code {version} start niet:\n{traceback.format_exc()}terug naar de code in de bundel")
    mark_bad(code_dir, version)
    launcher = RES.parent / "MacOS" / "LocalTranscribe"
    if launcher.is_file():
        os.execv(str(launcher), [str(launcher)])


def run():
    os.environ["LOCALTRANSCRIBE_RES"] = str(RES)   # waar de bundel staat, ook als de code elders draait
    sys.path.insert(0, str(HERE))
    import app_shell
    chosen = None
    try:
        chosen = choose_code(HERE, app_shell.CODE_DIR, app_shell.SHELL_VERSION, app_shell.version_tuple)
    except Exception as error:
        log(f"code kiezen mislukte ({type(error).__name__}: {error}); code uit de bundel")
    os.environ.pop("LOCALTRANSCRIBE_CODE", None)
    if chosen:
        folder, version = chosen
        sys.path.insert(0, str(folder))
        os.environ["LOCALTRANSCRIBE_CODE"] = version
    try:
        import app_main
        app_main.main()
    except Exception:
        if chosen:
            fall_back(app_shell.CODE_DIR, chosen[1])
        raise


if __name__ == "__main__":
    run()
