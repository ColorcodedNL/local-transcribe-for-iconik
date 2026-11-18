"""De verbinding met Iconik: inloggen (OAuth, keychain), de client, transcripten weghalen, ISG-mounts en
het mediabestand van een asset op deze Mac vinden.

Wat de rest van de app met Iconik doet, loopt via de connector in iconik.py.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
import requests

from base import (  # noqa: F401
    KEYCHAIN_SERVICE, L, MEDIA_EXTENSIONS, NeedsFolder, OLD_APP_NAME, Problem, SIDECAR_EXTENSIONS, fail, load_config, print, update_config,
)
import settings
from system import ask_folder, find_moved_mount, mount_reachable  # noqa: F401  (macOS: platform_mac.py)


DEFAULT_HOST = "https://app.iconik.io"
REDIRECT_URI = "http://localhost:8765/oauth/callback"
OAUTH_SCOPE = "assets:read assets:write collections:read user_impersonation"
BULK_LIMIT = 500
PAGE_SIZE = 100


LOGIN_LOCK = threading.Lock()


def api_host():
    return (settings.get("iconik.host") or DEFAULT_HOST).rstrip("/")


# De inlog in het geheugen: de sleutelhanger één keer lezen in plaats van bij elke aanroep naar Iconik. Bij
# een testbuild die macOS niet herkent scheelt dat een rij wachtwoordvragen. Deze app werkt het geheugen zelf
# bij als hij ververst (save_login); ververst iets anders het token, dan leest _authorize opnieuw (fresh).
_login_cache = [False, None]   # [gelezen, inlog]
# Bij het opstarten vragen het venster en de achtergrondtaak tegelijk om de inlog. Zonder slot lazen ze allebei
# de sleutelhanger terwijl macOS nog op het wachtwoord wachtte: twee vragen. Nu wacht de tweede op de eerste.
_login_read = threading.Lock()


def load_login(fresh=False):
    """De bewaarde inlog (uit het geheugen, of met `fresh` uit de sleutelhanger van macOS), of None."""
    with _login_read:
        if _login_cache[0] and not fresh:
            return dict(_login_cache[1]) if _login_cache[1] else None
        return _read_login()


def _read_login():
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
        login = json.loads(raw) if raw else None
    except ValueError:
        login = None
    _login_cache[:] = [True, login]
    return dict(login) if login else None


def save_login(login):
    import keyring
    keyring.set_password(KEYCHAIN_SERVICE, "login", json.dumps(login))
    _login_cache[:] = [True, dict(login)]
    _has_login[:] = [True, time.monotonic()]


def clear_login():
    import keyring
    _login_cache[:] = [True, None]
    _has_login[:] = [False, time.monotonic()]
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, "login")
    except Exception:
        pass


# Is er een inlog? Het venster vraagt dat elke seconde; de sleutelhanger hoeft dan niet elke keer.
# (Sinds de inlog in het geheugen staat, is dit vooral nog voor de eerste keer lezen.)
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
    client_id = settings.get("iconik.client_id")
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
        "grant_type": "authorization_code", "client_id": settings.get("iconik.client_id"),
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
                        "client_id": settings.get("iconik.client_id"),
                        "refresh_token": login["refresh_token"],
                    })
                except Problem:
                    # Misschien heeft iets anders (het script op de opdrachtregel) intussen ververst: dan
                    # staat er een nieuwere inlog in de sleutelhanger dan in het geheugen.
                    latest = load_login(fresh=True)
                    if latest and latest.get("refresh_token") != login.get("refresh_token") \
                            and latest["expires_at"] - time.time() >= 120:
                        login = latest
                        self.session.headers["Authorization"] = f"Bearer {login['access_token']}"
                        return
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


def set_mount(storage_id, folder):
    """Onthoudt (of vergeet, met None) waar een storage op deze Mac staat."""
    def change(mounts):
        if folder is None:
            mounts.pop(storage_id, None)
        else:
            mounts[storage_id] = str(folder)
    settings.update("iconik.mounts", change)



def find_media_file(iconik, asset_id, mount_override=None, ask_mount=False, interactive=True):
    """Zoekt het mediabestand van een asset op deze Mac.

    Waar een storage lokaal staat wordt per storage onthouden. Is dat nog niet
    bekend en klopt het pad uit Iconik hier niet, dan vraagt een venster erom.
    """
    files = iconik.get(f"files/v1/assets/{asset_id}/files/").get("objects", [])
    stored = [f for f in files if f.get("type") == "FILE"]
    media = [f for f in stored if Path(f.get("name", "")).suffix.lower() in MEDIA_EXTENSIONS]
    if not media:
        other = sorted({Path(f.get("name", "")).suffix.lower() or L("zonder extensie", "no extension")
                        for f in stored if Path(f.get("name", "")).suffix.lower() not in SIDECAR_EXTENSIONS})
        if other:
            fail(L(f"Geen audio of video op dit asset ({', '.join(other)}).",
                   f"No audio or video on this asset ({', '.join(other)})."), skip=True)
        fail(L("Geen mediabestand op dit asset.", "No media file on this asset."), skip=True)

    def relative_of(item):
        return Path(item.get("directory_path", "")) / item["name"]

    if len(media) > 1:
        print(f"Let op: {len(media)} kopieën van het mediabestand, de eerste die op deze Mac te vinden is wordt gebruikt.")

    if mount_override:
        for item in media:
            path = Path(mount_override) / relative_of(item)
            if path.is_file():
                return path
        fail(L(f"Bestand niet gevonden: {Path(mount_override) / relative_of(media[0])}",
               f"File not found: {Path(mount_override) / relative_of(media[0])}"))

    mounts = settings.get("iconik.mounts")
    chosen = media[0]
    if not ask_mount:
        # Iconik koppelt identieke bestanden op verschillende plekken aan een asset. Elke kopie op een
        # opslag waarvan het mount point bekend is, mag: de eerste die hier echt staat wint.
        known_copies = [item for item in media if mounts.get(item.get("storage_id"))]
        for item in known_copies:
            known, relative = mounts[item["storage_id"]], relative_of(item)
            if (Path(known) / relative).is_file():
                return Path(known) / relative
            moved = find_moved_mount(known, relative)
            if moved is not None:
                set_mount(item["storage_id"], moved)
                print(f"Opslag staat nu op {moved} (was {known}), dat is onthouden.")
                return moved / relative
        unknown = [item for item in media if not mounts.get(item.get("storage_id"))]
        if known_copies and not unknown:
            known, relative = mounts[known_copies[0]["storage_id"]], relative_of(known_copies[0])
            if not Path(known).is_dir():
                fail(L(f"De map {known} is niet bereikbaar. Is de opslag (ISG) gemount?",
                       f"The folder {known} cannot be reached. Is the storage (ISG) mounted?"), local=True)
            fail(L(f"Bestand niet gevonden: {Path(known) / relative}", f"File not found: {Path(known) / relative}"))
        if unknown:
            chosen = unknown[0]   # een kopie op een opslag waarvan deze Mac het mount point nog niet weet
    relative = relative_of(chosen)
    storage_id = chosen["storage_id"]

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
