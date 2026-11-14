"""Vaste laag van Local Transcribe for Iconik: wat een code-update niet kan veranderen.

Dit bestand en main.py zitten alleen in de app-bundel, nooit in een code-update.

SHELL_VERSION gaat omhoog als een update iets nodig heeft wat de bundel niet heeft: een andere
Python, nieuwe pakketten, een nieuwe ffmpeg of een aangepaste main.py of app_shell.py. Zo'n update
is dan een nieuwe dmg, geen code-update.

PUBLIC_KEY controleert de handtekening van updates. De privésleutel staat in Proton Pass
(zie tools/sign_update.py). Een andere sleutel invoeren kan alleen met een nieuwe dmg.

Vanaf shell 5 wordt een code-update bij elke start opnieuw gecontroleerd (load_code) en daarna uit het
geheugen geladen (use_code), niet uit de map. De uitgepakte bestanden in Application Support doen dan
niet meer mee: wie ze aanpast of er iets bij zet, verandert niets aan wat de app draait.
"""
import base64
import hashlib
import importlib.abc
import importlib.util
import io
import json
import re
import sys
import zipfile
from pathlib import Path

SHELL_VERSION = 5
PUBLIC_KEY = "g4XE02D2oSS+XiFvXbzqQdRpF0Mboixkr8iKnKRIczM="  # Ed25519, base64

APP_NAME = "Local Transcribe for Iconik"
SUPPORT = Path.home() / "Library" / "Application Support" / APP_NAME
CODE_DIR = SUPPORT / "code"   # hier staan gedownloade code-updates, naast de bundel en niet erin


PACKAGE, SIGNATURE = "package.zip", "package.sig"   # de zip en handtekening zoals gedownload (updater.py bewaart ze)
MAX_PACKAGE = 50 * 1024 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
NEVER_IN_UPDATE = {"main.py", "app_shell.py", "manifest.json", PACKAGE, SIGNATURE}
CODE = {}   # naam -> inhoud van de code-update die nu draait; leeg als de code uit de bundel draait


def version_tuple(text):
    numbers = [int(n) for n in re.findall(r"\d+", text or "")[:3]]
    return tuple(numbers + [0] * (3 - len(numbers)))


def load_code(folder, version, public_key=PUBLIC_KEY):
    """Leest een geïnstalleerde code-update uit zijn ondertekende zip en controleert hem opnieuw.

    Geeft ({naam: inhoud}, None), of (None, reden) als de update niet te vertrouwen is. Alleen package.zip en
    package.sig in de map tellen: de handtekening over de zip, het manifest in de zip en de hash van elk
    bestand. De los uitgepakte bestanden ernaast worden niet gelezen."""
    try:
        if (folder / PACKAGE).stat().st_size > MAX_PACKAGE:
            return None, "het pakket is te groot"
        data = (folder / PACKAGE).read_bytes()
        signature = (folder / SIGNATURE).read_bytes()
    except OSError:
        return None, "het ondertekende pakket ontbreekt (geïnstalleerd door een oudere versie)"
    try:
        from nacl.signing import VerifyKey
        VerifyKey(base64.b64decode(public_key)).verify(data, base64.b64decode(signature.strip()))
    except Exception as error:
        return None, f"de handtekening klopt niet ({type(error).__name__})"
    try:
        package = zipfile.ZipFile(io.BytesIO(data))
        names = package.namelist()
        raw_manifest = package.read("manifest.json")
        manifest = json.loads(raw_manifest)
        files = manifest["files"]
        if manifest["version"] != version or not isinstance(files, dict):
            return None, "het manifest hoort niet bij deze versie"
        if set(names) != set(files) | {"manifest.json"} or len(names) != len(set(names)):
            return None, "de zip bevat iets anders dan het manifest noemt"
        contents = {"manifest.json": raw_manifest}
        for name, digest in files.items():
            if not NAME_RE.match(name) or name in NEVER_IN_UPDATE:
                return None, f"bestandsnaam niet toegestaan: {name!r:.60}"
            blob = package.read(name)
            if hashlib.sha256(blob).hexdigest() != digest:
                return None, f"{name} wijkt af van het manifest"
            contents[name] = blob
    except Exception as error:   # geen zip, geen manifest, vreemde opbouw
        return None, f"het pakket is niet in orde ({type(error).__name__})"
    return contents, None


class _SignedCode(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Laadt de modules van een code-update uit het geheugen, voor alle andere plekken. Alleen de namen uit
    het ondertekende manifest; al het andere komt zoals altijd uit de bundel."""

    def __init__(self, contents, version, here):
        self.contents, self.version, self.here = contents, version, here

    def find_spec(self, name, path=None, target=None):
        if path is None and f"{name}.py" in self.contents:
            return importlib.util.spec_from_loader(name, self, origin=f"code-{self.version}/{name}.py")
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__name__
        # __file__ wijst naar de bundel, niet naar Application Support: code die een pad naast zichzelf
        # zoekt of op het zoekpad zet, komt zo nooit in een map uit die een ander proces kan vullen.
        module.__file__ = str(self.here / f"{name}.py")
        exec(compile(self.contents[f"{name}.py"], f"code-{self.version}/{name}.py", "exec"), module.__dict__)

    def get_source(self, name):   # voor regels in een foutmelding (linecache)
        return self.contents[f"{name}.py"].decode("utf-8")


def use_code(contents, version):
    """Vanaf nu komen de modules en de pagina's van de app uit deze gecontroleerde code-update."""
    CODE.clear()
    CODE.update(contents)
    sys.meta_path[:] = [finder for finder in sys.meta_path if not isinstance(finder, _SignedCode)]
    sys.meta_path.insert(0, _SignedCode(CODE, version, Path(__file__).resolve().parent))


def read_text(name):
    """Een bestand van de app dat geen module is (ui.html, refine.html): uit de draaiende code-update, anders
    uit de bundel."""
    blob = CODE.get(name)
    if blob is not None:
        return blob.decode("utf-8")
    return (Path(__file__).resolve().parent / name).read_text(encoding="utf-8")
