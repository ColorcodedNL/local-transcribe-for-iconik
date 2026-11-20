#!/usr/bin/env python3
"""Lokale ontvanger voor de Iconik Custom Action "Transcribeer".

Iconik opent in je browser http://localhost:8765/transcribe/<sleutel>?asset_ids=...
Dit programma zet die assets in een rij, transcribeert ze een voor een en
pusht het resultaat naar Iconik. De pagina in de browser toont de voortgang.

Gebruik:
    source ~/.iconik-env
    uv run serve.py
Stoppen met Ctrl+C. Luistert alleen op deze Mac zelf (127.0.0.1).
"""

import argparse
import atexit
import json
import os
import re
import subprocess
import queue
import secrets
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests

import app_shell
import refine
import resources
import settings as setting   # de lijst met alle instellingen; `settings` hieronder is die van een opdracht
import system
import jobs as job_list
import transcribe
import updater
import worker as remote
from transcribe import Problem

PORT = 8765
PAGE_SIZE = 100
MAX_DEPTH = 5  # hoe diep collecties in collecties worden gevolgd

KEY_FILE = transcribe.CONFIG_PATH.parent / "key"
# Wie mag er bij de ontvanger? Alles behalve de paar open routes hieronder vraagt een sessie:
# - het venster van de app: een cookie met SESSION, die het bij het openen krijgt via /window/open. SESSION
#   bestaat alleen in het geheugen van deze start, dus alleen het venster van dit proces heeft hem;
# - de app zelf en een tweede start van de app (zelfde gebruiker): de sleutel uit KEY_FILE (alleen leesbaar
#   voor de eigenaar) in de header X-Local-Transcribe-Key.
# Zonder sessie kan een ander proces of een andere gebruiker op deze Mac de opdrachten, de naam van de
# ingelogde gebruiker, transcripten en beeld niet meer opvragen op poort 8765.
SESSION = secrets.token_urlsafe(32)
SESSION_COOKIE = "lt_session"


def window_url(page="/"):
    """Het adres waarmee de app haar eigen venster naar een pagina stuurt: altijd langs /window/open, zodat
    het venster de cookie zeker heeft. Start de app door een klik in Iconik, dan komt de pagina van die
    klik soms voor het eerste openen klaar is; rechtstreeks laden gaf dan 403 in het venster."""
    return f"/window/open?t={SESSION}&next={quote(page or '/', safe='')}"
KEY_HEADER = "X-Local-Transcribe-Key"
pending_login = {}        # state -> verifier, voor een inlog die onderweg is
pending_back = {}         # state -> pagina waar het venster na het inloggen heen gaat (Instellingen)
pending_ids = []          # opdrachten die wachten tot er is ingelogd
window_state = {"n": 0, "page": "/"}   # wat het venster van de app moet tonen
show_hook = None  # de app zet hier een functie neer die het venster toont
save_hook = None  # idem: toont het bewaarvenster van macOS (map, naam) en geeft het gekozen pad of None
open_hook = None  # idem: laat iemand een ondertitelbestand kiezen (map) en geeft het pad of None
restart_hook = None  # idem, om opnieuw te starten na een code-update
settings_hook = None  # idem: opent het venster Instellingen, eventueel op een tab
title_hook = None  # idem: zet de titel van het venster Instellingen (pywebview neemt de <title> niet over)
clear_log_hook = None  # idem: maakt het log leeg (alleen de app weet waar het staat)


def client():
    """De verbinding met Iconik, of None als er nog niet is ingelogd."""
    return transcribe.make_client()


def listener_key():
    """De sleutel die de app meestuurt. Wordt de eerste keer aangemaakt."""
    if os.environ.get("LISTENER_KEY"):
        return os.environ["LISTENER_KEY"]
    try:
        return KEY_FILE.read_text().strip()
    except OSError:
        key = secrets.token_hex(16)
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(key)
        KEY_FILE.chmod(0o600)
        return key


def enqueue(asset_ids):
    added = sum(add_job(asset_id) for asset_id in dict.fromkeys(asset_ids))
    waiting = [job for job in jobs if job.status == "wachtend"]
    threading.Thread(target=fill_titles, args=(waiting,), daemon=True).start()
    print(f"Ontvangen: {len(asset_ids)} asset(s), {added} toegevoegd aan de rij.")
    return added


jobs = job_list.JobList()  # de opdrachten, nieuwste achteraan (jobs.py)
jobs_lock = job_list.lock
work = queue.Queue()
settings = {}
MAX_KEPT_JOBS = job_list.MAX_KEPT
_dirty = threading.Event()


def jobs_path():
    return transcribe.CONFIG_PATH.parent / "jobs.json"


def save_jobs():
    jobs.save(jobs_path())


def touch_jobs():
    """Er is iets veranderd: de lijst wordt binnen een paar seconden bewaard (niet bij elke wijziging,
    anders schrijft een collectie van honderden assets honderd keer)."""
    _dirty.set()


def _saver():
    while True:
        _dirty.wait()
        time.sleep(2)
        _dirty.clear()
        save_jobs()


def load_jobs():
    """Haalt de bewaarde lijst terug, bij de start. Wat nog liep of wachtte is afgebroken."""
    jobs.load(jobs_path())


current = [None]          # de opdracht die nu verwerkt wordt
# Zonder opdracht en zonder open venster stopt de ontvanger zichzelf, zodat het geheugen vrijkomt.
# De volgende klik in Iconik start hem opnieuw. Het venster haalt elke paar seconden /state op.
IDLE_EXIT = int(os.environ.get("LOCALTRANSCRIBE_IDLE_EXIT", "120"))
activity = {"poll": time.time(), "done": time.time()}
# Een opdracht die zo lang niets meldt is vastgelopen, bijvoorbeeld na de slaapstand met een weggevallen
# opslag of verbinding. Monotone klok: de tijd dat de Mac sliep telt niet mee.
STALL_SECONDS = int(os.environ.get("LOCALTRANSCRIBE_STALL", "900"))
STALL_GRACE = 20          # seconden die de app krijgt om zelf af te sluiten voor we het afdwingen
last_progress = [time.monotonic()]
UI_FILE = Path(__file__).with_name("ui.html")
REFINE_FILE = Path(__file__).with_name("refine.html")
SETTINGS_FILE = Path(__file__).with_name("settings.html")
SETTINGS_TABS = ("general", "transcription", "files", "integrations")


def page_text(file):
    """De tekst van een pagina van het venster. Met shell 5 of hoger via app_shell: dan komt hij bij een
    code-update uit de gecontroleerde zip in het geheugen en niet uit een map die aan te passen is."""
    reader = getattr(app_shell, "read_text", None)
    return reader(file.name) if reader else file.read_text()


def make_options(job):
    # Een opdracht uit de rij die een andere Mac heeft overgenomen, wordt niet meer naar Iconik geschreven.
    still_ours = (lambda: job.asset_id not in remote.lost) if job.remote else None
    return argparse.Namespace(
        asset_id=job.asset_id, model=settings["model"], language=settings["language"] or setting.get("transcription.language"),
        mount=settings["mount"], push=True, replace=True, speakers=None, still_ours=still_ours,
        no_speakers=not transcribe.speaker_model_available(settings["hf_token"]), no_voice_check=False, out="/tmp/whisper-test")


PREFETCH_MIN_FREE_GB = setting.SCHEMA["transcription.prefetch_min_free_gb"].default   # vooruit lezen piekt rond 3,8 GB bovenop de lopende opdracht


def prefetch_room():
    """(mag vooruit lezen, reden). Vooruit lezen loopt tijdens de lopende opdracht, dus beide pieken tellen
    op (in het slechtste geval ongeveer 9,9 + 3,8 GB). Op een Mac met weinig geheugen wacht het daarom tot er
    genoeg vrij is; is dat er niet, dan leest de volgende opdracht straks zelf, zonder swappen."""
    need = setting.get("transcription.prefetch_min_free_gb")
    available, level = resources.memory()
    if level is not None and level > resources.PRESSURE_NORMAL:
        return False, "geheugendruk hoog"
    if available is not None and available < need * 1e9:
        return False, f"{available / 1e9:.1f} GB vrij, minstens {need:g} GB nodig"
    return True, ""


def start_prefetch():
    """Leest de audio van de volgende opdracht alvast in, terwijl de GPU met deze bezig is.

    Bij een netwerkvolume kost het lezen van een groot bestand tientallen seconden.
    Lukt het vooruit lezen niet (of is er een mapkeuze nodig, of te weinig geheugen vrij), dan doet de
    opdracht het later zelf.
    """
    with work.mutex:
        upcoming = work.queue[0] if work.queue else None
    if upcoming is None or upcoming.prefetch is not None:
        return
    room, reason = prefetch_room()
    if not room:
        print(f"Vooruit lezen overgeslagen ({reason}): {upcoming.asset_id} leest straks zelf.")
        return
    box = {"events": [], "lines": [], "result": None}

    def run():
        transcribe._local.events, transcribe._local.lines = box["events"], box["lines"]
        try:
            connection = client()
            if connection is not None:
                def on_info(title, seconds, job=upcoming):
                    job.set(title=title or "", duration=seconds)
                box["result"] = transcribe.prepare_asset(connection, make_options(upcoming), on_info, interactive=False)
        except transcribe.NeedsFolder:
            box["lines"].append("Vooruit lezen overgeslagen: mount point nog onbekend.")
        except Exception as error:
            box["lines"].append(f"Vooruit lezen gestopt ({type(error).__name__}: {error}), de opdracht leest zelf.")
        finally:
            transcribe._local.events = transcribe._local.lines = None

    box["thread"] = threading.Thread(target=run, daemon=True)
    upcoming.set(prefetch=box)
    box["thread"].start()
    print(f"Vooruit lezen gestart: {upcoming.asset_id}")


def listen(kind, **data):
    """Krijgt de voortgang van de motor en zet die bij de lopende opdracht."""
    last_progress[0] = time.monotonic()
    job = current[0]
    if job is None:
        return
    if kind == "stage":
        job.set(stage=data["name"], fraction=None)
        if data["name"] == "transcriberen":
            start_prefetch()  # het eigen bestand is gelezen, het netwerk is vrij
    elif kind == "progress":
        job.set(fraction=data["fraction"])
    elif kind == "language":
        job.set(language=data["code"])
    elif kind == "tracks":
        job.set(tracks=data["tracks"])


ACTION_TITLE = "Local Transcribe"
ACTION_URL = "iconiktranscribe://transcribe"
ACTION_CONTEXTS = ("ASSET", "COLLECTION", "BULK")
# De knoppen in Iconik: (titel, link, contexten). Refine transcript opent de editor, alleen op één asset.
ACTIONS = ((ACTION_TITLE, ACTION_URL, ACTION_CONTEXTS),
           ("Refine transcript", "iconiktranscribe://refine", ("ASSET",)))
actions = {}              # "state": aanwezig | ontbreekt | geweigerd | onbekend
update = {}               # nieuwere versie op GitHub: version, url, mode (code of dmg), state, error
update_lock = threading.Lock()


def version_tuple(text):
    numbers = [int(n) for n in re.findall(r"\d+", text or "")[:3]]
    return tuple(numbers + [0] * (3 - len(numbers)))


def update_view():
    """Wat het venster van een beschikbare update mag zien."""
    if not update:
        return None
    view = {key: update[key] for key in ("version", "mode", "state", "error") if key in update}
    view["waiting"] = update.get("state") == "ready" and busy()
    return view


def check_update():
    """Kijkt bij de start en daarna elke 6 uur of er op GitHub een nieuwere versie staat."""
    url = f"https://api.github.com/repos/{transcribe.RELEASES_REPO}/releases/latest"
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": f"{transcribe.DISPLAY_NAME}/{transcribe.APP_VERSION}"}
    while True:
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                found = updater.find_update(response.json(), transcribe.RELEASES_REPO, transcribe.APP_VERSION,
                                            app_shell.SHELL_VERSION, updater.bad_versions())
                with update_lock:
                    if update.get("state") in ("installing", "ready"):
                        pass  # er wordt al geïnstalleerd: de melding met rust laten
                    elif found:
                        if update.get("version") != found["version"]:
                            print(f"Nieuwe versie beschikbaar: {found['version']} ({found['mode']}, {found['url']})")
                        update.clear()
                        update.update(found, state="available")
                    else:
                        update.clear()
        except Exception:
            pass  # geen netwerk of GitHub onbereikbaar: volgende keer opnieuw
        time.sleep(6 * 3600)


def install_update():
    """Klik op Installeren: downloaden, controleren, neerzetten en herstarten zodra er niets meer loopt."""
    with update_lock:
        if update.get("mode") != "code" or update.get("state") not in ("available", "error"):
            return
        info = dict(update)
        update.update(state="installing", error="")

    def work():
        try:
            version = updater.install(info, transcribe.APP_VERSION)
        except updater.UpdateError as error:
            print(f"Update {info['version']} mislukt: {error.nl}")
            update.update(state="error", error=transcribe.L(error.nl, error.en))
            return
        except requests.RequestException as error:
            print(f"Update {info['version']} mislukt: {type(error).__name__}: {error}")
            update.update(state="error", error=transcribe.L("Geen verbinding met GitHub.", "No connection to GitHub."))
            return
        except Exception as error:
            print(f"Update {info['version']} mislukt: {type(error).__name__}: {error}")
            update.update(state="error", error=f"{type(error).__name__}: {error}")
            return
        update.update(state="ready")
        print(f"Code-update {version} staat klaar; de app herstart zodra er geen opdracht loopt")
        while busy():
            time.sleep(2)
        if restart_hook:
            restart_hook()

    threading.Thread(target=work, daemon=True, name="update-install").start()


ASSET_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def valid_asset_id(asset_id):
    """Een asset-ID dat de editor mag gebruiken. Het komt in een pad bij Iconik en in bestandsnamen op deze
    Mac terecht, dus alleen letters, cijfers, streepje en liggend streepje: geen schuine streep, punt of
    vraagteken waarmee het naar iets anders zou wijzen."""
    return bool(ASSET_ID_RE.fullmatch(asset_id or ""))


def rotate_log(path, limit=5 * 1024 * 1024):
    """Een log boven de grens wordt <naam>.1 (een vorige .1 verdwijnt); daarna begint een leeg log. Geeft True
    als dat gebeurd is: wie het log open heeft, moet het dan opnieuw openen. Het log groeide anders eindeloos,
    met de titels van alle assets erin."""
    try:
        if path.stat().st_size <= limit:
            return False
        os.replace(path, path.with_name(path.name + ".1"))
        return True
    except OSError:
        return False


def refine_link(url):
    """De link van de knop Refine transcript: iconiktranscribe://refine?asset_ids=<id>[,...]. Geeft de pagina
    van de editor voor het eerste asset, of None. Alleen openen: niets in de rij, dus geen sleutel nodig."""
    if not (url or "").startswith("iconiktranscribe://refine"):
        return None
    query = parse_qs(urlparse(url).query)
    ids = [i for value in query.get("asset_ids", []) for i in value.split(",") if i]
    if not ids or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", ids[0]):
        return None
    return f"/refine?asset={ids[0]}"


def missing_actions(connection):
    """De knoppen die nog niet in deze Iconik-omgeving staan, als (context, titel, link)."""
    existing = connection.get("assets/v1/custom_actions/?per_page=200").get("objects", [])
    have = {(a.get("context"), a.get("url")) for a in existing}
    return [(context, title, url) for title, url, contexts in ACTIONS for context in contexts
            if (context, url) not in have]


def check_actions():
    """Kijkt of de knop al in deze Iconik-omgeving staat. Dat hoeft maar een keer, door een beheerder."""
    try:
        actions["state"] = "ontbreekt" if missing_actions(client()) else "aanwezig"
    except Exception:
        # niet kunnen kijken: dan vallen we de gebruiker er niet mee lastig
        actions.setdefault("state", "onbekend")  # bij een storing houden we de vorige uitslag aan
    finally:
        actions["checked"] = time.time()
        actions["busy"] = False


def install_actions():
    try:
        connection = client()
        for context, title, url in missing_actions(connection):
            connection.post(f"assets/v1/custom_actions/{context}/",
                            {"context": context, "title": title, "type": "OPEN", "url": url})
        actions["state"] = "aanwezig"
        print("De knoppen van Local Transcribe staan in Iconik.")
    except Exception as error:
        actions["state"] = "geweigerd"
        print(f"Knop in Iconik zetten lukte niet: {error}")


who = {}                  # "name": wie er is ingelogd, zodra dat is opgehaald


def fetch_who():
    try:
        user = client().get("users/v1/users/current/")
        full = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
        email = user.get("email") or ""
        who["name"] = f"{full} ({email})" if full and email else full or email
    except Exception:
        who["name"] = ""  # dan tonen we alleen dat er verbinding is


storage_names = {}        # storage-ID -> naam in Iconik, voor de instellingen


def fetch_storage_name(storage_id):
    try:
        storage_names[storage_id] = client().get(f"files/v1/storages/{storage_id}/").get("name") or ""
    except Exception:
        storage_names[storage_id] = ""  # geen recht of geen verbinding: dan zonder naam


def mount_list(mounts):
    rows = []
    for storage_id, path in mounts.items():
        if storage_id not in storage_names:
            storage_names[storage_id] = ""
            threading.Thread(target=fetch_storage_name, args=(storage_id,), daemon=True).start()
        rows.append({"id": storage_id, "name": storage_names[storage_id], "path": path})
    return rows


def change_mount(storage_id, remove=False):
    mounts = setting.get("iconik.mounts")
    if storage_id not in mounts:
        return
    if remove:
        transcribe.set_mount(storage_id, None)
        return
    name = storage_names.get(storage_id) or storage_id
    folder = transcribe.ask_folder(transcribe.L(
        f"ISG \u201c{name}\u201d: kies het mount point van deze ISG op deze Mac.",
        f"ISG \u201c{name}\u201d: choose the mount point of this ISG on this Mac."), mounts[storage_id])
    if folder:
        transcribe.set_mount(storage_id, folder)   # na het venster opnieuw gelezen, zie set_mount


def iconik_account():
    """Hoe deze Mac met Iconik verbonden is: ("geen" | "uitgelogd" | "gebruiker" | "token", client-ID). Start op
    de achtergrond de controle op de knoppen in Iconik en het ophalen van de naam van de gebruiker."""
    client_id = setting.get("iconik.client_id")
    connection = client()
    account = ("geen" if connection is None and not client_id else
               "uitgelogd" if connection is None else
               "gebruiker" if connection.uses_login else "token")
    if connection is None:
        actions.clear()
    elif "state" not in actions or (not actions.get("busy") and actions.get("state") != "geweigerd" and time.time() - actions.get("checked", 0) > 60):
        actions.setdefault("state", "onbekend")
        actions["busy"] = True
        threading.Thread(target=check_actions, daemon=True).start()
    if connection is not None and "name" not in who:
        who["name"] = ""
        threading.Thread(target=fetch_who, daemon=True).start()
    return account, client_id


def app_state():
    """Alles wat het venster toont, als gegevens."""
    now = datetime.now()
    out = []
    for row in jobs.views():
        end = row["finished"] or now
        out.append({
            "n": row["n"], "asset_id": row["asset_id"], "title": row["title"],
            "status": row["status"], "message": row["message"],
            "added": row["added"].strftime("%H:%M"), "stage": row["stage"],
            "fraction": row["fraction"], "tracks": row["tracks"],
            "duration": row["duration"],
            "result": row["result"], "language": row["language"],
            "refine": refine.marks(row["asset_id"]) if row["status"] == "klaar" else None,
            "elapsed": (end - row["started"]).total_seconds() if row["started"] else None,
            "auto": row["auto"],
        })
    account, _ = iconik_account()
    # Alleen wat het opdrachtenvenster gebruikt; het venster Instellingen haalt zijn gegevens bij /settings-data.
    return {
        "jobs": out, "account": account, "actions": actions.get("state", "onbekend"), "lang": transcribe.app_language(),
        "update": update_view(), "mounts": mount_list(setting.get("iconik.mounts")),
        "worker": dict(remote.state, enabled=worker_enabled(), machine=remote.machine_name(),
                       machine_alias=remote.machine_alias[0], computer=remote.computer_name(),
                       login=login_item_on(), login_available=bundle_path() is not None,
                       iconik_language=worker_languages()[1], auto_on=setting.get("iconik.auto_transcribe"),
                       auto_audio=setting.get("iconik.auto_audio"), awake=awake["on"]),
    }


def clear_finished():
    jobs.clear_finished()
    save_jobs()


def remove_jobs(number, everything=False):
    """Eén regel uit de lijst halen (✕, ⌫ of rechtsklik). Met everything ook de andere afgeronde regels van
    hetzelfde asset en alles wat de app daarvan op deze Mac bewaart (refine.forget_asset). Een opdracht die wacht
    of loopt blijft staan. Geeft {"ok", "removed": aantal regels, "files": wat er van de Mac weg is}."""
    gone, asset, error = jobs.remove(number, everything)
    if error:
        return {"ok": False, "error": error}
    save_jobs()
    files = refine.forget_asset(asset) if everything and asset else []
    if files:
        print(f"[{datetime.now():%H:%M:%S}] van deze Mac verwijderd: {len(files)} bestand(en) van één asset")
    return {"ok": True, "removed": len(gone), "files": files}


def add_failed(title, message):
    """Een rode regel in de lijst zonder opdracht erachter, bijvoorbeeld voor een collectie die niet
    uit te pakken was. Zo ziet de gebruiker het ook als de klik in Iconik allang is afgehandeld."""
    jobs.add_failed(title, message)
    touch_jobs()


def add_job(asset_id, remote=0, auto=False):
    """`remote`: de opdracht komt uit de rij in Iconik (werker-modus), het resultaat gaat daar terug.
    Het is het nummer van de poging (1, 2 of 3); 0 betekent een gewone opdracht.
    `auto`: automatisch opgepakt door auto transcribe, niet aangevraagd (alleen voor de weergave)."""
    job = jobs.add(asset_id, remote=remote, auto=auto)
    if job is None:
        return False
    touch_jobs()
    work.put(job)
    if remote:   # opdrachten uit de rij in Iconik hebben nog geen titel: ophalen, anders staat er een asset-ID
        threading.Thread(target=fill_titles, args=([job],), daemon=True).start()
    return True


def fill_titles(new_jobs):
    """Haalt op de achtergrond de titels op, zodat de rij leesbaar is."""
    for job in new_jobs:
        if job.title:
            continue
        try:
            title = client().get(f"assets/v1/assets/{job.asset_id}/").get("title")
            with jobs_lock:
                if title and not job.title:
                    job.title = title
        except Exception:
            pass  # de titel is alleen voor de weergave


def collection_assets(iconik, collection_id, depth=0):
    """Alle asset-ID's in een collectie, inclusief onderliggende collecties."""
    found, page = [], 1
    while True:
        objects = iconik.get(
            f"assets/v1/collections/{collection_id}/contents/?per_page={PAGE_SIZE}&page={page}"
        ).get("objects", [])
        for item in objects:
            kind = item.get("object_type") or item.get("type")
            if kind == "assets":
                found.append(item["id"])
            elif kind == "collections" and depth < MAX_DEPTH:
                found += collection_assets(iconik, item["id"], depth + 1)
        if len(objects) < PAGE_SIZE:
            return found
        page += 1


def queue_request(requests_waiting):
    """Zet opdrachten in de rij; `requests_waiting` is een lijst (asset_ids, collection_ids).
    Losse assets meteen. Collecties worden in een eigen draad uitgepakt: een grote collectie kost
    vele pagina's bij Iconik, en binnen het verzoek liep dat tegen de 60 seconden van de klik aan."""
    asset_ids, collection_ids = [], []
    for assets, collections in requests_waiting:
        asset_ids += assets
        collection_ids += collections
    if asset_ids:
        enqueue(asset_ids)
    if collection_ids:
        threading.Thread(target=unpack_collections, args=(collection_ids,), daemon=True, name="collecties").start()


def unpack_collections(collection_ids):
    """Pakt collecties uit en zet de assets in de rij. Lukt dat niet, dan een rode regel in de lijst."""
    for collection_id in collection_ids:
        try:
            connection = client()
            if connection is None:
                raise Problem(transcribe.L("Niet ingelogd bij Iconik.", "Not logged in to Iconik."))
            found = collection_assets(connection, collection_id)
        except Exception as error:
            name = collection_id
            try:
                name = client().get(f"assets/v1/collections/{collection_id}/").get("title") or collection_id
            except Exception:
                pass
            print(f"Collectie {collection_id} uitpakken mislukte: {error}")
            add_failed(transcribe.L(f"Collectie {name}", f"Collection {name}"),
                       transcribe.L(f"Uitpakken mislukte: {error}", f"Could not unpack: {error}")[:300])
            continue
        if found:
            enqueue(found)
        else:
            print(f"Collectie {collection_id} bevat geen assets.")


def split_ids(values):
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def clock(seconds):
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def running(job):
    """Tekst voor een opdracht die bezig is: hoe lang al, en hoe lang de audio is."""
    if job.status != "bezig" or not job.started:
        return ""
    text = f"{clock((datetime.now() - job.started).total_seconds())} bezig"
    if job.duration:
        text += f", audio duurt {clock(job.duration)}"
    return text


def status_page(note=""):
    """Het venster: een vaste pagina die zelf de gegevens ophaalt bij /state."""
    return page_text(UI_FILE).replace('"__NOTE__"', json.dumps(note))


def settings_page():
    """Het venster Instellingen, in de taal van de app."""
    return page_text(SETTINGS_FILE).replace('"__LANG__"', json.dumps(transcribe.app_language()))


# Routes die iets veranderen: alleen als POST met de header X-Local-Transcribe, die alleen het venster
# meestuurt (en de sessie, zoals alles). Een link, een plaatje of een formulier op een andere pagina kan zo'n
# verzoek niet maken. De waarden staan nog in de querystring, zoals voorheen.
def set_labels_language(code):
    """De labels van de rij in Iconik in deze taal. Gelden voor iedereen in die Iconik-omgeving; daarom eerst
    in Iconik en pas daarna bewaren. Een fout gaat als Problem of andere fout naar boven."""
    connection = client()
    if connection is None:
        raise Problem(transcribe.L("Niet ingelogd bij Iconik.", "Not logged in to Iconik."))
    queue_in_iconik = remote.Queue(connection)
    queue_in_iconik.setup(create=True, language=code)
    queue_in_iconik.apply_language(code)


def _after_host(old, new):
    if old != new:   # een andere Iconik-omgeving: de inlog hoort bij de oude
        transcribe.clear_login()
        who.clear()
        actions.clear()


def _wake_worker(old, new):
    remote.resume()
    remote.wake.set()


def _wake_auto(old, new):
    storage_wake.set()  # meteen kijken of de opslag er is
    remote.wake.set()   # meteen kijken, en de meter begint te meten


# Wat er na het bewaren moet gebeuren: functie(oude waarde, nieuwe waarde).
AFTER_SAVE = {
    "iconik.host": _after_host,
    "iconik.worker_enabled": _wake_worker,
    "iconik.machine_name": lambda old, new: remote.set_machine_alias(new),
    "iconik.auto_transcribe": _wake_auto,
    "iconik.auto_audio": _wake_auto,
}


def reset_settings(keys):
    """Zet instellingen terug op hun standaardwaarde. Alleen gewone instellingen; mounts en ondertitels hebben
    hun eigen weg. Daarna dezelfde stappen als na bewaren."""
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
        return {"ok": False, "error": transcribe.L("Niets om terug te zetten.", "Nothing to reset.")}
    unknown = [k for k in keys if k not in setting.SCHEMA or setting.SCHEMA[k].kind == "mapping"]
    if unknown:
        return {"ok": False, "error": transcribe.L("Onbekende instelling.", "Unknown setting."), "keys": unknown}
    old = {key: setting.get(key) for key in keys}
    setting.reset(keys)
    for key in keys:
        if key in AFTER_SAVE and old[key] != setting.get(key):
            AFTER_SAVE[key](old[key], setting.get(key))
    return {"ok": True, "values": {key: setting.get(key) for key in keys}}


def _general_extra():
    return {"login": {"on": login_item_on(), "available": bundle_path() is not None}}


def _transcription_extra():
    local = (transcribe.LOCAL_SPEAKER_MODEL / "config.yaml").is_file()
    return {"model": settings.get("model", ""), "version": transcribe.APP_VERSION,
            "speakers": "lokaal" if local else "huggingface" if settings.get("hf_token") else "uit",
            "originals": refine.originals_info()}


# Wat het venster Instellingen per tab ophaalt: het schema (ook wat geavanceerd is), de waarden, wat iemand
# zelf heeft ingesteld en wat er verder op die tab staat maar geen instelling is.
icons = {}   # host van de Iconik-omgeving -> (adres van het icoon of "", wanneer gezocht)


def find_icon(host):
    """Op de achtergrond: zoekt op de startpagina van de Iconik-omgeving het apple-touch-icon (scherper) of anders
    het favicon, voor de lijst bij Integraties. Alleen een adres op dezelfde host telt; anders blijft het de stekker.
    Er komt zo geen logo van Iconik in de app zelf."""
    found = ""
    try:
        page = requests.get(host + "/", timeout=8).text[:200000]
        links = re.findall(r"<link\b[^>]*>", page, re.I)
        for rel in ("apple-touch-icon", "icon", "shortcut icon"):
            for tag in links:
                if re.search(r"""rel\s*=\s*["']?""" + rel + r"""["'\s>]""", tag, re.I):
                    href = re.search(r"""href\s*=\s*["']([^"']+)""", tag, re.I)
                    url = urljoin(host + "/", href.group(1)) if href else ""
                    if url.startswith(host + "/"):
                        found = url
                        break
            if found:
                break
    except Exception as error:
        print(f"[{datetime.now():%H:%M:%S}] icoon van {host} niet gevonden: {type(error).__name__}")
    icons[host] = (found, time.time())


def integration_icon():
    """Het adres van het icoon van de Iconik-omgeving, of None (dan de stekker). Zoekt het één keer op; lukte
    dat niet, dan na tien minuten opnieuw."""
    host = transcribe.api_host()
    url, when = icons.get(host, (None, 0))
    if url is None or (not url and time.time() - when > 600):
        icons[host] = ("", time.time())   # wordt gezocht; niet nog een keer tegelijk
        threading.Thread(target=find_icon, args=(host,), daemon=True).start()
    return icons[host][0] or None


def _iconik_extra():
    account, client_id = iconik_account()
    worker = dict(remote.state, enabled=worker_enabled(), machine=remote.machine_name(),
                  machine_alias=remote.machine_alias[0], computer=remote.computer_name(), awake=awake["on"])
    return {"instances": setting.integrations(), "account": account, "user": who.get("name", ""),
            "has_client_id": bool(client_id), "actions": actions.get("state", "onbekend"),
            "mounts": mount_list(setting.get("iconik.mounts")), "worker": worker, "icon": integration_icon()}


SETTINGS_DATA = {"general": _general_extra, "transcription": _transcription_extra, "iconik": _iconik_extra}


def remove_integration(instance_id):
    """Een koppeling verwijderen (de knop − bij Integraties): uitloggen en de instellingen van die koppeling
    vergeten. Niet tijdens een opdracht. Transcripten in Iconik blijven staan."""
    known = {item["id"] for item in setting.integrations()}
    if instance_id not in known:
        return {"ok": False, "error": transcribe.L("Deze integratie bestaat niet (meer).", "This integration does not exist (any more).")}
    if busy():
        return {"ok": False, "error": transcribe.L("Wacht tot de lopende opdracht klaar is.", "Wait until the running job has finished.")}
    transcribe.clear_login()
    who.clear()
    actions.clear()
    setting.remove_integration(instance_id)
    _wake_worker(True, False)   # de werker ziet dat opdrachten op afstand nu uit staan
    print(f"[{datetime.now():%H:%M:%S}] integratie verwijderd: {instance_id}")
    return {"ok": True}


def settings_data(group):
    return {"settings": setting.describe(group, advanced=True), "values": setting.values(group),
            "custom": setting.custom(group), "extra": SETTINGS_DATA[group]()}


def apply_settings(changes):
    """Eén ingang voor elke wijziging uit het venster: {sleutel: waarde}. Alles wordt eerst gecontroleerd
    tegen het schema; mag één waarde niet, dan verandert er niets. Geeft {"ok": True, "values": ...} of
    {"ok": False, "errors": {sleutel: melding}, "error": de eerste melding}."""
    if not isinstance(changes, dict) or not changes:
        return {"ok": False, "errors": {}, "error": transcribe.L("Niets om te bewaren.", "Nothing to save.")}
    errors, cleaned = {}, {}
    for key, value in changes.items():
        spec = setting.SCHEMA.get(key) if isinstance(key, str) else None
        if spec is None:
            errors[str(key)] = transcribe.L("Onbekende instelling.", "Unknown setting.")
            continue
        if spec.kind == "mapping":   # mounts en ondertitels hebben hun eigen weg (mappenkiezer, Refine)
            errors[key] = transcribe.L("Deze instelling verandert elders.", "This setting is changed elsewhere.")
            continue
        if key == "iconik.machine_name" and isinstance(value, str):
            value = remote.clean_alias(value)   # zoals altijd: leesbare tekens, niet te lang
        try:
            cleaned[key] = setting.clean(key, value)
        except setting.SettingError as error:
            errors[key] = error.message()
    if "iconik.machine_name" in cleaned and busy():
        # Niet tijdens een opdracht: het asset staat dan onder de oude naam op "bezig", en de werker herkent
        # zijn eigen werk aan die naam.
        errors["iconik.machine_name"] = transcribe.L("Wacht tot de lopende opdracht klaar is.",
                                                     "Wait until the running job has finished.")
    if not errors and cleaned.get("iconik.iconik_language") in ("nl", "en"):
        try:
            set_labels_language(cleaned["iconik.iconik_language"])
        except Exception as error:
            errors["iconik.iconik_language"] = str(error)[:300]
    if errors:
        return {"ok": False, "errors": errors, "error": next(iter(errors.values()))}
    old = {key: setting.get(key) for key in cleaned}
    setting.put_many(cleaned)
    for key, value in cleaned.items():
        if key in AFTER_SAVE:
            AFTER_SAVE[key](old[key], value)
    return {"ok": True, "values": cleaned}


# --- de routes -------------------------------------------------------------------------------------------------
#
# Elke route staat één keer in ROUTES, met wie er bij mag. Handler hieronder doet de controles voor alle routes
# hetzelfde; een route hoeft dat dus niet zelf te onthouden.
#
#   session    het verzoek moet van het venster komen (cookie) of van de app zelf (sleutel). Uit voor wat van
#              buiten komt: de knop in Iconik (heeft de sleutel in het pad), de terugkeer van het inloggen (heeft
#              zijn eigen state), het venster dat zich meldt (heeft SESSION) en een tweede start van de app.
#   protected  verandert iets: alleen als POST en met de header X-Local-Transcribe, die een andere webpagina niet
#              naar localhost kan meesturen. Een GET op zo'n route krijgt 405.
#   kind       window: het venster van de app; link: van buiten (Iconik, inloggen); app: een tweede start van
#              de app. API_PREFIX is gereserveerd voor een gedocumenteerde, versioneerde API (nog leeg).
#   errors     page: een fout wordt een pagina met de melding; json: {"error": ...}.

API_PREFIX = "/api/v1/"
KINDS = ("window", "link", "app", "api")


class Route:
    __slots__ = ("path", "methods", "handler", "session", "protected", "kind", "errors")

    def __init__(self, path, handler, methods, session, protected, kind, errors):
        self.path, self.handler, self.methods = path, handler, tuple(methods)
        self.session, self.protected, self.kind, self.errors = session, protected, kind, errors


ROUTES = {}
PREFIXES = {}     # routes waar na het pad nog iets komt (/transcribe/<sleutel>)


def route(path, methods=None, session=True, protected=False, kind="window", errors="page", prefix=False):
    """Legt een route vast. Zonder methods: POST als hij beschermd is, anders GET."""
    if kind not in KINDS or (kind == "api") != path.startswith(API_PREFIX):
        raise ValueError(f"route {path}: soort {kind}")

    def add(handler):
        table = PREFIXES if prefix else ROUTES
        if path in table:
            raise ValueError(f"route {path} staat er al")
        table[path] = Route(path, handler, methods or (("POST",) if protected else ("GET",)),
                            session, protected, kind, errors)
        return handler
    return add


def find_route(path):
    found = ROUTES.get(path)
    if found is None:
        found = next((r for p, r in PREFIXES.items() if path.startswith(p)), None)
    return found


def protected_paths():
    """De paden die iets veranderen (wat vroeger PROTECTED was)."""
    return {p for p, r in ROUTES.items() if r.protected}


class Request:
    """Wat een route nodig heeft van een verzoek: de query, de inhoud en de antwoorden van Handler."""

    def __init__(self, handler, url):
        self.handler, self.url, self.path = handler, url, url.path
        self.query = parse_qs(url.query)
        self.size = int(handler.headers.get("Content-Length") or 0)
        self._read = False

    def first(self, name):
        return self.query.get(name, [""])[0]

    def body(self):
        self._read = True
        return self.handler.rfile.read(self.size) if self.size else b""

    def drain(self):
        """Wat het venster toch meestuurt bij een route zonder inhoud: weg (hooguit 64 kB)."""
        if self.size and not self._read:
            self._read = True
            self.handler.rfile.read(min(self.size, 65536))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, page):
        body = page.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reply_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reply_bytes(self, body, kind):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def host_ok(self):
        """Alleen verzoeken aan deze Mac zelf. Een webpagina die haar domeinnaam naar 127.0.0.1 laat
        wijzen (DNS-rebinding) stuurt haar eigen naam mee en komt er zo niet in."""
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"localhost:{port}", f"127.0.0.1:{port}")

    def session_ok(self):
        """Komt dit verzoek van het venster (cookie) of van de app zelf (sleutel)? Zie SESSION bovenaan."""
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and secrets.compare_digest(value.encode(), SESSION.encode()):
                return True
        given, key = self.headers.get(KEY_HEADER) or "", settings.get("key") or ""
        return bool(given and key) and secrets.compare_digest(given.encode(), key.encode())

    def header_ok(self):
        return self.headers.get("X-Local-Transcribe") == "1"

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        """Dezelfde controles voor elke route, in deze volgorde: deze Mac, (bij POST) de header, de sessie,
        de bescherming, de methode."""
        url = urlparse(self.path)
        if not self.host_ok() or (method == "POST" and not self.header_ok()):
            return self.send_error(403)
        found = find_route(url.path)
        known = found is not None and method in found.methods   # een methode die de route niet kent: als onbekend
        if (not known or found.session) and not self.session_ok():
            return self.send_error(403)
        if found is not None and found.protected and not self.header_ok():
            return self.send_error(403)
        if not known:
            if method == "GET" and found is not None and found.protected:
                self.send_response(405)
                self.send_header("Allow", "POST")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if method == "POST":
                return self.send_error(404)
            return self.reply(404, "Niet gevonden.")
        request = Request(self, url)
        try:
            return found.handler(self, request)
        except Exception as error:
            return self.failed(found, url, error)
        finally:
            request.drain()

    def failed(self, found, url, error):
        if found.errors == "json":
            if isinstance(error, (refine.RefineError, Problem, ValueError, KeyError, TypeError)):
                return self.reply_json({"error": str(error) or type(error).__name__})
        elif isinstance(error, Problem):
            return self.reply(200, status_page(str(error)))
        # Geen netwerk of een onverwachte fout: een pagina met de fout, geen verbroken verbinding.
        print(f"Fout bij {url.path}: {type(error).__name__}: {error}")
        try:
            return self.reply(500, status_page(f"{type(error).__name__}: {error}"))
        except Exception:
            pass

    def finish_login(self, code, state, error):
        verifier = pending_login.pop(state, None)
        back = pending_back.pop(state, None)
        if back and (error or not verifier or not code):
            return self.redirect(back + "&login=failed")
        if error:
            return self.reply(200, status_page(transcribe.L(f"Iconik gaf geen toegang ({error}).", f"Iconik denied access ({error}).")))
        if not verifier or not code:
            return self.reply(200, status_page(transcribe.L("Deze inlog is verlopen of hoort niet bij deze app. Probeer opnieuw.",
                                                            "This login has expired or does not belong to this app. Try again.")))
        transcribe.finish_login(code, verifier)
        who.clear()
        actions.clear()
        print("Ingelogd bij Iconik.")
        waiting, pending_ids[:] = list(pending_ids), []
        if waiting:
            queue_request(waiting)
        return self.redirect(back or "/")


def _ok(handler):
    return handler.reply_json({"ok": True})


def _json_body(handler, request, limit, minimum=0):
    """De inhoud als JSON, of None als die te groot is (dan is er al 413 gestuurd)."""
    if not minimum <= request.size <= limit:
        handler.send_error(413)
        return None
    return json.loads(request.body())


# --- het venster: pagina's en gegevens

@route("/")
def _page_jobs(h, r):
    return h.reply(200, status_page())


@route("/state")
def _state(h, r):
    activity["poll"] = time.time()
    return h.reply_json(app_state())


@route("/settings-window")
def _page_settings(h, r):
    return h.reply(200, settings_page())


@route("/settings-data")
def _settings_data(h, r):
    group = r.first("group")
    if group not in SETTINGS_DATA:
        return h.send_error(404)
    return h.reply_json(settings_data(group))


@route("/window/open", session=False)
def _window_open(h, r):
    """Het venster van de app meldt zich met SESSION en krijgt de cookie. Daarna gaat het naar de app,
    of naar de pagina in next (alleen een eigen pad, nooit een ander adres)."""
    token, target = r.first("t"), r.first("next")
    if not secrets.compare_digest((token or "").encode(), SESSION.encode()):
        return h.send_error(403)
    if not target.startswith("/") or target.startswith("//") or "\\" in target or any(ord(c) < 32 for c in target):
        target = "/"
    h.send_response(303)
    h.send_header("Location", target)
    # Lax: de cookie gaat mee als Iconik na het inloggen terugstuurt naar /oauth/callback en door naar /.
    h.send_header("Set-Cookie", f"{SESSION_COOKIE}={SESSION}; Path=/; HttpOnly; SameSite=Lax")
    h.send_header("Cache-Control", "no-store")
    h.end_headers()


# --- een tweede start van de app: kijken of er al een exemplaar draait en het venster naar voren halen.
# Open zonder sessie voor een oudere versie van de app die de sleutel nog niet meestuurt; ze geven niets prijs.

@route("/window/state", session=False, kind="app")
def _window_state(h, r):
    # Zonder sessie alleen dat de app er is, niet welke pagina openstaat.
    return h.reply_json(window_state if h.session_ok() else {"ok": True})


@route("/window/show", methods=("GET", "POST"), session=False, protected=True, kind="app")
def _window_show(h, r):
    # De app vraagt het venster een pagina te tonen en naar voren te komen. Ook nog als GET (oudere versies).
    page = r.first("page")
    refine_page = re.fullmatch(r"/refine\?asset=[0-9a-fA-F-]{8,64}", page or "")
    window_state["page"] = page if page == "/login" or refine_page else "/"
    window_state["n"] += 1
    if show_hook:
        show_hook(window_state["page"])
    return h.reply_json(window_state)


# --- het venster: wat iets verandert

@route("/settings", protected=True)
def _settings(h, r):
    # {sleutel: waarde, ...} als JSON, alles of niets.
    if not r.size:
        return h.reply(404, "Niet gevonden.")
    if r.size > 16384:
        return h.send_error(413)
    try:
        changes = json.loads(r.body())
    except ValueError:
        return h.reply_json({"ok": False, "errors": {}, "error": "json"})
    return h.reply_json(apply_settings(changes))


@route("/settings/reset", protected=True)
def _settings_reset(h, r):
    # {"keys": [...]}: terug naar de standaardwaarde ("Terug naar aanbevolen" in Instellingen).
    try:
        data = _json_body(h, r, 4096, minimum=1)
        if data is None:
            return None
        keys = data.get("keys")
    except (ValueError, AttributeError):
        keys = None
    return h.reply_json(reset_settings(keys))


@route("/window/title", protected=True)
def _window_title(h, r):
    # {"title": tekst}: de titel van het venster Instellingen volgt de tab.
    try:
        data = _json_body(h, r, 1024, minimum=1)
        if data is None:
            return None
        title = data.get("title")
    except (ValueError, AttributeError):
        title = None
    if not isinstance(title, str) or not title.strip():
        return h.reply_json({"ok": False})
    if title_hook:
        title_hook(" ".join(title.split())[:60])
    return _ok(h)


@route("/window/settings", protected=True)
def _window_settings(h, r):
    # Het venster Instellingen openen, eventueel op een tab.
    tab = r.first("tab")
    if settings_hook:
        settings_hook(tab if tab in SETTINGS_TABS else None)
    return _ok(h)


@route("/mount/change", protected=True)
def _mount_change(h, r):
    change_mount(r.first("id"))
    return _ok(h)


@route("/mount/remove", protected=True)
def _mount_remove(h, r):
    change_mount(r.first("id"), remove=True)
    return _ok(h)


@route("/update/install", protected=True)
def _update_install(h, r):
    install_update()
    return _ok(h)


@route("/update/open", protected=True)
def _update_open(h, r):
    if update.get("url"):
        system.open_url(update["url"])  # in de gewone browser, niet in het venster
    return _ok(h)


@route("/worker", protected=True)
def _worker(h, r):   # alleen "Nu hervatten"; aan en uit gaat via POST /settings
    if r.first("value") == "resume":
        remote.resume()
        remote.wake.set()
    return _ok(h)


@route("/login-item", protected=True)
def _login_item(h, r):   # opstarten bij inloggen op de Mac; /login is inloggen bij Iconik
    if r.first("value") in ("on", "off"):
        set_login_item(r.first("value") == "on")
    return _ok(h)


@route("/asset/open", protected=True)
def _asset_open(h, r):
    # Het asset in Iconik, in de gewone browser. Alleen een asset-ID: geen willekeurige adressen openen.
    asset_id = r.first("id")
    if re.fullmatch(r"[0-9a-fA-F-]{8,64}", asset_id or ""):
        system.open_url(f"{transcribe.api_host()}/asset/{asset_id}")
    return _ok(h)


@route("/actions/install", protected=True)
def _actions_install(h, r):
    install_actions()
    return _ok(h)


@route("/log/clear", protected=True)
def _log_clear(h, r):
    return h.reply_json({"ok": bool(clear_log_hook and clear_log_hook())})


@route("/jobs/remove", protected=True)
def _jobs_remove(h, r):
    try:
        number = int(r.first("n"))
    except ValueError:
        return h.reply_json({"ok": False, "error": "n"})
    return h.reply_json(remove_jobs(number, everything=r.first("all") == "1"))


@route("/integrations/remove", protected=True)
def _integrations_remove(h, r):
    return h.reply_json(remove_integration(r.first("id")))


@route("/clear", protected=True)
def _clear(h, r):
    clear_finished()
    return _ok(h)


@route("/logout", protected=True)
def _logout(h, r):
    transcribe.clear_login()
    who.clear()
    actions.clear()
    return _ok(h)


# --- inloggen bij Iconik

@route("/login")
def _login(h, r):
    address, state, verifier = transcribe.start_login()
    pending_login.clear()
    pending_back.clear()
    pending_login[state] = verifier
    if r.first("back") == "settings":   # ingelogd vanuit Instellingen: daarna daar terug, niet naar /
        pending_back[state] = "/settings-window?tab=integrations"
    return h.redirect(address)


@route("/oauth/callback", session=False, kind="link")
def _oauth_callback(h, r):
    return h.finish_login(r.first("code"), r.first("state"), r.first("error"))


# --- de knop in Iconik: /transcribe/<sleutel>?asset_ids=...&collection_ids=...

def _transcribe(h, r):
    parts = r.path.strip("/").split("/")
    if parts[0] != "transcribe" or len(parts) > 2:
        return h.reply(404, "Niet gevonden.")
    given = parts[1] if len(parts) == 2 else r.first("key")
    if given != settings["key"]:
        print("Geweigerd: verzoek zonder geldige sleutel.")
        return h.reply(403, "Geen geldige sleutel.")
    request = (split_ids(r.query.get("asset_ids", [])), split_ids(r.query.get("collection_ids", [])))
    if client() is None:
        # Eerst inloggen; de opdracht gaat daarna alsnog de rij in.
        pending_ids.append(request)
        if setting.get("iconik.client_id"):
            return h.redirect("/login")
        return h.redirect("/")
    queue_request([request])
    # Doorsturen naar de statuspagina, zodat de sleutel uit de adresbalk verdwijnt
    # en verversen de opdracht niet opnieuw instuurt.
    return h.redirect("/")


route("/transcribe", session=False, kind="link")(_transcribe)
route("/transcribe/", session=False, kind="link", prefix=True)(_transcribe)


# --- Refine

@route("/refine")
def _page_refine(h, r):
    return h.reply(200, page_text(REFINE_FILE))


@route("/refine/originals")
def _refine_originals(h, r):   # de lijst in Instellingen
    return h.reply_json(refine.originals_list())


@route("/refine/originals/clear", protected=True, errors="json")
def _refine_originals_clear(h, r):
    refine.clear_originals()
    return _ok(h)


def refine_route(path, methods=("GET",), limit=0):
    """Een route van Refine voor één asset (?asset=...). Een ongeldig asset geeft {"error": "asset"};
    `limit`: de grootste inhoud die de route aanneemt (413 daarboven)."""
    def add(handler):
        def run(h, r):
            asset_id = r.first("asset")
            if not valid_asset_id(asset_id):
                return h.reply_json({"error": "asset"})
            if r.size > limit:
                return h.send_error(413)
            return handler(h, r, asset_id)
        route(path, methods=methods, protected="POST" in methods, errors="json")(run)
        return handler
    return add


@refine_route("/refine/view")
def _refine_view(h, r, asset_id):
    return h.reply_json(refine.view(asset_id))


@refine_route("/refine/chunk")
def _refine_chunk(h, r, asset_id):
    body = refine.chunk(asset_id, r.first("t") or 0, r.first("d") or refine.CHUNK_SECONDS, int(r.first("solo") or 0))
    return h.reply_bytes(body, "video/mp4")


@refine_route("/refine/waves")
def _refine_waves(h, r, asset_id):
    body, fraction = refine.waves(asset_id)
    if body is None:
        return h.reply_json({"fraction": fraction})
    return h.reply_bytes(body, "application/octet-stream")


@refine_route("/refine/originals/remove", methods=("POST",), limit=65536)
def _refine_original_remove(h, r, asset_id):   # één origineel, uit de lijst in Instellingen
    refine.remove_original(asset_id)
    return _ok(h)


@refine_route("/refine/draft", methods=("POST",), limit=refine.DRAFT_LIMIT)
def _refine_draft(h, r, asset_id):
    if not r.size:
        return h.send_error(413)
    return h.reply_json({"saved_at": refine.save_draft(asset_id, r.body())})


@refine_route("/refine/discard", methods=("POST",), limit=65536)
def _refine_discard(h, r, asset_id):
    refine.discard_draft(asset_id)
    return _ok(h)


@refine_route("/refine/save", methods=("POST",), limit=65536)
def _refine_save(h, r, asset_id):
    return h.reply_json(refine.save_to_iconik(asset_id))


@refine_route("/refine/subtitles/preview", methods=("POST",), limit=4096)
def _subtitles_preview(h, r, asset_id):
    return h.reply_json(refine.preview_subtitles(asset_id, r.body()))


@refine_route("/refine/subtitles/make", methods=("POST",), limit=4096)
def _subtitles_make(h, r, asset_id):   # ook het bewaarvenster, als iemand een andere plek kiest
    return h.reply_json(refine.make_subtitles(asset_id, r.body(), save_hook))


@refine_route("/refine/subtitles/import", methods=("POST",), limit=4096)
def _subtitles_import(h, r, asset_id):   # een bestaand .srt of .vtt inladen
    return h.reply_json(refine.import_subtitles(asset_id, r.body(), open_hook))


@refine_route("/refine/subtitles/draft", methods=("POST",), limit=refine.DRAFT_LIMIT)
def _subtitles_draft(h, r, asset_id):
    return h.reply_json({"saved_at": refine.save_subtitles(asset_id, r.body())})


@refine_route("/refine/subtitles/write", methods=("POST",), limit=4096)
def _subtitles_write(h, r, asset_id):
    return h.reply_json(refine.write_subtitles(asset_id))


@refine_route("/refine/subtitles/write-as", methods=("POST",), limit=4096)
def _subtitles_write_as(h, r, asset_id):
    return h.reply_json(refine.write_subtitles(asset_id, save_hook, elsewhere=True))


@refine_route("/refine/subtitles/fit", methods=("POST",), limit=4096)
def _subtitles_fit(h, r, asset_id):
    return h.reply_json(refine.fit_subtitles(asset_id, r.body()))


@refine_route("/refine/subtitles/discard", methods=("POST",), limit=4096)
def _subtitles_discard(h, r, asset_id):
    refine.discard_subtitles(asset_id)
    return _ok(h)


def busy():
    """Loopt er een opdracht of staat er nog iets in de rij?"""
    return current[0] is not None or not work.empty()


def report_remote(job):
    """Zet het resultaat van een opdracht uit de rij terug in Iconik. Drie pogingen,
    want blijft de status op "bezig" staan, dan gaat het asset na een paar uur opnieuw de rij in."""
    for attempt in range(3):
        try:
            connection = client()
            if connection is None:
                raise Problem("Niet ingelogd bij Iconik.")
            queue_in_iconik = remote.Queue(connection)
            queue_in_iconik.setup()
            outcome = remote.report(queue_in_iconik, job)
            if outcome == "requeued":
                # Het ligt niet aan het asset: het staat weer in de rij en een andere Mac pakt het op.
                with jobs_lock:
                    job.status = "teruggezet"
                    job.message = ((job.message or "").rstrip() + " "
                                   + transcribe.L("Terug in de rij voor een andere Mac.", "Back in the queue for another Mac.")).strip()
                save_jobs()
            return
        except Exception as error:
            print(f"Resultaat terugmelden mislukte ({attempt + 1}/3): {error}")
            time.sleep(5)


def say(text):
    """Een logregel in één schrijfactie. print() schrijft tekst en regeleinde apart, en dan kwam de
    regel van een andere draad er soms tussen: "===== asset =====Werker: asset ... aangenomen"."""
    sys.stdout.write(f"{text}\n")
    sys.stdout.flush()


def worker_enabled():
    return setting.get("iconik.worker_enabled")


meter = resources.Meter(lambda: busy())   # meet pas zodra auto transcribe aan staat
awake = {"on": False, "process": None}


def auto_status_text():
    """Korte regel voor de menubalk over auto transcribe, of None als het uit staat."""
    a = remote.state.get("auto")
    if not a:
        return None
    offline = ", ".join(a.get("offline") or [])
    if a.get("ready"):
        if offline:
            return transcribe.L(f"Transcribeert automatisch, niet bereikbaar: {offline}",
                                f"Transcribing automatically, not reachable: {offline}")
        return transcribe.L("Wacht op opdrachten, transcribeert automatisch", "Waiting for jobs, transcribing automatically")
    v = a.get("values") or {}
    def n(key):
        return "?" if v.get(key) is None else f"{v[key]:.0f}"
    why = {"power": transcribe.L("op batterij", "on battery"),
           "memory": transcribe.L(f"te weinig geheugen vrij ({n('free_gb')} GB)", f"not enough memory free ({n('free_gb')} GB)"),
           "pressure": transcribe.L("geheugendruk hoog", "memory pressure high"),
           "measuring": transcribe.L("meet nog hoe druk de Mac is", "still measuring how busy the Mac is"),
           "cpu": transcribe.L(f"CPU {n('cpu')}% bezet", f"CPU {n('cpu')}% in use"),
           "gpu": transcribe.L(f"GPU {n('gpu')}% bezet", f"GPU {n('gpu')}% in use"),
           "storage": transcribe.L("geen opslag (ISG) gekoppeld", "no storage (ISG) connected"),
           "checking": transcribe.L("kijkt of de opslag (ISG) bereikbaar is", "checking whether the storage (ISG) is reachable"),
           "offline": transcribe.L(f"opslag (ISG) niet bereikbaar ({offline})", f"storage (ISG) not reachable ({offline})")}.get(a.get("reason"), a.get("reason") or "")
    return transcribe.L(f"Automatisch: wacht, {why}", f"Automatic: waiting, {why}")


quit_after = threading.Event()   # Cmd+Q met "Na deze opdracht": niets nieuws oppakken, daarna afsluiten
quit_asking = threading.Event()  # de vraag bij Cmd+Q staat open: ook dan niets nieuws, de keuze komt zo
quit_hook = None                 # het venster zet hier de functie die de app afsluit


def request_quit():
    """De lopende opdracht is klaar en de gebruiker wilde daarna afsluiten."""
    print(f"[{datetime.now():%H:%M:%S}] opdracht klaar, de app sluit af zoals gevraagd")
    if quit_hook is not None:
        quit_hook()


def worker_may_take():
    """De werker mag opdrachten uit de rij (en automatische) oppakken: aan, en niet aan het afsluiten.
    Ook niet zolang de vraag bij Cmd+Q openstaat: anders werd de lopende opdracht klaar terwijl de vraag
    er stond, en claimde de werker alvast de volgende (gezien 23 oktober)."""
    return worker_enabled() and not quit_after.is_set() and not quit_asking.is_set()


RELEASE_AFTER = 120   # met auto aan: modellen pas vrijgeven na zoveel seconden zonder opdracht


def auto_active():
    return setting.get("iconik.worker_enabled") and setting.get("iconik.auto_transcribe")


def release_models_now():
    try:
        transcribe.release_models()
        transcribe.memory_report("vrijgeven")
    except Exception as error:
        print(f"Modellen vrijgeven mislukte: {error}")


def release_later(done_at, wait=RELEASE_AFTER, sleep=time.sleep):
    """Geeft de modellen vrij als er sinds `done_at` geen opdracht meer is geweest. Is er intussen een
    opdracht geweest (of loopt er een), dan doet deze draad niets: die opdracht regelt het zelf."""
    sleep(wait)
    if activity["done"] == done_at and not busy():
        release_models_now()


def want_awake():
    """Systeemslaap tegenhouden tijdens elke opdracht, en met auto transcribe aan op netstroom ook
    tussen opdrachten: anders valt de Mac na zijn eigen slaaptimer in slaap voordat er ooit ruimte is.
    Het scherm mag wel uit. Op batterij tussen opdrachten niets extra."""
    if busy():
        return True
    if quit_after.is_set():
        return False
    return setting.get("iconik.worker_enabled") and setting.get("iconik.auto_transcribe") and resources.on_mains_power() is True


def keep_awake(want=want_awake, interval=10, once=False):
    """Houdt de computer wakker of laat hem weer slapen (system.keep_awake_command; stopt vanzelf als deze app wegvalt)."""
    while True:
        try:
            on = bool(want())
            process = awake["process"]
            if on and (process is None or process.poll() is not None):
                awake["process"] = subprocess.Popen(system.keep_awake_command(os.getpid()),
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"[{datetime.now():%H:%M:%S}] Computer blijft wakker")
            elif not on and process is not None:
                process.terminate()
                process.wait(timeout=5)
                awake["process"] = None
                print(f"[{datetime.now():%H:%M:%S}] Mac mag weer slapen")
            awake["on"] = awake["process"] is not None
        except Exception as error:   # het commando ontbreekt of iets anders: dan maar niet wakker houden
            awake["on"] = False
            print(f"Wakker houden: {error}")
            if not once:
                time.sleep(300)
        if once:
            return
        time.sleep(interval)


STORAGE_CHECK = 30     # met auto aan: zo vaak kijken of de opslag (ISG) nog gekoppeld is
STORAGE_TIMEOUT = 5    # een netwerkkoppeling die zo lang niet antwoordt telt als weg
STORAGE_RECHECK = 15   # net weg (of opstarten): zo snel nog eens kijken voordat er een waarschuwing komt
storage = {"offline": [], "checked": 0.0, "warned": []}   # storage-ID's; "warned": waarvoor de waarschuwing al kwam
storage_wake = threading.Event()
storage_hook = None    # het venster zet hier een functie(soort, namen): "startup", "lost" of "back"
storage_pending = []   # waarschuwingen van voor het venster er was
_storage_checks = {}   # pad -> draad van een controle die nog hangt


def storage_labels(storage_ids):
    """Namen voor in een melding: de naam van de ISG in Iconik, anders het mount point."""
    mounts = setting.get("iconik.mounts")
    return [storage_names.get(storage_id) or mounts.get(storage_id) or storage_id for storage_id in storage_ids]


def path_reachable(path, timeout=STORAGE_TIMEOUT):
    """mount_reachable met een tijdslimiet. Een stat op een netwerkkoppeling die niet meer antwoordt kan
    minuten hangen; die draad laten we lopen en zolang hij hangt telt het pad als weg (geen stapel draden)."""
    hanging = _storage_checks.get(path)
    if hanging is not None and hanging.is_alive():
        return False
    box = {}

    def look():
        try:
            box["ok"] = transcribe.mount_reachable(path)
        except OSError:
            box["ok"] = False
    thread = threading.Thread(target=look, daemon=True, name="opslag-check")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        _storage_checks[path] = thread
        return False
    _storage_checks.pop(path, None)
    return bool(box.get("ok"))


def warn_storage(kind, storage_ids):
    names = storage_labels(storage_ids)
    say(f"[{datetime.now():%H:%M:%S}] opslag (ISG) " + {"startup": "niet bereikbaar bij opstarten",
        "lost": "verbinding kwijt", "back": "weer bereikbaar"}[kind] + ": " + ", ".join(names))
    if storage_hook is not None:
        storage_hook(kind, names)
    else:
        storage_pending.append((kind, names))


def check_storage(state):
    """Een keer kijken. `state` houdt bij wat de vorige keer weg was en hoeveelste ronde dit is.
    De lijst voor de werker (storage["offline"]) gaat meteen mee: een opslag die weg is, pakt hij niet.
    De waarschuwing pas als hij twee keer achter elkaar weg is: een korte hapering geeft geen melding,
    en bij inloggen koppelt macOS netwerkschijven vaak pas na een paar seconden.
    Geeft het aantal seconden tot de volgende ronde."""
    mounts = setting.get("iconik.mounts")
    offline = [storage_id for storage_id, path in mounts.items() if not path_reachable(path)]
    storage["offline"] = offline
    storage["checked"] = time.time()
    warned = [storage_id for storage_id in storage["warned"] if storage_id in mounts]
    new = [storage_id for storage_id in offline if storage_id in state["suspect"] and storage_id not in warned]
    back = [storage_id for storage_id in warned if storage_id not in offline]
    gone = [storage_id for storage_id in offline if storage_id not in state["suspect"] and storage_id not in warned]
    starting = state["round"] < 2      # opstarten, of auto ging net aan
    state["round"] += 1
    state["suspect"] = offline
    storage["warned"] = [storage_id for storage_id in warned if storage_id not in back] + new
    if gone and storage_hook is not None:
        storage_hook("gone", storage_labels(gone))   # alleen voor het venster: de gele balk staat er nu, nog geen melding
    if new:
        warn_storage("startup" if starting else "lost", new)
    if back:
        warn_storage("back", back)
        remote.wake.set()   # er is weer opslag: de werker kijkt meteen
    if new or back or offline != state.get("shown"):
        # Het venster leest remote.state["auto"], dat de werker anders pas bij zijn volgende ronde bijwerkt
        # (tot een halve minuut later dan de melding): nu meteen.
        try:
            auto_settings()
        except Exception as error:
            print(f"Opslag bewaken (venster bijwerken): {error}")
        state["shown"] = offline
    # Net weg en nog geen melding: snel nog eens kijken, zodat de melding kort na de balk komt.
    pending = [storage_id for storage_id in offline if storage_id not in storage["warned"]]
    return STORAGE_RECHECK if pending else STORAGE_CHECK


def watch_storage():
    """Draait altijd, kijkt alleen met auto transcribe aan."""
    state = {"round": 0, "suspect": []}
    while True:
        wait = STORAGE_CHECK
        try:
            if auto_active():
                wait = check_storage(state)
            else:
                if storage["warned"] and storage_hook is not None:
                    storage_hook("back", [])   # auto staat uit: waarschuwing weghalen
                storage.update(offline=[], checked=0.0, warned=[])
                state.update(round=0, suspect=[], shown=[])
        except Exception as error:
            print(f"Opslag bewaken: {error}")
        storage_wake.wait(wait)
        storage_wake.clear()


def auto_settings():
    """Voor de werker: de instellingen van auto transcribe als die aan staat en er op deze Mac ruimte
    is, anders None. Wat de meter vond staat in remote.state["auto"], voor het venster.
    De grenzen (transcription.auto_min_free_gb, auto_max_cpu, auto_max_gpu en iconik.auto_max_minutes)
    staan in het schema als geavanceerd: bewust niet in het gewone venster."""
    if not setting.get("iconik.auto_transcribe"):
        remote.state["auto"] = None
        return None
    mounts = setting.get("iconik.mounts")
    offline = [storage_id for storage_id in storage["offline"] if storage_id in mounts]
    storages = [storage_id for storage_id in mounts if storage_id not in offline]
    meter.start()
    if not mounts:
        ready, reason, values = False, "storage", {}
    elif not storage["checked"]:
        ready, reason, values = False, "checking", {}   # nog niet gekeken of de opslag er is: niets oppakken
        storage_wake.set()
    elif not storages:
        ready, reason, values = False, "offline", {}
    else:
        limits = {key: setting.get(f"transcription.{key}") for key in resources.DEFAULTS}
        ready, reason, values = meter.check(limits)
    remote.state["auto"] = {"ready": ready, "reason": reason,
                            "values": {k: round(v, 1) if isinstance(v, float) else v for k, v in values.items()}}
    if offline and storage["checked"]:
        remote.state["auto"]["offline"] = storage_labels(offline)
    if not ready:
        return None
    return {"storages": storages, "audio": setting.get("iconik.auto_audio"),
            "max_minutes": setting.get("iconik.auto_max_minutes")}


def worker_languages():
    """(taal van het detailveld, taal van de labels in Iconik). De labels volgen de app tot iemand
    een taal kiest: ze gelden voor iedereen in deze Iconik-omgeving, dus ze wisselen niet vanzelf mee."""
    chosen = setting.get("iconik.iconik_language")
    return transcribe.app_language(), chosen if chosen in ("nl", "en") else transcribe.app_language()


LOGIN_MARKER = transcribe.SUPPORT_DIR / "login-start"  # de LaunchAgent legt dit neer, de app ruimt het op


def bundle_path():
    """De app zelf, alleen als dit de echte app is (niet bij `uv run serve.py`)."""
    res = os.environ.get("LOCALTRANSCRIBE_RES")
    return Path(res).parent.parent if res else None


def login_item_on():
    return system.login_item_on()


def set_login_item(on):
    """Opstarten bij inloggen aan of uit. Gaat in bij de volgende login."""
    system.set_login_item(on, bundle_path(), LOGIN_MARKER)



def stall_limit(job):
    """Hoe lang een opdracht niets mag melden. De sprekerherkenning meldt tussendoor niets en duurt
    bij lange audio langer, dus de grens groeit mee met de lengte."""
    return max(STALL_SECONDS, 0.5 * (job.duration or 0))


def progress_ok():
    """Vordert de lopende opdracht nog? Het eenmalige downloaden van het model beoordelen we niet."""
    job = current[0]
    if job is None or job.stage == "model":
        return True
    return time.monotonic() - last_progress[0] <= stall_limit(job)


def leave(code):
    save_jobs()
    os._exit(code)


def give_up_stalled(job):
    """Een vastgelopen draad is in Python niet te stoppen. De schone weg: de opdracht terugmelden
    (een andere Mac kan hem dan oppakken) en de app herstarten."""
    minutes = max(1, round((time.monotonic() - last_progress[0]) / 60))
    nl, en = ("minuut", "minute") if minutes == 1 else ("minuten", "minutes")
    message = transcribe.L(f"De opdracht meldde {minutes} {nl} niets meer en is gestopt.",
                           f"The job reported nothing for {minutes} {en} and was stopped.")
    print(f"HANGT: {job.asset_id} meldde {minutes} {nl} niets, de app herstart.", flush=True)
    job.set(abandoned=True, status="fout", local=True, message=message, finished=datetime.now())
    save_jobs()
    if job.remote:
        # Vaak is de verbinding juist de reden: niet langer dan 30 seconden wachten op het terugmelden.
        reporter = threading.Thread(target=report_remote, args=(job,), daemon=True)
        reporter.start()
        reporter.join(30)
    current[0] = None
    with work.mutex:
        work.queue.clear()   # anders blijft busy() waar en vraagt afsluiten om een bevestiging
    if restart_hook:
        restart_hook()
        time.sleep(STALL_GRACE)
    leave(0 if restart_hook else 3)


def watchdog():
    """Kijkt elke 30 seconden of de lopende opdracht nog vordert."""
    while True:
        time.sleep(30)
        job = current[0]
        if job is not None and current[0] is job and not progress_ok():
            give_up_stalled(job)


def worker():
    """Een asset tegelijk. Draait in een eigen draad, naast het venster."""
    while True:
        try:
            job = work.get(timeout=30)
        except queue.Empty:
            if settings.get("idle_exit") and not remote.state["enabled"] and time.time() - max(activity["poll"], activity["done"]) > IDLE_EXIT:
                print(f"Geen opdrachten en geen venster sinds {IDLE_EXIT} s, de ontvanger stopt.", flush=True)
                os._exit(0)
            continue
        job.set(status="bezig", started=datetime.now())
        touch_jobs()
        last_progress[0] = time.monotonic()
        current[0] = job

        def on_info(title, seconds, job=job):
            job.set(title=title or "", duration=seconds)
        say(f"\n===== {job.asset_id} =====")
        options = make_options(job)
        prepared = None
        with jobs_lock:
            box, job.prefetch = job.prefetch, None
        if box is not None:
            box["thread"].join()  # nog bezig met lezen: afwachten, niet dubbel lezen
            for line in box["lines"]:
                print(line)
            for kind, data in box["events"]:
                listen(kind, **data)
            prepared = box["result"]
            if prepared is not None:
                print("Audio was al vooruit gelezen.")
        try:
            connection = client()
            if connection is None:
                raise Problem(transcribe.L("Niet ingelogd bij Iconik.", "Not logged in to Iconik."))
            result = transcribe.process_asset(
                connection, options, settings["hf_token"], on_info, prepared)
            job.set(title=result["title"] or "", status="klaar",
                    message=f"{result['segments']} regels, {result['speakers']} spreker(s)",
                    result={"segments": result["segments"], "speakers": result["speakers"]})
        except transcribe.NeedsFolder as folder:
            job.set(status="fout", local=True,
                    message=transcribe.L(f"Het mount point van ISG {folder} is op deze Mac nog niet ingesteld.",
                                         f"The mount point of ISG {folder} is not set on this Mac yet."))
            print(f"FOUT: {job.message}")
        except Problem as problem:
            skipped = getattr(problem, "skip", False)
            job.set(local=getattr(problem, "local", False), status="overgeslagen" if skipped else "fout", message=str(problem))
            print(f"{job.status.upper()}: {problem}")
        except Exception as error:  # de rij moet blijven lopen
            job.set(status="fout", message=f"{type(error).__name__}: {error}")
            print(f"FOUT: {job.message}")
        # Niet in een finally: daar is continue vanaf Python 3.14 een waarschuwing (PEP 765).
        if job.abandoned:
            continue   # de watchdog gaf deze opdracht al op en meldde hem terug
        job.set(finished=datetime.now())
        save_jobs()
        if job.remote:
            report_remote(job)
        current[0] = None
        activity["done"] = time.time()
        if quit_after.is_set():
            request_quit()
            continue
        remote.wake.set()   # de werker kijkt meteen of er iets ligt, niet pas over een minuut
        if work.empty():
            if auto_active():
                # Met auto aan komt de volgende opdracht meestal binnen een minuut: de modellen niet bij
                # elke korte clip opnieuw laden. Staat de Mac daarna stil, dan alsnog vrijgeven.
                threading.Thread(target=release_later, args=(activity["done"],), daemon=True).start()
            else:
                release_models_now()


def start(model="mlx-community/whisper-large-v3-mlx", language=None, mount=None, port=PORT, idle_exit=False):
    """Start de ontvanger en de werkrij als draden. Gooit OSError als de poort al bezet is.

    `idle_exit`: stoppen zonder opdracht en zonder venster. Alleen voor gebruik vanuit de
    terminal; de app blijft open tot Cmd+Q.
    """
    settings.update(
        hf_token=os.environ.get("HF_TOKEN"), key=listener_key(), model=model,
        language=language, mount=mount, idle_exit=idle_exit)

    transcribe.LISTENER = listen
    remote.set_machine_alias(setting.get("iconik.machine_name"))
    load_jobs()
    atexit.register(save_jobs)
    threading.Thread(target=_saver, daemon=True, name="bewaren").start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="ontvanger").start()
    print(f"Luistert op http://localhost:{port}")
    connection = client()
    print("Iconik: " + ("niet ingelogd" if connection is None else
                        "ingelogd als gebruiker" if connection.uses_login else "persoonlijk token"))
    if not transcribe.speaker_model_available(settings["hf_token"]):
        print("Let op: geen sprekermodel gevonden, sprekerherkenning staat uit.")
    threading.Thread(target=worker, daemon=True, name="werkrij").start()
    threading.Thread(target=watchdog, daemon=True, name="waker").start()
    threading.Thread(target=keep_awake, daemon=True, name="wakker").start()
    threading.Thread(target=watch_storage, daemon=True, name="opslag").start()
    remote.start(client, worker_may_take, busy, add_job, log=say, languages=worker_languages, healthy=progress_ok,
                 auto=auto_settings)
    threading.Thread(target=check_update, daemon=True, name="update").start()
    return server


def main():
    """Los vanuit de terminal, zonder venster (om te testen)."""
    parser = argparse.ArgumentParser(description="Lokale ontvanger voor Iconik Custom Actions.")
    parser.add_argument("--model", default="mlx-community/whisper-large-v3-mlx")
    parser.add_argument("--language", help="Taalcode; standaard de keuze uit de voorkeuren, anders herkennen.")
    parser.add_argument("--mount", help="Lokaal pad van de storage, als dat afwijkt van Iconik.")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    start(args.model, args.language, args.mount, args.port, idle_exit=True)
    print("Ctrl+C om te stoppen")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nGestopt.")


if __name__ == "__main__":
    main()
