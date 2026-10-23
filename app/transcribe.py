#!/usr/bin/env python3
"""Transcribeer een Iconik-asset lokaal met Whisper en push het resultaat
als transcript met woord-sync naar Iconik.

Gebruik:
    source ~/.iconik-env
    uv run transcribe.py <asset_id>            # proefrun, schrijft niets naar Iconik
    uv run transcribe.py <asset_id> --push     # schrijft het transcript naar Iconik
    uv run transcribe.py <asset_id> --speakers 2   # aantal sprekers opgeven
    uv run transcribe.py <asset_id> --no-speakers  # sprekerherkenning overslaan
    uv run transcribe.py <asset_id> --push --replace   # eigen eerdere transcript vervangen

Versie 3: sprekerherkenning met pyannote, een regel per zin, en per zin een
controle van de spreker aan de hand van de stem.
"""

import argparse
import base64
import builtins
import hashlib
import json
import mmap
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from importlib.metadata import version as pkg_version
from pathlib import Path

import numpy as np
import requests

# Niets naar buiten behalve Iconik zelf (en eenmalig de modeldownload). pyannote 4 stuurt
# standaard gebruiksgegevens naar otel.pyannote.ai; dat zetten we uit voordat het geladen wordt.
os.environ["PYANNOTE_METRICS_ENABLED"] = "false"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

DEFAULT_HOST = "https://app.iconik.io"
REDIRECT_URI = "http://localhost:8765/oauth/callback"
OAUTH_SCOPE = "assets:read assets:write collections:read user_impersonation"
APP_NAME = "Local Transcribe for Iconik"
APP_VERSION = "0.3.6"  # ook het versienummer van de app-bundel (build_bundle.sh leest het hier)
RELEASES_REPO = "ColorcodedNL/local-transcribe-for-iconik"  # hier kijkt de app of er een nieuwere versie is
OLD_APP_NAME = "Iconik Transcribe"  # de naam tot oktober 2026; voorkeuren en inlog verhuizen mee
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
try:
    _old_dir = SUPPORT_DIR.with_name(OLD_APP_NAME)
    if _old_dir.is_dir() and not SUPPORT_DIR.exists():
        _old_dir.rename(SUPPORT_DIR)
except OSError:
    pass
KEYCHAIN_SERVICE = APP_NAME
BULK_LIMIT = 500
CONFIG_PATH = SUPPORT_DIR / "config.json"
PAGE_SIZE = 100
ENGINE_NAME = "mlx-whisper"  # hieraan herkent --replace de transcripten van dit script
SAMPLE_RATE = 16000

# Spoorkeuze bij bestanden met losse audiosporen (zie analyse_tracks).
TRACK_SILENT_DB = -65.0         # gemiddeld niveau waaronder een spoor als stil telt
TIMECODE_REGULARITY = 0.85      # tijdcode: vrijwel alle pulsen in een blok op twee vaste lengtes
TIMECODE_SHARE = 0.5            # en dat in meer dan de helft van de blokken met signaal
TRACK_COPY_CORRELATION = 0.9    # vanaf deze gelijkenis dragen twee sporen hetzelfde signaal
MIC_ACTIVE_DB = -12.0           # binnen zoveel dB van de luide momenten telt als spraak
MIC_DOMINANT_DB = 6.0           # zoveel luider dan de rest telt als "dit spoor overheerst"
MIC_MIN_DOMINANCE = 0.10        # een eigen microfoon overheerst minstens dit deel van de tijd
MIC_MIN_MARGIN_DB = 3.0         # kleiner verschil in een zin: de vorige spreker aanhouden
DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"
# Lokale kopie van het sprekermodel (CC-BY-4.0, pyannote). Staat die er, dan is
# er geen Hugging Face-account of token nodig.
_BUNDLE_RES = Path(os.environ.get("LOCALTRANSCRIBE_RES") or Path(__file__).resolve().parent.parent)  # ook als de code uit een update komt
_BUNDLED_SPEAKER_MODEL = _BUNDLE_RES / "models" / "speaker-diarization-community-1"
LOCAL_SPEAKER_MODEL = (_BUNDLED_SPEAKER_MODEL if (_BUNDLED_SPEAKER_MODEL / "config.yaml").is_file()
                       else SUPPORT_DIR / "models" / "speaker-diarization-community-1")


LISTENER = None  # de ontvanger zet hier een functie neer om de voortgang te volgen

# Vooruit lezen draait naast een andere opdracht. Die draad zet hier lijsten neer, zodat zijn
# meldingen en logregels bewaard worden tot zijn eigen opdracht begint.
_local = threading.local()


def print(*args, **kwargs):  # noqa: A001 - bewust: logregels van het vooruit lezen bewaren
    lines = getattr(_local, "lines", None)
    if lines is not None and "file" not in kwargs:
        lines.append(kwargs.get("sep", " ").join(str(a) for a in args))
        return
    builtins.print(*args, **kwargs)


_meter = {"peak": 0, "since": None, "thread": None}


def _footprint():
    """Geheugen van dit proces zoals Activiteitenweergave het toont (phys_footprint), in bytes."""
    try:
        import ctypes
        lib = getattr(_footprint, "lib", None)
        if lib is None:
            lib = _footprint.lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        info = (ctypes.c_uint64 * 48)()
        if lib.proc_pid_rusage(os.getpid(), 2, ctypes.byref(info)) != 0:  # RUSAGE_INFO_V2
            return 0
        return int(info[2 + 7])  # na de uuid (16 bytes): ri_phys_footprint
    except Exception:
        return 0


def memory_report(stage):
    """Logregel met geheugen en tijd sinds de vorige meting, ook de piek daartussen."""
    if getattr(_local, "lines", None) is not None:
        return  # vooruit lezen: de metingen horen bij de opdracht die nu loopt
    try:
        now = time.time()
        if _meter["thread"] is None:
            def sample():
                while True:
                    _meter["peak"] = max(_meter["peak"], _footprint())
                    time.sleep(0.25)
            _meter["thread"] = threading.Thread(target=sample, daemon=True)
            _meter["thread"].start()
        current = _footprint()
        peak = max(_meter["peak"], current)
        since = _meter["since"] or now
        parts = [f"nu {current / 1e9:.1f} GB", f"piek {peak / 1e9:.1f} GB", f"{now - since:.1f} s"]
        mx = sys.modules.get("mlx.core")
        if mx is not None:
            parts.append(f"MLX actief {mx.get_active_memory() / 1e9:.1f} / cache {mx.get_cache_memory() / 1e9:.1f} GB")
        torch = sys.modules.get("torch")
        if torch is not None and torch.backends.mps.is_available():
            # driver_allocated telt al het Metal-geheugen van het proces, dus ook Whisper in MLX
            parts.append(f"torch {torch.mps.current_allocated_memory() / 1e9:.1f} / "
                         f"Metal totaal {torch.mps.driver_allocated_memory() / 1e9:.1f} GB")
        print(f"Meting {stage}: " + ", ".join(parts), flush=True)
        _meter["peak"], _meter["since"] = 0, now
    except Exception as error:
        print(f"Meting {stage}: lukte niet ({type(error).__name__}: {error})")


def tell(kind, **data):
    """Meldt voortgang aan wie meeluistert; mag de verwerking nooit storen."""
    events = getattr(_local, "events", None)
    if events is not None:
        events.append((kind, data))
        return
    if LISTENER:
        try:
            LISTENER(kind, **data)
        except Exception:
            pass


def envelope(x, buckets=160):
    """Het verloop van het niveau van een spoor, als waarden van 0 tot 1 (-60 tot 0 dB)."""
    size = max(len(x) // buckets, 1)
    rows = x[: len(x) // size * size].reshape(-1, size)
    if not len(rows):
        return []
    db = 10 * np.log10(np.maximum((rows.astype(np.float64) ** 2).mean(axis=1), 1e-12))
    return [round(float(v), 2) for v in np.clip((db + 60) / 60, 0, 1)]


def track_info(number, x, note, used):
    rms = float(np.sqrt((x.astype(np.float64) ** 2).mean())) if len(x) else 0.0
    return {"n": number, "db": round(20 * np.log10(max(rms, 1e-6)), 1),
            "note": note, "used": used, "env": envelope(x)}


def speaker_model_available(token=None):
    return bool(token) or (LOCAL_SPEAKER_MODEL / "config.yaml").is_file()

# Een regel van Whisper wordt op zinseinden in zinnen geknipt; elke zin krijgt een spreker.
SENTENCE_END = (".", "?", "!", "\u2026")

REPEAT_LIMIT = 4  # dezelfde zin zo vaak achter elkaar geldt als een lus van Whisper

# Stemcontrole per zin (zie refine_speakers).
VOICE_LONG_TURN = 3.0      # beurten vanaf deze lengte bepalen hoe een stem klinkt
VOICE_MAX_CROP = 10.0      # per beurt hooguit zoveel seconden gebruiken
VOICE_MIN_SECONDS = 10.0   # een stem telt mee met minstens zoveel seconden lange beurten
VOICE_MIN_SENTENCE = 0.3   # kortere zinnen niet beoordelen
VOICE_MIN_MARGIN = 0.10    # onder deze marge blijft de keuze van het sprekermodel staan

# Korte clips zonder lange beurten: zinnen onderling vergelijken (zie cluster_sentences).
CLUSTER_MIN_SENTENCE = 1.0  # alleen zinnen vanaf deze lengte vormen de groepen
CLUSTER_DISTANCE = 0.75     # groepen samenvoegen tot de gemiddelde afstand hierboven komt

# Bestanden op een asset die geen media zijn.
SIDECAR_EXTENSIONS = {".srt", ".vtt", ".json", ".xml", ".txt", ".csv", ".edl"}

# Verzonnen tekst begint met een eerste woord waar Whisper nauwelijks in gelooft,
# op een plek waar het sprekermodel weinig of geen spraak hoort. Beide moeten gelden.
FIRST_WORD_MIN_PROBABILITY = 0.05
MIN_SPEECH_SHARE = 0.5

# Gemiddelde woordzekerheid waaronder een heel asset als onverstaanbaar geldt.
# Gemeten: geroezemoes via cameramicrofoon 0.47, heldere spraak 0.96.
MIN_CONFIDENCE = 0.65

# Bekende verzinsels van Whisper bij stilte of muziek. Vergelijking is hoofdletterongevoelig.
# Per taal, want een Nederlands patroon mag geen Engelse zin wegfilteren.
HALLUCINATION_PATTERNS = {
    "nl": [
        r"tv gelderland",
        r"ondertiteld door",
        r"ondertiteling[: ]",
        r"ondertitels[: ]",
        r"^\W*intromuziek\W*$",
        r"^\W*muziek\W*$",
        r"^\W*applaus\W*$",
        r"bedankt voor het kijken",
    ],
    "en": [
        r"thanks? (you )?for watching",
        r"subtitles by",
        r"amara\.org",
        r"^\W*music\W*$",
        r"^\W*applause\W*$",
    ],
}
LANGUAGE_WINDOWS = 5  # aantal stukken van 30 seconden waarop de taal wordt bepaald


class NeedsFolder(Exception):
    """Het mount point is onbekend en er mag nu geen venster komen."""


class Problem(Exception):
    """Een fout die we de gebruiker in gewone woorden kunnen melden."""


def fail(message, skip=False, local=False):
    """`skip`: het asset is overgeslagen om een gewone reden, er is niets misgegaan.
    `local`: het ligt aan deze Mac (opslag niet gemount, geen inlog, geen internet) en niet aan
    het asset. Een werker zet zo'n asset dan terug in de rij voor een andere Mac."""
    problem = Problem(message)
    problem.skip = skip
    problem.local = local
    raise problem


_system_language = []


def app_language():
    """De taal van de app: de keuze uit de voorkeuren, anders die van macOS."""
    try:
        choice = load_config().get("app_language", "auto")
    except Exception:
        choice = "auto"
    if choice in ("nl", "en"):
        return choice
    if not _system_language:
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                                 capture_output=True, text=True, timeout=5).stdout
            first = re.search(r"[A-Za-z]{2}", out)
            _system_language.append("nl" if first and first.group().lower() == "nl" else "en")
        except Exception:
            _system_language.append("en")
    return _system_language[0]


def L(dutch, english):
    """Een melding voor de gebruiker, in de taal van de app."""
    return dutch if app_language() == "nl" else english


LOGIN_LOCK = threading.Lock()


def api_host():
    return (load_config().get("host") or DEFAULT_HOST).rstrip("/")


def load_login():
    """De bewaarde inlog uit de sleutelhanger van macOS, of None."""
    import keyring
    raw = keyring.get_password(KEYCHAIN_SERVICE, "login")
    if not raw:
        # Inlog van voor de naamswijziging: overzetten naar de nieuwe naam.
        raw = keyring.get_password(OLD_APP_NAME, "login")
        if raw:
            keyring.set_password(KEYCHAIN_SERVICE, "login", raw)
            try:
                keyring.delete_password(OLD_APP_NAME, "login")
            except Exception:
                pass
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def save_login(login):
    import keyring
    keyring.set_password(KEYCHAIN_SERVICE, "login", json.dumps(login))
    _has_login[:] = [True, time.monotonic()]


def clear_login():
    import keyring
    _has_login[:] = [False, time.monotonic()]
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, "login")
    except Exception:
        pass


# Is er een inlog? Het venster vraagt dat elke seconde; de sleutelhanger hoeft dan niet elke keer.
# Alleen voor die vraag: het token zelf komt altijd vers uit de sleutelhanger (zie Iconik._authorize),
# want een ververstoken mag maar een keer gebruikt worden.
HAS_LOGIN_SECONDS = 5
_has_login = [None, 0.0]   # [uitkomst, wanneer gekeken]


def has_login():
    known, checked = _has_login
    if known is not None and time.monotonic() - checked < HAS_LOGIN_SECONDS:
        return known
    found = bool(load_login())
    _has_login[:] = [found, time.monotonic()]
    return found


def token_request(fields):
    """Vraagt bij Iconik een token op en bewaart het meteen. Geeft de inlog terug."""
    response = requests.post(f"{api_host()}/API/auth/v1/oauth/token/", data=fields, timeout=30)
    if not response.ok:
        fail(L(f"Iconik gaf geen token: HTTP {response.status_code} {response.text[:200]}",
               f"Iconik did not return a token: HTTP {response.status_code} {response.text[:200]}"))
    payload = response.json()
    login = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "expires_at": time.time() + float(payload.get("expires_in", 3600)),
        "scope": payload.get("scope", ""),
    }
    # Een ververstoken is eenmalig; het nieuwe moet bewaard zijn voor er iets anders gebeurt.
    save_login(login)
    return login


def start_login():
    """Begin van de inlog: geeft (adres voor de browser, state, verifier)."""
    client_id = load_config().get("client_id")
    if not client_id:
        fail(L("Er is nog geen client-ID van de Iconik-applicatie ingesteld.",
               "No client ID for the Iconik application has been set yet."))
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    from urllib.parse import urlencode
    query = urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT_URI, "scope": OAUTH_SCOPE,
        "response_type": "code", "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state,
    })
    return f"{api_host()}/oauth/authorize?{query}", state, verifier


def finish_login(code, verifier):
    return token_request({
        "grant_type": "authorization_code", "client_id": load_config().get("client_id"),
        "code": code, "code_verifier": verifier, "redirect_uri": REDIRECT_URI,
    })


class Iconik:
    """Praat met de API van Iconik.

    Zonder argumenten: met de inlog van de gebruiker (wordt vanzelf ververst).
    Met app_id en token: met een persoonlijk token, zoals in ~/.iconik-env.
    """

    def __init__(self, app_id=None, token=None):
        self.session = requests.Session()
        self.uses_login = app_id is None
        if not self.uses_login:
            self.session.headers.update({"App-ID": app_id, "Auth-Token": token})

    def _authorize(self, rejected=None):
        """Zet een geldig token klaar. `rejected` is een token dat Iconik net weigerde."""
        # Een slot voor het hele programma: een ververstoken mag maar een keer gebruikt
        # worden, en hergebruik maakt de hele inlog ongeldig.
        with LOGIN_LOCK:
            login = load_login()
            if not login:
                fail(L("Niet ingelogd bij Iconik.", "Not logged in to Iconik."), local=True)
            # Is het geweigerde token intussen al door een ander vervangen, dan niet opnieuw verversen.
            stale = rejected is not None and login["access_token"] == rejected
            if stale or login["expires_at"] - time.time() < 120:
                if not login.get("refresh_token"):
                    clear_login()
                    fail(L("De inlog bij Iconik is verlopen. Log opnieuw in.", "The Iconik login has expired. Log in again."), local=True)
                try:
                    login = token_request({
                        "grant_type": "refresh_token",
                        "client_id": load_config().get("client_id"),
                        "refresh_token": login["refresh_token"],
                    })
                except Problem:
                    clear_login()
                    fail(L("De inlog bij Iconik is verlopen. Log opnieuw in.", "The Iconik login has expired. Log in again."), local=True)
            self.session.headers["Authorization"] = f"Bearer {login['access_token']}"

    def _call(self, method, path, **kwargs):
        if self.uses_login:
            self._authorize()
        url = f"{api_host()}/API/{path}"
        response = self.session.request(method, url, timeout=60, **kwargs)
        if response.status_code == 401 and self.uses_login:
            used = self.session.headers.get("Authorization", "").removeprefix("Bearer ")
            self._authorize(rejected=used)
            response = self.session.request(method, url, timeout=60, **kwargs)
        if not response.ok:
            fail(L(f"{method} {path} gaf HTTP {response.status_code}: {response.text[:300]}",
                   f"{method} {path} returned HTTP {response.status_code}: {response.text[:300]}"))
        try:
            return response.json() if response.content else {}
        except ValueError:
            return {}

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body):
        return self._call("POST", path, json=body)

    def delete(self, path, body=None):
        return self._call("DELETE", path, json=body)


def make_client():
    """De inlog van de gebruiker als die er is, anders het token uit de omgeving, anders None."""
    try:
        if has_login():
            return Iconik()
    except Exception:
        pass  # sleutelhanger niet beschikbaar: dan alleen het token uit de omgeving
    app_id, token = os.environ.get("ICONIK_APP_ID"), os.environ.get("ICONIK_TOKEN")
    return Iconik(app_id, token) if app_id and token else None


def delete_transcription(iconik, asset_id, version_id, transcription_id):
    """Verwijdert een transcript: eerst de segmenten op ID, dan het transcript zelf."""
    segment_ids, page = [], 1
    while True:
        objects = iconik.get(
            f"assets/v1/assets/{asset_id}/segments/?segment_type=TRANSCRIPTION"
            f"&transcription_id={transcription_id}&per_page={PAGE_SIZE}&page={page}"
        ).get("objects", [])
        # Zelf nog eens filteren, zodat er nooit segmenten van een ander transcript meegaan.
        segment_ids += [o["id"] for o in objects if o.get("transcription_id") == transcription_id]
        if len(objects) < PAGE_SIZE:
            break
        page += 1
    for offset in range(0, len(segment_ids), BULK_LIMIT):
        iconik.delete(
            f"assets/v1/assets/{asset_id}/segments/bulk/",
            {"segment_ids": segment_ids[offset:offset + BULK_LIMIT]},
        )
    iconik.delete(
        f"assets/v1/assets/{asset_id}/versions/{version_id}"
        f"/transcriptions/{transcription_id}/properties/"
    )
    return len(segment_ids)


def find_version(asset):
    versions = [v for v in asset.get("versions", []) if v.get("status") == "ACTIVE"]
    if not versions:
        fail(L("Geen actieve versie gevonden op dit asset.", "No active version found on this asset."))
    return versions[-1]["id"]


CONFIG_LOCK = threading.RLock()


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_config(config):
    """Schrijft via een tijdelijk bestand: wie tegelijk leest, ziet de oude of de nieuwe versie, nooit een halve."""
    with CONFIG_LOCK:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        temp.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        os.replace(temp, CONFIG_PATH)


def update_config(change):
    """Leest de voorkeuren, past ze aan met `change(config)` en schrijft ze weg, in één keer.
    Zo gaat er geen wijziging verloren die een andere draad intussen deed."""
    with CONFIG_LOCK:
        config = load_config()
        change(config)
        save_config(config)
        return config


def set_config(key, value):
    update_config(lambda config: config.__setitem__(key, value))


def set_mount(storage_id, folder):
    """Onthoudt (of vergeet, met None) waar een storage op deze Mac staat."""
    def change(config):
        mounts = config.setdefault("mounts", {})
        if folder is None:
            mounts.pop(storage_id, None)
        else:
            mounts[storage_id] = str(folder)
    update_config(change)


def ask_folder(prompt, start=None):
    """Laat de gebruiker een map kiezen in een venster van macOS. None bij annuleren.
    `start` is de map waarin het venster opent, als die bestaat."""
    if start and Path(start).is_dir():
        choose = "choose folder with prompt (item 1 of argv) default location (POSIX file (item 2 of argv))"
    else:
        choose, start = "choose folder with prompt (item 1 of argv)", ""
    script = f"on run argv\ntell me to activate\nreturn POSIX path of ({choose})\nend run"
    result = subprocess.run(["osascript", "-e", script, prompt, str(start)],
                            capture_output=True, text=True)
    chosen = result.stdout.strip()
    return chosen.rstrip("/") if result.returncode == 0 and chosen else None


def find_moved_mount(known, relative, volumes=Path("/Volumes")):
    """Zoekt dezelfde map op een opnieuw gekoppeld volume.

    Na een verbroken netwerkkoppeling blijft er soms een lege /Volumes/Opslag
    achter en koppelt macOS de opslag opnieuw als /Volumes/Opslag-1. Hier
    zoeken we in beide richtingen, met of zonder zo'n achtervoegsel.
    """
    try:
        inner = Path(known).relative_to(volumes)
    except ValueError:
        return None  # geen netwerkvolume onder /Volumes
    if not inner.parts:
        return None
    volume, rest = inner.parts[0], Path(*inner.parts[1:])
    base = re.sub(r"-\d+$", "", volume)
    try:
        entries = sorted(volumes.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.name != volume and re.sub(r"-\d+$", "", entry.name) == base:
            candidate = entry / rest
            try:
                if (candidate / relative).is_file():
                    return candidate
            except OSError:
                continue
    return None


def find_media_file(iconik, asset_id, mount_override=None, ask_mount=False, interactive=True):
    """Zoekt het mediabestand van een asset op deze Mac.

    Waar een storage lokaal staat wordt per storage onthouden. Is dat nog niet
    bekend en klopt het pad uit Iconik hier niet, dan vraagt een venster erom.
    """
    files = iconik.get(f"files/v1/assets/{asset_id}/files/").get("objects", [])
    media = [
        f for f in files
        if f.get("type") == "FILE"
        and Path(f.get("name", "")).suffix.lower() not in SIDECAR_EXTENSIONS
    ]
    if not media:
        fail(L("Geen mediabestand gevonden op dit asset.", "No media file found on this asset."))
    if len(media) > 1:
        print(f"Let op: {len(media)} mediabestanden gevonden, het eerste wordt gebruikt.")
    chosen = media[0]
    relative = Path(chosen.get("directory_path", "")) / chosen["name"]

    if mount_override:
        path = Path(mount_override) / relative
        if not path.is_file():
            fail(L(f"Bestand niet gevonden: {path}", f"File not found: {path}"))
        return path

    storage_id = chosen["storage_id"]
    known = (load_config().get("mounts") or {}).get(storage_id)

    if known and not ask_mount:
        path = Path(known) / relative
        if path.is_file():
            return path
        moved = find_moved_mount(known, relative)
        if moved is not None:
            set_mount(storage_id, moved)
            print(f"Opslag staat nu op {moved} (was {known}), dat is onthouden.")
            return moved / relative
        if not Path(known).is_dir():
            fail(L(f"De map {known} is niet bereikbaar. Is de opslag (ISG) gemount?",
                   f"The folder {known} cannot be reached. Is the storage (ISG) mounted?"), local=True)
        fail(L(f"Bestand niet gevonden: {path}", f"File not found: {path}"))

    try:
        storage = iconik.get(f"files/v1/storages/{storage_id}/")
    except Problem:
        storage = {}  # geen recht om de storage te lezen: dan vragen we de map gewoon
    name = storage.get("name") or storage_id
    remote = storage.get("settings", {}).get("mount_point")

    def remember(mount):
        # Pas nu lezen en schrijven: het venster kan lang open hebben gestaan.
        set_mount(storage_id, mount)
        print(f"Onthouden: storage {name} staat hier op {mount}")

    # Staat de storage hier op hetzelfde pad als in Iconik, dan hoeft niemand iets te kiezen.
    if remote and not ask_mount and (Path(remote) / relative).is_file():
        remember(remote)
        return Path(remote) / relative

    # De naam van de storage in Iconik zegt weinig over de map op schijf. Wat de
    # gebruiker wel herkent is de eerste map binnen de storage.
    top = relative.parts[0] if len(relative.parts) > 1 else relative.name
    if not remote:
        hint = ""
    elif Path(remote).is_dir():
        hint = L(f" Volgens Iconik is dat {remote}; bevestig die map of kies een andere.",
                 f" According to Iconik that is {remote}; confirm that folder or choose another.")
    else:
        hint = L(f" Volgens Iconik is dat {remote}, maar die map bestaat niet op deze Mac.",
                 f" According to Iconik that is {remote}, but that folder does not exist on this Mac.")
    prompt = L(f"ISG \u201c{name}\u201d: kies het mount point, de map waarin \u201c{top}\u201d staat.{hint}",
               f"ISG \u201c{name}\u201d: choose the mount point, the folder that contains \u201c{top}\u201d.{hint}")
    if not interactive:
        raise NeedsFolder(name)
    for _ in range(3):
        folder = ask_folder(prompt, remote)
        if folder is None:
            fail(L(f"Geen mount point gekozen voor ISG {name}.", f"No mount point chosen for ISG {name}."), local=True)
        # Ook goed: een map erboven of eronder gekozen.
        chosen_folder = Path(folder)
        candidates = [chosen_folder, chosen_folder.parent, chosen_folder.parent.parent]
        if remote:
            candidates.insert(1, chosen_folder / Path(remote).name)
        for candidate in candidates:
            if (candidate / relative).is_file():
                remember(candidate)
                return candidate / relative
        prompt = L(f"Daar is het niet gevonden. Kies de map waarin \u201c{top}\u201d staat (ISG \u201c{name}\u201d).{hint}",
                   f"Not found there. Choose the folder that contains \u201c{top}\u201d (ISG \u201c{name}\u201d).{hint}")
    fail(L(f"Het bestand is niet gevonden in de gekozen mappen voor ISG {name}.",
           f"The file was not found in the chosen folders for ISG {name}."))


def list_tracks(path):
    """Alle audiosporen als (stream, kanaal, aantal kanalen in die stream)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index,channels", "-of", "json", str(path)],
        capture_output=True, text=True).stdout
    tracks = []
    for audio_index, stream in enumerate(json.loads(out or "{}").get("streams", [])):
        channels = int(stream.get("channels") or 1)
        tracks += [(audio_index, channel, channels) for channel in range(channels)]
    return tracks


def media_seconds(path):
    """Duur van het bestand volgens ffprobe, of None."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    try:
        return float(json.loads(out or "{}").get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None


PCM_BLOCK = 64 * 1024 * 1024  # bytes per extra blok als de duur onbekend of te krap is


def pcm_block(size):
    """Een int16-tabel van `size` bytes in eigen geheugen (mmap). macOS geeft gewoon vrijgegeven
    grote blokken niet terug aan het systeem maar houdt ze vast voor hergebruik in het proces; dan
    bleef de ruwe audio van een lang bestand na afloop als bezet geheugen staan. Een mmap gaat bij
    het vrijgeven van de laatste verwijzing gegarandeerd terug."""
    return np.frombuffer(mmap.mmap(-1, max(size, 2)), np.int16)


def read_pcm(command, channels, seconds=None):
    """Draait ffmpeg met s16le op stdout en leest dat in blokken direct in een int16-tabel.

    Met subprocess.run(capture_output=True) stond de hele uitvoer eerst als bytes in het geheugen
    en daarna nog eens als een samengevoegde kopie: bij 12 sporen van anderhalf uur ruim 4 GB.
    Met de duur vooraf is er één tabel van de juiste grootte. Geeft (samples, returncode, stderr)."""
    frame = 2 * channels
    size = int((seconds + 5) * SAMPLE_RATE) * frame if seconds else PCM_BLOCK // frame * frame
    blocks, current, used = [], pcm_block(size), 0
    with tempfile.TemporaryFile() as errors:   # een bestand: een volle stderr-pijp kan ffmpeg niet laten wachten
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        try:
            view = memoryview(current).cast("B")
            while True:
                if used == len(view):
                    blocks.append(current)
                    current, used = pcm_block(PCM_BLOCK), 0
                    view = memoryview(current).cast("B")
                read = process.stdout.readinto(view[used:])
                if not read:
                    break
                used += read
            view.release()
        finally:
            process.stdout.close()
            code = process.wait()
        errors.seek(0)
        message = errors.read().decode(errors="replace")
    current = current[: used // 2]
    data = np.concatenate(blocks + [current]) if blocks else current
    return data[: len(data) // channels * channels], code, message


def read_track(path, track=None, seconds=None):
    """Leest een spoor (of, zonder spoor, de gewone mix) als mono 16 kHz."""
    command = ["ffmpeg", "-nostdin", "-v", "error"]
    if seconds:
        command += ["-t", str(seconds)]
    command += ["-i", str(path), "-vn"]
    if track is not None:
        audio_index, channel, channels = track
        command += ["-map", f"0:a:{audio_index}"]
        if channels > 1:
            command += ["-af", f"pan=mono|c0=c{channel}"]
    command += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    data, code, errors = read_pcm(command, 1, seconds or media_seconds(path))
    if code != 0 and not len(data):
        fail(L("ffmpeg kon geen audio lezen: ", "ffmpeg could not read audio: ") + errors[:300])
    return data.astype(np.float32) / 32768.0


def measure_track(x):
    """Kengetallen van een spoor, of None als er te weinig audio is."""
    if len(x) < SAMPLE_RATE // 2:
        return None
    frame = SAMPLE_RATE * 30 // 1000
    frames = x[: len(x) // frame * frame].reshape(-1, frame)
    levels = np.sqrt((frames ** 2).mean(axis=1))
    rms, peak = float(np.sqrt((x ** 2).mean())), float(np.abs(x).max())
    active = levels > 10 ** (-50 / 20)
    loud = 20 * np.log10(np.maximum(levels[active], 1e-9)) if active.any() else np.array([0.0])
    # Tijdcode (LTC) is een vast bitpatroon met maar twee pulslengtes; spraak en muziek
    # gaan grillig door nul. Per blok van 100 ms kijken, zodat ook een spoor waar de
    # tijdcode maar een deel van de tijd op staat herkend wordt.
    block = SAMPLE_RATE // 10
    blocks = x[: len(x) // block * block].reshape(-1, block)
    coded_candidates = np.flatnonzero(np.sqrt((blocks ** 2).mean(axis=1)) > 10 ** (-50 / 20))
    if len(coded_candidates) > 400:
        coded_candidates = coded_candidates[np.linspace(0, len(coded_candidates) - 1, 400).astype(int)]
    coded = 0
    for index in coded_candidates:
        gaps = np.diff(np.flatnonzero(np.diff(np.signbit(blocks[index]))))
        if len(gaps) > 20 and np.sort(np.bincount(gaps))[::-1][:2].sum() / len(gaps) > TIMECODE_REGULARITY:
            coded += 1
    regular = coded / len(coded_candidates) if len(coded_candidates) else 0.0
    return {
        "regelmaat": regular,                    # deel van de luide blokken met een strak pulspatroon
        "rms": 20 * np.log10(max(rms, 1e-9)),
        "piek": 20 * np.log10(max(peak, 1e-9)),
        "actief": float(active.mean()),          # deel van de tijd met geluid
        "crest": peak / max(rms, 1e-9),          # piek ten opzichte van gemiddelde
        "spreiding": float(loud.std()),          # hoe sterk het niveau wisselt (dB)
    }


def classify_track(d):
    if d is None or d["rms"] < TRACK_SILENT_DB:
        return "stil"
    if d["regelmaat"] > TIMECODE_SHARE:
        return "tijdcode of toon"
    if d["actief"] > 0.97 and d["spreiding"] < 2.0 and d["crest"] < 3.0:
        return "constant signaal"  # bijvoorbeeld een testtoon
    if d["actief"] < 0.03:
        return "vrijwel stil"
    return "geluid"


def read_all_tracks(path, tracks):
    """Leest alle sporen in een keer: een tabel (samples, sporen) in 16 kHz."""
    streams = sorted({track[0] for track in tracks})
    command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn"]
    if len(streams) == 1:
        command += ["-map", f"0:a:{streams[0]}"]
    else:
        inputs = "".join(f"[0:a:{index}]" for index in streams)
        command += ["-filter_complex", f"{inputs}amerge=inputs={len(streams)}[all]", "-map", "[all]"]
    command += ["-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    data, code, errors = read_pcm(command, len(tracks), media_seconds(path))
    if code != 0 and not len(data):
        fail(L("ffmpeg kon de sporen niet lezen: ", "ffmpeg could not read the tracks: ") + errors[:300])
    return data[: len(data) // len(tracks) * len(tracks)].reshape(-1, len(tracks))


SIMILARITY_STEP = 16  # 1 kHz uit 16 kHz


def similarity(a, b, step=SIMILARITY_STEP):
    """Hoe sterk twee sporen op elkaar lijken (absolute correlatie, 0 tot 1), op elk 16e sample (1 kHz).
    Voor "is dit dezelfde microfoon" is dat genoeg, en het kost een paar MB in plaats van bijna 2 GB
    per paar per uur audio met np.corrcoef op de volle sporen."""
    x = a[::step].astype(np.float64)
    y = b[::step].astype(np.float64)
    x -= x.mean()
    y -= y.mean()
    scale = float(np.sqrt((x @ x) * (y @ y)))
    return abs(float(x @ y)) / scale if scale > 0 else 0.0


def analyse_tracks(data, mix_same_source=False):
    """Bepaalt welke sporen gebruikt worden. Geeft (samples, microfoons) terug.

    `microfoons` is None, of bij twee of meer eigen microfoons een tabel met per
    100 ms het niveau van elke microfoon, om per zin de spreker uit af te lezen.
    Met `mix_same_source` (stereo) worden kanalen met dezelfde bron gemixt in
    plaats van dat er een gekozen wordt.
    """
    count = data.shape[1]

    def floats(i):
        # Een spoor als float32, pas op het moment dat het nodig is. Alle sporen tegelijk
        # omzetten kostte bij 12 sporen van anderhalf uur ruim 4 GB, ook voor stille sporen.
        return data[:, i].astype(np.float32) / 32768.0

    # Eén spoor tegelijk: meten, en van de bruikbare alleen bewaren wat later nodig is
    # (een uitgedunde kopie voor de vergelijking en het niveau per 100 ms).
    frame = SAMPLE_RATE // 10
    frames = len(data) // frame
    measured, kinds, infos, thin, power_of = [], [], [], {}, {}
    for i in range(count):
        x = floats(i)
        measured.append(measure_track(x))
        kinds.append(classify_track(measured[-1]))
        infos.append(track_info(i + 1, x, "", False))
        if kinds[-1] == "geluid":
            thin[i] = x[::SIMILARITY_STEP].copy()
            power_of[i] = (x[: frames * frame].reshape(frames, frame) ** 2).mean(axis=1)
        del x
    usable = [i for i in range(count) if kinds[i] == "geluid"]

    # Sporen die (vrijwel) hetzelfde signaal dragen horen bij elkaar: een microfoon
    # die dubbel is opgenomen. Per groep blijft het beste spoor over.
    group = {i: i for i in usable}
    for a in usable:
        for b in usable:
            if a < b and similarity(thin[a], thin[b], step=1) > TRACK_COPY_CORRELATION:
                root_a, root_b = group[a], group[b]
                for key in group:
                    if group[key] == root_b:
                        group[key] = root_a
    clipped = {i: int((np.abs(data[:, i].astype(np.int32)) >= 32700).sum()) for i in usable}
    chosen = {}
    for i in usable:
        best = chosen.get(group[i])
        if best is None or (clipped[i], -measured[i]["rms"]) < (clipped[best], -measured[best]["rms"]):
            chosen[group[i]] = i
    mics = sorted(chosen.values())

    notes = {i: kinds[i] for i in range(count)}
    for i in usable:
        if i not in mics:
            notes[i] = f"zelfde signaal als spoor {chosen[group[i]] + 1}"
    tell("tracks", tracks=[{**infos[i], "note": notes[i], "used": i in mics} for i in range(count)])
    if not mics:
        for i in range(count):
            print(f"  spoor {i + 1}: {notes[i]}")
        fail(L("Geen spoor met geluid gevonden in dit bestand.", "No track with audio found in this file."), skip=True)

    # Niveau per 100 ms, per spoor gelijkgetrokken op zijn eigen luide momenten.
    power = np.array([power_of[i] for i in mics]) if frames else np.zeros((len(mics), 0))
    level = 10 * np.log10(np.maximum(power, 1e-12))
    reference = np.array([np.percentile(row, 90) if len(row) else 0.0 for row in level])
    relative = level - reference[:, None]

    dominant = np.zeros(len(mics))
    if len(mics) > 1 and frames:
        # Alleen momenten waarop ergens echt gesproken wordt, niet de pauzes met ruis.
        active = relative.max(axis=0) > MIC_ACTIVE_DB
        for k in range(len(mics)):
            others = np.delete(relative, k, axis=0).max(axis=0)
            dominant[k] = float(((relative[k] - others) > MIC_DOMINANT_DB)[active].mean()) if active.any() else 0.0
    own = [k for k in range(len(mics)) if dominant[k] >= MIC_MIN_DOMINANCE]

    for i in range(count):
        extra = ""
        if i in mics and len(mics) > 1 and dominant[mics.index(i)] >= 0.005:   # 0% zegt niets, en leest als een tegenspraak
            extra = f"  overheerst {dominant[mics.index(i)]:4.0%} van de tijd"
        level_text = f"{measured[i]['rms']:6.1f} dB" if measured[i] else "     leeg"
        print(f"  spoor {i + 1}: {level_text}  {notes[i]}{extra}")

    if len(own) >= 2:
        # Elk van deze sporen is geregeld duidelijk het luidst: eigen microfoons.
        gains = 10 ** ((reference[own].mean() - reference[own]) / 20)
        mix = sum(floats(mics[k]) * gain for k, gain in zip(own, gains))
        peak = float(np.abs(mix).max()) if len(mix) else 0.0
        if peak > 0.99:
            mix = mix * (0.99 / peak)
        print("  gebruikt: sporen " + " + ".join(str(mics[k] + 1) for k in own)
              + " samen; de spreker volgt uit het luidste spoor per zin")
        return mix.astype(np.float32), {"relative": relative[own], "tracks": [mics[k] + 1 for k in own]}

    if len(own) < 2 and mix_same_source and len(mics) > 1:
        print("  gebruikt: sporen " + " + ".join(str(i + 1) for i in mics) + " gemixt")
        return (sum(floats(i) for i in mics) / len(mics)).astype(np.float32), None
    if own:
        best = mics[own[0]]
    else:
        # Geen spoor overheerst: dezelfde bron op verschillende microfoons. Een microfoon
        # dicht op de spreker heeft het grootste verschil tussen spraak en pauzes.
        contrast = [float(np.percentile(row, 90) - np.percentile(row, 10)) if len(row) else 0.0
                    for row in level]
        best = mics[int(np.argmax(contrast))]
        if len(mics) > 1:
            print("  Meerdere sporen met dezelfde bron; gekozen is het spoor met de helderste spraak.")
    print(f"  gebruikt: spoor {best + 1}")
    return floats(best), None


def speaker_from_tracks(words, microphones, previous):
    """De microfoon die tijdens deze zin het luidst is. Bij twijfel de vorige spreker."""
    relative = microphones["relative"]
    first = int(words[0]["start"] * 10)
    last = max(int(np.ceil(words[-1]["end"] * 10)), first + 1)
    window = relative[:, first:last]
    if window.shape[1] == 0:
        return previous if previous is not None else f"spoor-{microphones['tracks'][0]}"
    mean = 10 * np.log10(np.maximum((10 ** (window / 10)).mean(axis=1), 1e-12))
    order = np.argsort(mean)[::-1]
    if previous is not None and mean[order[0]] - mean[order[1]] < MIC_MIN_MARGIN_DB:
        return previous
    return f"spoor-{microphones['tracks'][order[0]]}"


def load_audio_tracks(path, track_number=None):
    """Audio als mono 16 kHz plus, bij eigen microfoons per spreker, de niveaus per spoor.

    Bij meer dan een spoor of kanaal wordt gekeken wat er op elk staat: stilte en
    tijdcode vallen af, eigen microfoons worden samengevoegd, een gewone
    stereomix wordt gemixt.
    """
    tracks = list_tracks(path)
    if not tracks:
        fail(L("Dit bestand heeft geen audio.", "This file has no audio."), skip=True)
    if track_number is not None:
        if not 1 <= track_number <= len(tracks):
            fail(L(f"Spoor {track_number} bestaat niet; dit bestand heeft {len(tracks)} sporen.",
                   f"Track {track_number} does not exist; this file has {len(tracks)} tracks."))
        print(f"  gebruikt: spoor {track_number}")
        samples = read_track(path, tracks[track_number - 1])
        tell("tracks", tracks=[track_info(track_number, samples, "gekozen spoor", True)])
        return samples, None
    if len(tracks) == 1:
        samples = read_track(path)
        tell("tracks", tracks=[track_info(1, samples, "geluid", True)])
        return samples, None
    print(f"Sporen:  {len(tracks)}")
    # Stereo is meestal een gewone mix, maar er kan ook tijdcode of een eigen
    # microfoon per kanaal op staan. Daarom altijd eerst kijken wat erop staat.
    stereo = len({t[0] for t in tracks}) == 1 and len(tracks) == 2
    return analyse_tracks(read_all_tracks(path, tracks), mix_same_source=stereo)


def load_audio(path, track_number=None):
    return load_audio_tracks(path, track_number)[0]


_pipeline = None  # het sprekermodel blijft geladen tussen opdrachten


def release_models():
    """Geeft het geheugen van de modellen vrij. Een volgende opdracht laadt ze opnieuw."""
    global _pipeline
    _pipeline = None
    holder = getattr(sys.modules.get("mlx_whisper.transcribe"), "ModelHolder", None)
    if holder is not None:
        holder.model = None
        holder.model_path = None
    import gc
    gc.collect()
    try:
        import mlx.core as mx
        mx.clear_cache()
    except Exception:
        pass
    empty_gpu_cache()


def diarize(samples, token, num_speakers):
    """Geeft een lijst (start, eind, spreker) in seconden."""
    global _pipeline
    import torch
    from pyannote.audio import Pipeline

    if _pipeline is None:
        if (LOCAL_SPEAKER_MODEL / "config.yaml").is_file():
            print("Sprekermodel: lokale kopie")
            _pipeline = Pipeline.from_pretrained(str(LOCAL_SPEAKER_MODEL))
        else:
            print("Sprekermodel: via Hugging Face")
            _pipeline = Pipeline.from_pretrained(DIARIZATION_MODEL, token=token)
    pipeline = _pipeline
    audio = {"waveform": torch.from_numpy(samples).unsqueeze(0), "sample_rate": SAMPLE_RATE}
    options = {"num_speakers": num_speakers} if num_speakers else {}
    try:
        pipeline.to(torch.device("mps"))
        output = pipeline(audio, **options)
    except Exception as error:  # MPS ondersteunt niet elke bewerking
        print(f"GPU lukte niet ({type(error).__name__}), opnieuw op CPU.")
        pipeline.to(torch.device("cpu"))
        output = pipeline(audio, **options)

    diarization = getattr(output, "exclusive_speaker_diarization", None)
    if diarization is None:
        diarization = getattr(output, "speaker_diarization", output)
    return [(turn.start, turn.end, speaker) for turn, speaker in diarization]


def speaker_at(start, end, turns):
    """De spreker met de meeste overlap, anders de dichtstbijzijnde beurt."""
    best, best_overlap = None, 0.0
    for turn_start, turn_end, speaker in turns:
        overlap = min(end, turn_end) - max(start, turn_start)
        if overlap > best_overlap:
            best, best_overlap = speaker, overlap
    if best is not None:
        return best
    middle = (start + end) / 2
    return min(turns, key=lambda t: min(abs(t[0] - middle), abs(t[1] - middle)))[2]


def split_sentences(words):
    """Knipt de woorden van een Whisper-regel in zinnen."""
    sentences, current = [], []
    for index, word in enumerate(words):
        current.append(word)
        following = words[index + 1]["word"] if index + 1 < len(words) else " "
        # Alleen knippen als het volgende stuk een nieuw woord is ("ad" + ".nl" blijft heel).
        if word["word"].strip().endswith(SENTENCE_END) and following.startswith(" "):
            sentences.append(current)
            current = []
    if current:
        sentences.append(current)
    return sentences


def drop_repeats(pieces):
    """Haalt herhalingslussen weg: dezelfde zin vier keer of vaker achter elkaar.

    Whisper blijft soms in een zin hangen, vooral bij muziek of een filmpje dat
    op de achtergrond speelt. Van zo'n reeks blijft alleen de eerste staan.
    """
    def text(piece):
        return "".join(word["word"] for word in piece[1]).strip().lower()

    result, removed, index = [], 0, 0
    while index < len(pieces):
        end = index
        while end + 1 < len(pieces) and text(pieces[end + 1]) == text(pieces[index]):
            end += 1
        run = end - index + 1
        if run >= REPEAT_LIMIT:
            result.append(pieces[index])
            removed += run - 1
        else:
            result.extend(pieces[index:end + 1])
        index = end + 1
    return result, removed


VOICE_CACHE_EVERY = 64  # de GPU-cache van torch legen na zoveel stemafdrukken


def empty_gpu_cache():
    """Geeft de buffers terug die torch op de GPU (MPS) vasthoudt voor hergebruik."""
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


def make_voiceprint(samples):
    """Geeft een functie (start, eind) -> stemafdruk, met het model uit diarize()."""
    import torch

    embedder = _pipeline._embedding
    minimum = int(getattr(embedder, "min_num_samples", 0) or 0)
    calls = [0]

    def voiceprint(start, end):
        # Elke zin heeft een andere lengte, en torch houdt op de GPU voor elke lengte nieuwe
        # buffers aan: bij 1452 zinnen groeide dat van 9,5 naar 14,1 GB. Af en toe legen.
        calls[0] += 1
        if calls[0] % VOICE_CACHE_EVERY == 0:
            empty_gpu_cache()
        a, b = int(start * SAMPLE_RATE), int(end * SAMPLE_RATE)
        if b - a < minimum:  # te kort voor het model: symmetrisch iets ruimer nemen
            extra = (minimum - (b - a)) // 2 + 1
            a, b = max(0, a - extra), min(len(samples), b + extra)
        wave = torch.from_numpy(samples[a:b].copy())[None, None, :]
        with torch.inference_mode():
            vector = np.asarray(embedder(wave))[0]
        norm = np.linalg.norm(vector)
        return None if not np.isfinite(norm) or norm == 0 else vector / norm

    return voiceprint


def refine_speakers(pieces, turns, voiceprint):
    """Controleert per zin de spreker aan de hand van de stem.

    Het sprekermodel is goed in lange beurten, maar rekent een korte reactie van
    de ander vaak bij degene die net sprak. Daarom: van de lange beurten leren hoe
    elke stem klinkt, en elke zin daarmee vergelijken. Geeft (stukken, aantal gewisseld).
    """
    by_voice, seconds = {}, {}
    for start, end, speaker in turns:
        if end - start >= VOICE_LONG_TURN:
            vector = voiceprint(start, min(end, start + VOICE_MAX_CROP))
            if vector is not None:
                by_voice.setdefault(speaker, []).append(vector)
                seconds[speaker] = seconds.get(speaker, 0.0) + end - start
    voices = {}
    for speaker, vectors in by_voice.items():
        if seconds[speaker] >= VOICE_MIN_SECONDS:
            mean = np.mean(vectors, axis=0)
            voices[speaker] = mean / np.linalg.norm(mean)
    if len(voices) < 2:
        # Te weinig lange beurten om stemmen van te leren: zinnen onderling vergelijken.
        return cluster_sentences(pieces, voiceprint)

    refined, changed = [], 0
    for speaker, words in pieces:
        start, end = words[0]["start"], words[-1]["end"]
        # Een spreker zonder eigen stemafdruk (te weinig materiaal) laten we met rust.
        if speaker in voices and end - start >= VOICE_MIN_SENTENCE:
            vector = voiceprint(start, end)
            if vector is not None:
                scores = sorted(((float(vector @ v), s) for s, v in voices.items()), reverse=True)
                best, margin = scores[0][1], scores[0][0] - scores[1][0]
                if best != speaker and margin >= VOICE_MIN_MARGIN:
                    speaker, changed = best, changed + 1
        refined.append((speaker, words))
    return refined, f"{changed} zinnen van spreker gewisseld"


def cluster_sentences(pieces, voiceprint):
    """Sprekers vinden door zinnen onderling te vergelijken, voor korte clips.

    Zinnen van een seconde of langer worden gegroepeerd op gelijkenis van de stem.
    Kortere zinnen sluiten aan bij de groep waar ze het meest op lijken.
    """
    from scipy.cluster.hierarchy import fcluster, linkage

    vectors = []
    for _, words in pieces:
        start, end = words[0]["start"], words[-1]["end"]
        vectors.append(voiceprint(start, end) if end - start >= VOICE_MIN_SENTENCE else None)
    core = [i for i, (_, words) in enumerate(pieces)
            if vectors[i] is not None
            and words[-1]["end"] - words[0]["start"] >= CLUSTER_MIN_SENTENCE]
    if len(core) < 2:
        return pieces, "te weinig zinnen om stemmen te vergelijken"

    tree = linkage(np.array([vectors[i] for i in core]), method="average", metric="cosine")
    labels = fcluster(tree, t=CLUSTER_DISTANCE, criterion="distance")
    groups = sorted(set(labels))
    if len(groups) < 2:
        return pieces, "een stem gevonden"

    centers = {}
    for group in groups:
        mean = np.mean([vectors[i] for i, label in zip(core, labels) if label == group], axis=0)
        centers[group] = mean / np.linalg.norm(mean)
    chosen = dict(zip(core, labels))

    result, previous = [], None
    for index, (speaker, words) in enumerate(pieces):
        if index in chosen:
            group = chosen[index]
        elif vectors[index] is not None:
            group = max(centers, key=lambda g: float(vectors[index] @ centers[g]))
        else:  # te kort om te beoordelen: bij de vorige zin houden
            group = previous if previous is not None else chosen[core[0]]
        previous = group
        result.append((f"stem-{group}", words))
    return result, f"{len(groups)} stemmen gevonden door zinnen te vergelijken"


def speech_share(start, end, turns):
    """Welk deel van dit tijdvak volgens het sprekermodel spraak is (0 tot 1)."""
    if end <= start:
        return 0.0
    overlap = sum(max(0.0, min(end, t_end) - max(start, t_start)) for t_start, t_end, _ in turns)
    return min(overlap / (end - start), 1.0)


def ensure_model(repo):
    """Haalt het Whisper-model eenmalig binnen als het er nog niet is, met voortgang voor het venster.

    Geeft het lokale pad van het model terug. Met een repo-naam raadpleegt mlx_whisper bij elke
    load eerst Hugging Face; een Mac die net uit de slaapstand komt en nog geen verbinding heeft,
    bleef daar hangen. Met een pad gebeurt er niets met het netwerk."""
    import os
    import threading
    from huggingface_hub import HfApi, constants, snapshot_download

    if os.path.isdir(repo):
        return repo
    try:
        return snapshot_download(repo, local_files_only=True)  # staat al in de cache
    except Exception:
        pass

    print(L("Whisper-model wordt eenmalig gedownload (ongeveer 3 GB)...",
            "Downloading the Whisper model once (about 3 GB)..."))
    tell("stage", name="model")
    total = 0
    try:
        info = HfApi().model_info(repo, files_metadata=True)
        total = sum(item.size or 0 for item in info.siblings)
    except Exception:
        pass  # zonder totaal laat het venster een lopende balk zien

    folder = Path(constants.HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))
    finished = threading.Event()

    def watch():
        while not finished.wait(0.7):
            try:
                size = sum(f.stat().st_size for f in (folder / "blobs").glob("*") if f.is_file())
            except OSError:
                continue
            if total:
                tell("progress", fraction=min(size / total, 0.99))

    threading.Thread(target=watch, daemon=True).start()
    try:
        return snapshot_download(repo)
    except Exception as error:
        fail(L(f"Het Whisper-model kon niet worden gedownload ({type(error).__name__}). Dat is eenmalig nodig, "
               "controleer de internetverbinding en probeer het opnieuw.",
               f"The Whisper model could not be downloaded ({type(error).__name__}). This is needed once, "
               "check the internet connection and try again."), local=True)
    finally:
        finished.set()


def detect_language(samples, model_repo):
    """Bepaalt de gesproken taal op de luidste stukken van de opname, niet alleen
    op de eerste 30 seconden: daar zit vaak een leader of muziek.
    Geeft (taalcode, zekerheid) terug."""
    import mlx.core as mx
    import mlx_whisper
    from mlx_whisper.audio import N_FRAMES, log_mel_spectrogram, pad_or_trim

    model = sys.modules["mlx_whisper.transcribe"].ModelHolder.get_model(model_repo, mx.float16)
    window = 30 * SAMPLE_RATE
    starts = list(range(0, max(len(samples) - window, 0) + 1, window))
    starts.sort(key=lambda start: -float((samples[start:start + window] ** 2).mean()))
    chosen = starts[:LANGUAGE_WINDOWS]
    totals = {}
    for start in chosen:
        mel = log_mel_spectrogram(samples[start:start + window], n_mels=model.dims.n_mels)
        mel = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
        _, probabilities = model.detect_language(mel)
        for code, probability in probabilities.items():
            totals[code] = totals.get(code, 0.0) + float(probability)
    best = max(totals, key=totals.get)
    return best, totals[best] / len(chosen)


def is_hallucination(segment, turns=None, language="nl"):
    """Geeft de reden terug als dit segment verzonnen lijkt, anders None.

    `turns` is de uitkomst van het sprekermodel, of None als dat niet gedraaid heeft.
    Een laag eerste woord alleen is niet genoeg: dat komt ook voor bij echte zinnen
    na een pauze. Pas samen met "hier is nauwelijks spraak" telt het.
    """
    words = segment.get("words") or []
    if not words:
        return "geen woorden"
    text = segment.get("text", "").strip().lower()
    for pattern in HALLUCINATION_PATTERNS.get(language, []):
        if re.search(pattern, text):
            return f"zwarte lijst: {pattern}"
    probability = words[0].get("probability", 1.0)
    if probability < FIRST_WORD_MIN_PROBABILITY and turns is not None:
        share = speech_share(words[0]["start"], words[-1]["end"], turns)
        if share < MIN_SPEECH_SHARE:
            return f"eerste woord kans {probability:.3f}, {share:.0%} spraak"
    return None


def to_iconik_segment(words, transcription_id, version_id, speaker=0):
    start_ms = round(words[0]["start"] * 1000)
    end_ms = max(round(words[-1]["end"] * 1000), start_ms)

    iconik_words = []
    for word in words:
        raw = word["word"]
        value = raw.strip()
        if not value:
            continue
        word_start = max(round(word["start"] * 1000) - start_ms, 0)
        word_end = max(round(word["end"] * 1000) - start_ms, word_start)
        score = round(float(word.get("probability", 0.0)), 2)
        # Whisper zet een spatie voor elk nieuw woord. Ontbreekt die, dan hoort
        # dit stuk bij het vorige woord ("woke" + "-predikant").
        if iconik_words and not raw.startswith(" "):
            previous = iconik_words[-1]
            previous["value"] += value
            previous["end_ms"] = max(previous["end_ms"], word_end)
            previous["score"] = min(previous["score"], score)
            continue
        iconik_words.append({
            "value": value,
            "start_ms": word_start,
            "end_ms": word_end,
            "score": score,
        })

    return {
        "segment_type": "TRANSCRIPTION",
        "transcription_id": transcription_id,
        "version_id": version_id,
        "time_start_milliseconds": start_ms,
        "time_end_milliseconds": end_ms,
        "segment_text": " ".join(w["value"] for w in iconik_words),
        "transcription": {"speaker": speaker, "words": iconik_words},
    }


def main():
    parser = argparse.ArgumentParser(description="Whisper-transcript naar Iconik.")
    parser.add_argument("asset_id")
    parser.add_argument("--model", default="mlx-community/whisper-large-v3-mlx")
    parser.add_argument("--language", default="auto", help="Taalcode zoals nl of en; auto herkent de taal.")
    parser.add_argument("--mount", help="Lokaal pad van de storage, eenmalig, zonder het te onthouden.")
    parser.add_argument("--ask-mount", action="store_true",
                        help="Opnieuw vragen waar de storage op deze Mac staat.")
    parser.add_argument("--push", action="store_true", help="Schrijf het transcript naar Iconik.")
    parser.add_argument("--replace", action="store_true",
                        help="Vervang een eerder transcript van dit script.")
    parser.add_argument("--speakers", type=int, help="Aantal sprekers, als je dat weet.")
    parser.add_argument("--no-speakers", action="store_true", help="Sprekerherkenning overslaan.")
    parser.add_argument("--track", type=int, help="Nummer van het audiospoor (1, 2, ...).")
    parser.add_argument("--no-voice-check", action="store_true",
                        help="Stemcontrole per zin overslaan.")
    parser.add_argument("--out", default="/tmp/whisper-test", help="Map voor de proefoutput.")
    args = parser.parse_args()

    iconik = make_client()
    if iconik is None:
        fail("Niet ingelogd. Log in via de app, of zet ICONIK_APP_ID en ICONIK_TOKEN (source ~/.iconik-env).")
    print("Iconik:  " + ("ingelogd als gebruiker" if iconik.uses_login else "persoonlijk token"))

    hf_token = os.environ.get("HF_TOKEN")
    if not args.no_speakers and not speaker_model_available(hf_token):
        fail("Geen sprekermodel: de lokale kopie ontbreekt en er is geen HF_TOKEN. Gebruik --no-speakers om zonder te draaien.")

    process_asset(iconik, args, hf_token)


def prepare_asset(iconik, args, on_info=None, interactive=True):
    """Alles voor de GPU: controles bij Iconik, het bestand vinden en de audio lezen.
    Kan vooruit draaien terwijl een andere opdracht transcribeert."""
    asset = iconik.get(f"assets/v1/assets/{args.asset_id}/")
    version_id = find_version(asset)
    print(f"Asset:   {asset.get('title')}")

    existing = iconik.get(
        f"assets/v1/assets/{args.asset_id}/versions/{version_id}/transcriptions/properties/"
    ).get("objects", [])
    own = [t for t in existing if (t.get("engine_info") or {}).get("name") == ENGINE_NAME]
    foreign = [t for t in existing if t not in own]
    if foreign:
        message = f"Dit asset heeft {len(foreign)} transcript(en) uit een andere bron."
        if args.push:
            fail(L(message + " Die worden nooit overschreven.",
                   f"This asset has {len(foreign)} transcript(s) from another source. Those are never overwritten."), skip=True)
        print(f"Let op: {message} Een proefrun kan, pushen niet.")
    if own:
        message = f"Dit asset heeft al {len(own)} transcript(en) van dit script."
        if args.push and not args.replace:
            fail(message + " Gebruik --push --replace om te vervangen.")
        print(f"Let op: {message}" + (" Wordt vervangen." if args.push else ""))

    path = find_media_file(iconik, args.asset_id, args.mount, getattr(args, "ask_mount", False), interactive)
    print(f"Bestand: {path}")

    print(f"Model:   {args.model}")
    model_path = ensure_model(args.model)
    memory_report("begin")
    tell("stage", name="audio")
    samples, microphones = load_audio_tracks(path, getattr(args, "track", None))
    print(f"Audio:   {len(samples) / SAMPLE_RATE:.0f} seconden")
    memory_report("audio")
    if on_info:
        on_info(asset.get("title"), len(samples) / SAMPLE_RATE)
    return {"asset": asset, "version_id": version_id, "own": own,
            "samples": samples, "microphones": microphones, "model_path": model_path}


def process_asset(iconik, args, hf_token, on_info=None, prepared=None):
    """Transcribeert een asset. `args` heeft de velden van de opties hierboven.
    Geeft een korte samenvatting terug; gooit Problem bij een fout.
    `on_info(titel, seconden)` wordt aangeroepen zodra die bekend zijn.
    `prepared` is de uitkomst van prepare_asset als die al vooruit gelezen is."""
    if prepared is None:
        prepared = prepare_asset(iconik, args, on_info)
    else:
        memory_report("audio (vooruit gelezen)")
    asset, version_id, own = prepared["asset"], prepared["version_id"], prepared["own"]
    samples, microphones = prepared["samples"], prepared["microphones"]
    model_path = prepared.get("model_path") or args.model  # een lokaal pad: laden raakt het netwerk niet
    print("Transcriberen...")
    tell("stage", name="transcriberen")
    import mlx_whisper  # pas hier laden, dat scheelt opstarttijd bij fouten hierboven
    import mlx.core as mx

    # MLX bewaart vrijgegeven werkgeheugen voor hergebruik. Bij Whisper groeit dat zonder
    # grens uit tot vele GB die niet meer gebruikt worden. Instelbaar via mlx_cache_gb.
    try:
        cache_gb = float(load_config().get("mlx_cache_gb", 2))
    except (TypeError, ValueError):
        cache_gb = 2.0
    mx.set_cache_limit(int(cache_gb * 1e9))

    if LISTENER:
        # Whisper toont zijn voortgang met tqdm; daar lezen we op mee.
        try:
            import tqdm as tqdm_module

            class Watched(tqdm_module.tqdm):
                def update(self, n=1):
                    result = super().update(n)
                    if self.total:
                        tell("progress", fraction=min(self.n / self.total, 1.0))
                    return result

            sys.modules["mlx_whisper.transcribe"].tqdm = type("Shim", (), {"tqdm": Watched})
        except Exception:
            pass

    language = args.language
    if not language or language == "auto":
        try:
            language, certainty = detect_language(samples, model_path)
            print(f"Taal:    {language} (herkend, {certainty:.0%})")
        except Exception as error:
            language = None  # dan bepaalt Whisper het zelf op het begin van de opname
            print(f"Taal:    herkennen lukte niet ({type(error).__name__}: {error}), Whisper kiest zelf")
    else:
        print(f"Taal:    {language} (ingesteld)")
    memory_report("taal")

    result = mlx_whisper.transcribe(
        samples,
        path_or_hf_repo=model_path,
        language=language,
        word_timestamps=True,
        verbose=False,  # voortgangsbalk in de terminal
    )
    memory_report("transcriberen")
    mx.clear_cache()  # Whisper blijft geladen, het losse werkgeheugen gaat terug voor de sprekerherkenning

    language = language or result.get("language") or "nl"
    tell("language", code=language)

    turns = None
    if not args.no_speakers:
        print("Sprekers herkennen...")
        tell("stage", name="sprekers")
        turns = diarize(samples, hf_token, args.speakers)
        memory_report("sprekers")

    kept, dropped = [], []
    for segment in result["segments"]:
        reason = is_hallucination(segment, turns, language)
        if reason:
            dropped.append((segment, reason))
        else:
            kept.append(segment)

    print(f"\nSegmenten: {len(kept)} behouden, {len(dropped)} weggefilterd")
    empty = sum(1 for _, reason in dropped if reason == "geen woorden")
    if empty:
        print(f"  waarvan {empty} lege segmenten")
    for segment, reason in dropped:
        if reason != "geen woorden":
            print(f"  weg  [{segment['start']:7.2f}s] {segment['text'].strip()!r}  ({reason})")

    # Meting: hoe zeker is Whisper van de tekst? Verzonnen tekst scoort laag.
    scores = [w.get("probability", 0.0) for s in kept for w in s.get("words") or []]
    if scores:
        ordered = sorted(scores)
        low = sum(1 for p in scores if p < 0.5) / len(scores)
        print(f"Zekerheid: gemiddeld {sum(scores) / len(scores):.2f}, "
              f"mediaan {ordered[len(ordered) // 2]:.2f}, "
              f"{low:.0%} van {len(scores)} woorden onder 0.50")
        if sum(scores) / len(scores) < MIN_CONFIDENCE:
            for s in kept[:5]:
                print(f"  [{s['start']:7.2f}s] {s['text'].strip()[:70]}")
            # Eén slechte run (verkeerd spoor, kapotte audio) mag een eerder goed transcript niet kosten:
            # dat blijft staan. Tot 0.3.6 werd het hier verwijderd.
            note = L("Er is niets naar Iconik geschreven.", "Nothing was written to Iconik.")
            if args.push and args.replace and own:
                note = L("Het eerdere transcript is behouden.", "The earlier transcript was kept.")
            mean = sum(scores) / len(scores)
            fail(L(f"Geen verstaanbare spraak (zekerheid {mean:.2f}, grens {MIN_CONFIDENCE}). {note}",
                   f"No intelligible speech (confidence {mean:.2f}, threshold {MIN_CONFIDENCE}). {note}"), skip=True)
    if not kept:
        fail(L("Geen spraak gevonden, er is niets om te pushen.", "No speech found, nothing to send."), skip=True)

    # Stukken: (spreker van pyannote of None, woorden)
    if microphones and not args.no_speakers:
        pieces, previous = [], None
        for segment in kept:
            for words in split_sentences(segment["words"]):
                previous = speaker_from_tracks(words, microphones, previous)
                pieces.append((previous, words))
        print("Sprekers: afgelezen uit de sporen")
    elif not turns:
        if turns is not None:
            print(L("Geen sprekers gevonden, alles onder Spreker 1.", "No speakers found, everything under Speaker 1."))
        pieces = [(None, s["words"]) for s in kept]
    else:
        pieces = []
        for segment in kept:
            for words in split_sentences(segment["words"]):
                pieces.append((speaker_at(words[0]["start"], words[-1]["end"], turns), words))
        if not args.no_voice_check:
            pieces, note = refine_speakers(pieces, turns, make_voiceprint(samples))
            empty_gpu_cache()
            print(f"Stemcontrole: {note}")
            memory_report("stemcontrole")

    torch = sys.modules.get("torch")
    if torch is not None and torch.backends.mps.is_available():
        torch.mps.empty_cache()

    pieces, repeated = drop_repeats(pieces)
    if repeated:
        print(f"Herhalingen: {repeated} regels weggehaald uit lussen van dezelfde zin")

    # Sprekers nummeren op volgorde van eerste optreden.
    numbers = {}
    for speaker, _ in pieces:
        numbers.setdefault(speaker, len(numbers))
    pieces = [(numbers[speaker], words) for speaker, words in pieces]
    print(f"Sprekers: {len(numbers)}, stukken: {len(pieces)}")

    properties = {
        "language": language,
        "speaker_labels": {str(n): f"{L('Spreker', 'Speaker')} {n + 1}"
                           for n in numbers.values()},
        "engine_info": {
            "type": "EXTERNAL",
            "name": ENGINE_NAME,
            "model": args.model,
            "version": pkg_version("mlx-whisper"),
        },
    }

    if not args.push:
        segments = [to_iconik_segment(w, "<transcription_id>", version_id, n) for n, w in pieces]
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{args.asset_id}.iconik.json"
        out_file.write_text(json.dumps(
            {"properties": properties, "segments": segments}, ensure_ascii=False, indent=2
        ))
        print()
        for segment in segments[:25]:
            seconds = segment["time_start_milliseconds"] / 1000
            number = segment["transcription"]["speaker"] + 1
            print(f"  {seconds:7.2f}s  {L('Spreker', 'Speaker')} {number}  {segment['segment_text']}")
        if len(segments) > 25:
            print(f"  ... en nog {len(segments) - 25} stukken")
        print(f"\nProefrun klaar, niets naar Iconik geschreven.")
        print(f"Volledige payload: {out_file}")
        return {"title": asset.get("title"), "segments": len(segments),
                "speakers": len(numbers), "pushed": False, "language": language}

    tell("stage", name="schrijven")
    still_ours = getattr(args, "still_ours", None)
    if still_ours is not None and not still_ours():
        fail(L("Een andere Mac heeft dit asset overgenomen; er is niets naar Iconik geschreven.",
               "Another Mac took over this asset; nothing was written to Iconik."), skip=True)

    # Eerst het nieuwe transcript helemaal neerzetten, pas daarna het oude weghalen. Mislukt het
    # schrijven halverwege, dan blijft het oude staan en gaat het halve nieuwe weer weg.
    transcriptions = f"assets/v1/assets/{args.asset_id}/versions/{version_id}/transcriptions/properties/"
    transcription_id = iconik.post(transcriptions, properties)["id"]
    segments = [to_iconik_segment(w, transcription_id, version_id, n) for n, w in pieces]
    segments = [s for s in segments if s["segment_text"]]   # alleen witruimte: geen regel
    try:
        for offset in range(0, len(segments), BULK_LIMIT):
            batch = segments[offset:offset + BULK_LIMIT]
            iconik.post(f"assets/v1/assets/{args.asset_id}/segments/bulk/", {"objects": batch})
            print(f"Gepusht: segment {offset + 1} tot en met {offset + len(batch)}")
    except BaseException:
        try:
            delete_transcription(iconik, args.asset_id, version_id, transcription_id)
            print(f"Schrijven mislukte; het halve transcript {transcription_id} is weer weggehaald.")
        except Exception as error:
            print(f"Schrijven mislukte en het halve transcript {transcription_id} weghalen ook: {error}")
        raise

    # Opnieuw opvragen in plaats van de lijst van het begin: zo gaan ook transcripten weg die een
    # andere Mac intussen op dit asset zette, en blijft er precies een van deze app over.
    try:
        existing = iconik.get(transcriptions).get("objects", [])
    except Problem:
        existing = own
    for old in existing:
        if (old.get("engine_info") or {}).get("name") == ENGINE_NAME and old["id"] != transcription_id:
            removed = delete_transcription(iconik, args.asset_id, version_id, old["id"])
            print(f"Oud transcript {old['id']} verwijderd ({removed} segmenten).")
    print(f"\nKlaar. Transcript {transcription_id} staat op het asset.")
    return {"title": asset.get("title"), "segments": len(segments),
            "speakers": len(numbers), "pushed": True, "language": language}


if __name__ == "__main__":
    try:
        main()
    except Problem as problem:
        print(f"FOUT: {problem}", file=sys.stderr)
        sys.exit(1)
