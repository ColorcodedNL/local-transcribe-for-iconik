"""Code-updates voor Local Transcribe for Iconik: zoeken, downloaden, controleren, neerzetten.

Een release op GitHub kan naast de dmg twee bestanden hebben:

    code-<versie>-shell<N>.zip        de app-code (app_main.py, serve.py, ...) met manifest.json
    code-<versie>-shell<N>.zip.sig    Ed25519-handtekening over de zip (zie tools/sign_update.py)

N is de SHELL_VERSION die de update nodig heeft (zie app_shell.py). Heeft deze app een lagere
shell, dan biedt hij de dmg aan in plaats van de code-update.

De code komt in Application Support, nooit in de bundel. main.py kiest bij de volgende start.
Alleen een zip met een kloppende handtekening wordt geopend, en alleen platte bestandsnamen uit
het (ondertekende) manifest worden weggeschreven. Daarmee kan een zip niets buiten zijn eigen
map schrijven, ook niet met slimme paden.
"""
import base64
import binascii
import hashlib
import io
import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests

import app_shell

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
ASSET_RE = re.compile(r"^code-(\d+\.\d+\.\d+)-shell(\d+)\.zip$")
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
NEVER_IN_UPDATE = {"main.py", "app_shell.py"}          # alleen met een nieuwe dmg
REQUIRED = {"app_main.py", "transcribe.py", "serve.py", "ui.html"}
MAX_ZIP = 50 * 1024 * 1024
MAX_SIGNATURE = 1024
INSECURE_HOSTS = set()                                  # leeg; alleen tests zetten hier een host in
KEEP_VERSIONS = 2                                       # huidige en vorige, voor terugvallen


class UpdateError(Exception):
    """Fout met een tekst in beide talen (nl, en), zodat het venster hem kan tonen."""

    def __init__(self, nl, en):
        super().__init__(nl)
        self.nl, self.en = nl, en


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def bad_versions(code_dir=app_shell.CODE_DIR):
    bad = read_json(code_dir / "bad.json")
    return set(bad) if isinstance(bad, list) else set()


def find_update(release, repo, running_version, shell_version, bad=()):
    """Wat een release biedt: {"version", "url", "mode": "code" of "dmg", ...} of None als hij niet nieuwer is."""
    latest = (release.get("tag_name") or "").lstrip("vV")
    page = release.get("html_url") or ""
    if not VERSION_RE.match(latest) or not page.startswith(f"https://github.com/{repo}/"):
        return None
    if app_shell.version_tuple(latest) <= app_shell.version_tuple(running_version):
        return None
    found = {"version": latest, "url": page, "mode": "dmg"}
    prefix = f"https://github.com/{repo}/releases/download/"
    assets = {a.get("name") or "": a.get("browser_download_url") or "" for a in release.get("assets") or []}
    for name, link in assets.items():
        match = ASSET_RE.match(name)
        signature = assets.get(name + ".sig", "")
        if (match and match.group(1) == latest and int(match.group(2)) <= shell_version
                and link.startswith(prefix) and signature.startswith(prefix) and latest not in bad):
            found.update(mode="code", zip_url=link, sig_url=signature)
            break
    return found


def fetch(url, limit):
    """Haalt een bestand op, met een maximum aan bytes."""
    address = urlparse(url)
    if address.scheme != "https" and not (address.scheme == "http" and address.hostname in INSECURE_HOSTS):
        raise UpdateError("Onverwacht adres voor de update.", "Unexpected address for the update.")
    with requests.get(url, stream=True, timeout=30, headers={"User-Agent": app_shell.APP_NAME}) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_content(65536):
            data += chunk
            if len(data) > limit:
                raise UpdateError("De update is te groot.", "The update is too large.")
    return bytes(data)


def verify_signature(data, signature_b64, public_key_b64):
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
    try:
        VerifyKey(base64.b64decode(public_key_b64)).verify(data, base64.b64decode(signature_b64.strip()))
    except (BadSignatureError, binascii.Error, ValueError):
        raise UpdateError("De handtekening van de update klopt niet. Er is niets geïnstalleerd.",
                          "The update's signature is not valid. Nothing was installed.")


def read_package(data, expected_version, shell_version):
    """Controleert de inhoud van een zip (de handtekening is al gecontroleerd). Geeft (versie, {naam: bytes})."""
    bad = UpdateError("De update is niet in orde. Er is niets geïnstalleerd.", "The update is not valid. Nothing was installed.")
    try:
        package = zipfile.ZipFile(io.BytesIO(data))
        names = package.namelist()
        manifest = json.loads(package.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise bad
    version, files = manifest.get("version"), manifest.get("files")
    if version != expected_version or not VERSION_RE.match(str(version)) or not isinstance(files, dict):
        raise bad
    try:
        needs = int(manifest.get("requires_shell", 1))
    except (TypeError, ValueError):
        raise bad
    if needs > shell_version:
        raise UpdateError("Deze versie vraagt een nieuwe installatie (dmg).", "This version needs a full install (dmg).")
    if not REQUIRED <= set(files) or set(names) != set(files) | {"manifest.json"} or len(names) != len(set(names)):
        raise bad
    contents = {"manifest.json": package.read("manifest.json")}
    for name, digest in files.items():
        if not NAME_RE.match(name) or name in NEVER_IN_UPDATE or name == "manifest.json":
            raise bad
        blob = package.read(name)
        if hashlib.sha256(blob).hexdigest() != digest:
            raise bad
        contents[name] = blob
    return str(version), contents


def install_files(version, contents, code_dir=app_shell.CODE_DIR):
    """Zet de code neer en maakt hem pas als laatste de huidige. Een afgebroken installatie verandert niets."""
    code_dir.mkdir(parents=True, exist_ok=True)
    staging, final = code_dir / f".{version}.new", code_dir / version
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    for name, blob in contents.items():
        (staging / name).write_bytes(blob)
    shutil.rmtree(final, ignore_errors=True)
    os.replace(staging, final)

    previous = (read_json(code_dir / "current.json") or {}).get("version")
    bad = [v for v in sorted(bad_versions(code_dir)) if v != version]   # opnieuw geïnstalleerd: weer een kans
    (code_dir / "bad.json").write_text(json.dumps(bad))
    pointer = code_dir / ".current.tmp"
    pointer.write_text(json.dumps({"version": version, "installed": time.strftime("%Y-%m-%d %H:%M:%S")}))
    os.replace(pointer, code_dir / "current.json")

    keep = {version, previous} if KEEP_VERSIONS > 1 else {version}
    for entry in code_dir.iterdir():
        if entry.is_dir() and VERSION_RE.match(entry.name) and entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)


def install(info, running_version, shell_version=app_shell.SHELL_VERSION, public_key=app_shell.PUBLIC_KEY,
            code_dir=app_shell.CODE_DIR, get=fetch):
    """Downloadt, controleert en zet een code-update neer. Geeft de versie. Herstarten doet de aanroeper."""
    data = get(info["zip_url"], MAX_ZIP)
    signature = get(info["sig_url"], MAX_SIGNATURE).decode("ascii", "replace")
    verify_signature(data, signature, public_key)
    version, contents = read_package(data, info["version"], shell_version)
    if app_shell.version_tuple(version) <= app_shell.version_tuple(running_version):
        raise UpdateError("Deze versie is niet nieuwer dan de draaiende.", "This version is not newer than the running one.")
    install_files(version, contents, code_dir)
    return version
