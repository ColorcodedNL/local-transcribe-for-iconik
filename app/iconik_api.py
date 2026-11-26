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
    KEYCHAIN_SERVICE, L, MEDIA_EXTENSIONS, NeedsFolder, Problem, SIDECAR_EXTENSIONS, fail, load_config, print, update_config,
)
import keychain
import settings
from system import ask_folder, find_moved_mount, mount_reachable  # noqa: F401  (macOS: platform_mac.py)


DEFAULT_HOST = "https://app.iconik.io"
REDIRECT_URI = "http://localhost:8765/oauth/callback"
OAUTH_SCOPE = "assets:read assets:write collections:read user_impersonation"
BULK_LIMIT = 500
PAGE_SIZE = 100


LOGIN_LOCK = threading.Lock()

# De instellingen van een Iconik-koppeling, per exemplaar (settings.get("iconik.host", exemplaar)). Ze horen bij
# de connector (iconik.py: SETTINGS), maar worden hier al aangemeld: dit deel laadt vroeg, en de rest van de app
# leest ze voordat de connector zelf gestart is.
S = settings.Setting
SETTINGS = [
    S("iconik.name", "text", "", ("Naam in deze app", "Name in this app"),
      ("Leeg: Iconik, of Iconik (2) en verder als er meer zijn. Alleen op deze Mac.",
       "Empty: Iconik, or Iconik (2) and so on when there are more. Only on this Mac."), max_length=40),
    S("iconik.host", "text", "https://app.iconik.io", ("Iconik-adres", "Iconik address"),
      ("Alleen anders bij een eigen Iconik-omgeving.", "Only different for an own Iconik environment."),
      flat="host", pattern=r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?", max_length=200),
    S("iconik.client_id", "text", "", ("Client-ID", "Client ID"),
      ("Van de app-registratie in Iconik.", "From the app registration in Iconik."),
      flat="client_id", pattern=r"[0-9a-f-]{36}", required=True, extra={"lower": True},
      invalid=("Dat lijkt geen geldige client-ID.", "That does not look like a valid client ID.")),
    S("iconik.mounts", "mapping", {}, ("Opslag op deze Mac", "Storage on this Mac"),
      ("Per storage in Iconik de map op deze Mac.", "For each storage in Iconik the folder on this Mac."),
      flat="mounts"),
    S("iconik.worker_enabled", "bool", False, ("Opdrachten op afstand", "Remote jobs"), flat="worker_enabled"),
    S("iconik.machine_name", "text", "", ("Naam van deze Mac", "Name of this Mac"),
      ("Leeg is de computernaam.", "Empty means the computer name."), flat="machine_name", max_length=64),
    S("iconik.iconik_language", "choice", "", ("Taal van de labels in Iconik", "Language of the labels in Iconik"),
      ("Leeg volgt de app.", "Empty follows the app."), flat="iconik_language", choices=("", "nl", "en")),
    S("iconik.auto_transcribe", "bool", False, ("Automatisch transcriberen", "Auto transcribe"),
      ("Alleen als deze Mac ruimte heeft (zie Transcriptie).", "Only when this Mac has room (see Transcription)."),
      flat="auto_transcribe"),
    S("iconik.auto_audio", "bool", False, ("Ook audio", "Audio too"), flat="auto_audio"),
    S("iconik.auto_max_minutes", "number", 60.0, ("Langste asset (minuten)", "Longest asset (minutes)"),
      flat="auto_max_minutes", minimum=0, advanced=True),
]
settings.register_integration("iconik")
for _setting in SETTINGS:
    settings.register(_setting)


def api_host(instance=None):
    return (settings.get("iconik.host", _instance(instance)) or DEFAULT_HOST).rstrip("/")


def _instance(instance=None):
    """Het exemplaar: het gegeven, of de enige Iconik (None als er nog geen is; settings.InstanceError bij meer)."""
    return instance or settings.instance_id("iconik")


# De inlog bij Iconik staat per exemplaar in de sleutelhanger (keychain.py: account "<exemplaar>:login"), en wordt
# één keer gelezen en daarna uit het geheugen gehaald. Tot stap 5 stond hij voor de enige Iconik onder account "login"
# (en nog eerder onder de oude app-naam). Dat item verhuist eenmalig naar het enige exemplaar: lezen, onder de nieuwe
# naam schrijven en het oude weghalen. Lukt weghalen niet (macOS kan er nog eens om vragen), dan probeert uitloggen het
# opnieuw. Dat het verhuisd is, staat bij het exemplaar (settings.note), zodat een oude inlog nooit terugkomt.
LOGIN = "login"
LEGACY_ACCOUNT = "login"
LEGACY_NOTE = "keychain"       # settings.note(exemplaar, ...): "moved" als het oude item is verhuisd (of er niet was)


def _legacy_owner(instance):
    """Het oude item hoort bij de enige Iconik, en alleen zolang het nog niet verhuisd is."""
    return settings.instances("iconik") == [instance] and settings.noted(instance, LEGACY_NOTE) != "moved"


def _move_legacy(instance):
    """Verhuist de inlog van vóór stap 5 naar dit exemplaar. Geeft de inlog (tekst) of None."""
    import keyring
    raw = keyring.get_password(KEYCHAIN_SERVICE, LEGACY_ACCOUNT)
    if raw:
        keychain.put(instance, LOGIN, raw)
        print("Inlog bij Iconik verhuisd naar een eigen item per koppeling.")
        _delete_legacy()
    settings.note(instance, LEGACY_NOTE, "moved")
    return raw


def _delete_legacy():
    """Haalt de oude items weg (als ze er nog zijn). Een weigering van macOS alleen in het log."""
    import keyring
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, LEGACY_ACCOUNT)
    except Exception as error:
        if type(error).__name__ != "PasswordDeleteError":   # stond er niet: goed
            print(f"Oude inlog weghalen uit de sleutelhanger lukte niet ({type(error).__name__}); bij uitloggen opnieuw.")


# Bij het opstarten vragen het venster en de achtergrondtaak tegelijk om de inlog; keychain.py laat de tweede op de
# eerste wachten, en de verhuizing gebeurt onder dit slot maar één keer.
_login_read = threading.Lock()


def load_login(instance=None, fresh=False):
    """De bewaarde inlog van een exemplaar (uit het geheugen, of met `fresh` uit de sleutelhanger), of None."""
    instance = _instance(instance)
    if not instance:
        return None
    with _login_read:
        raw = keychain.get(instance, LOGIN, fresh=fresh)
        if raw is None and _legacy_owner(instance):
            raw = _move_legacy(instance)
    try:
        login = json.loads(raw) if raw else None
    except ValueError:
        login = None
    return login if isinstance(login, dict) else None


def save_login(login, instance=None):
    instance = _instance(instance)
    keychain.put(instance, LOGIN, json.dumps(login))
    _has_login[instance] = (True, time.monotonic())


def clear_login(instance=None):
    instance = _instance(instance)
    if not instance:
        return
    _has_login[instance] = (False, time.monotonic())
    keychain.delete(instance, LOGIN)
    if settings.instances("iconik") == [instance]:
        _delete_legacy()   # een oud item dat bij het verhuizen bleef staan, gaat nu alsnog weg


# Is er een inlog? Het venster vraagt dat elke seconde; daarvoor hoeft de sleutelhanger niet elke keer.
HAS_LOGIN_SECONDS = 5
_has_login = {}   # exemplaar -> (uitkomst, wanneer gekeken)


def has_login(instance=None):
    instance = _instance(instance)
    if not instance:
        return False
    known, checked = _has_login.get(instance, (None, 0.0))
    if known is not None and time.monotonic() - checked < HAS_LOGIN_SECONDS:
        return known
    found = bool(load_login(instance))
    _has_login[instance] = (found, time.monotonic())
    return found


def token_request(fields, instance=None):
    """Vraagt bij de Iconik van een exemplaar een token op en bewaart het meteen. Geeft de inlog terug."""
    instance = _instance(instance)
    response = requests.post(f"{api_host(instance)}/API/auth/v1/oauth/token/", data=fields, timeout=30)
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
    save_login(login, instance)
    return login


def start_login(instance=None):
    """Begin van de inlog bij een exemplaar: geeft (adres voor de browser, state, verifier)."""
    instance = _instance(instance)
    client_id = settings.get("iconik.client_id", instance)
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
    return f"{api_host(instance)}/oauth/authorize?{query}", state, verifier


def finish_login(code, verifier, instance=None):
    instance = _instance(instance)
    return token_request({
        "grant_type": "authorization_code", "client_id": settings.get("iconik.client_id", instance),
        "code": code, "code_verifier": verifier, "redirect_uri": REDIRECT_URI,
    }, instance)


class Iconik:
    """Praat met de API van Iconik.

    Zonder app_id: met de inlog van de gebruiker bij dit exemplaar (wordt vanzelf ververst).
    Met app_id en token: met een persoonlijk token, zoals in ~/.iconik-env.
    """

    def __init__(self, app_id=None, token=None, instance=None):
        self.session = requests.Session()
        self.uses_login = app_id is None
        self.instance = _instance(instance)
        if not self.uses_login:
            self.session.headers.update({"App-ID": app_id, "Auth-Token": token})

    def _authorize(self, rejected=None):
        """Zet een geldig token klaar. `rejected` is een token dat Iconik net weigerde."""
        # Een slot voor het hele programma: een ververstoken mag maar een keer gebruikt
        # worden, en hergebruik maakt de hele inlog ongeldig.
        with LOGIN_LOCK:
            login = load_login(self.instance)
            if not login:
                fail(L("Niet ingelogd bij Iconik.", "Not logged in to Iconik."), local=True)
            # Is het geweigerde token intussen al door een ander vervangen, dan niet opnieuw verversen.
            stale = rejected is not None and login["access_token"] == rejected
            if stale or login["expires_at"] - time.time() < 120:
                if not login.get("refresh_token"):
                    clear_login(self.instance)
                    fail(L("De inlog bij Iconik is verlopen. Log opnieuw in.", "The Iconik login has expired. Log in again."), local=True)
                try:
                    login = token_request({
                        "grant_type": "refresh_token",
                        "client_id": settings.get("iconik.client_id", self.instance),
                        "refresh_token": login["refresh_token"],
                    }, self.instance)
                except Problem:
                    # Misschien heeft iets anders (het script op de opdrachtregel) intussen ververst: dan
                    # staat er een nieuwere inlog in de sleutelhanger dan in het geheugen.
                    latest = load_login(self.instance, fresh=True)
                    if latest and latest.get("refresh_token") != login.get("refresh_token") \
                            and latest["expires_at"] - time.time() >= 120:
                        login = latest
                        self.session.headers["Authorization"] = f"Bearer {login['access_token']}"
                        return
                    clear_login(self.instance)
                    fail(L("De inlog bij Iconik is verlopen. Log opnieuw in.", "The Iconik login has expired. Log in again."), local=True)
            self.session.headers["Authorization"] = f"Bearer {login['access_token']}"

    def _call(self, method, path, **kwargs):
        if self.uses_login:
            self._authorize()
        url = f"{api_host(self.instance)}/API/{path}"
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


def make_client(instance=None):
    """De inlog van de gebruiker bij dit exemplaar (zonder: de enige Iconik) als die er is, anders het token uit de
    omgeving, anders None."""
    try:
        instance = _instance(instance)
        if instance and has_login(instance):
            return Iconik(instance=instance)
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


def set_mount(storage_id, folder, instance=None):
    """Onthoudt (of vergeet, met None) waar een storage op deze Mac staat."""
    def change(mounts):
        if folder is None:
            mounts.pop(storage_id, None)
        else:
            mounts[storage_id] = str(folder)
    settings.update("iconik.mounts", change, _instance(instance))



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

    instance = getattr(iconik, "instance", None)   # de opslag hoort bij de Iconik van deze verbinding
    mounts = settings.get("iconik.mounts", _instance(instance))
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
                set_mount(item["storage_id"], moved, instance)
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
        set_mount(storage_id, mount, instance)
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
