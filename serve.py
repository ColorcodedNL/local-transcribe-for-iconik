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
import html
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
from urllib.parse import parse_qs, urlparse

import requests

import app_shell
import transcribe
import updater
import worker as remote
from transcribe import Iconik, Problem

PORT = 8765
PAGE_SIZE = 100
MAX_DEPTH = 5  # hoe diep collecties in collecties worden gevolgd

KEY_FILE = transcribe.CONFIG_PATH.parent / "key"
pending_login = {}        # state -> verifier, voor een inlog die onderweg is
pending_ids = []          # opdrachten die wachten tot er is ingelogd
window_state = {"n": 0, "page": "/"}   # wat het venster van de app moet tonen
show_hook = None  # de app zet hier een functie neer die het venster toont
restart_hook = None  # idem, om opnieuw te starten na een code-update


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
    with jobs_lock:
        waiting = [job for job in jobs if job["status"] == "wachtend"]
    threading.Thread(target=fill_titles, args=(waiting,), daemon=True).start()
    print(f"Ontvangen: {len(asset_ids)} asset(s), {added} toegevoegd aan de rij.")
    return added


jobs = []                 # nieuwste achteraan
jobs_lock = threading.Lock()
work = queue.Queue()
settings = {}
cleared = [0]             # aantal opgeruimde opdrachten, zodat nummers uniek blijven
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


def make_options(job):
    return argparse.Namespace(
        asset_id=job["asset_id"], model=settings["model"], language=settings["language"] or transcribe.load_config().get("language", "auto"),
        mount=settings["mount"], push=True, replace=True, speakers=None,
        no_speakers=not transcribe.speaker_model_available(settings["hf_token"]), no_voice_check=False, out="/tmp/whisper-test")


def start_prefetch():
    """Leest de audio van de volgende opdracht alvast in, terwijl de GPU met deze bezig is.

    Bij een netwerkvolume kost het lezen van een groot bestand tientallen seconden.
    Lukt het vooruit lezen niet (of is er een mapkeuze nodig), dan doet de opdracht het later zelf.
    """
    with work.mutex:
        upcoming = work.queue[0] if work.queue else None
    if upcoming is None or "prefetch" in upcoming:
        return
    box = {"events": [], "lines": [], "result": None}

    def run():
        transcribe._local.events, transcribe._local.lines = box["events"], box["lines"]
        try:
            connection = client()
            if connection is not None:
                def on_info(title, seconds, job=upcoming):
                    job["title"], job["duration"] = title or "", seconds
                box["result"] = transcribe.prepare_asset(connection, make_options(upcoming), on_info, interactive=False)
        except transcribe.NeedsFolder:
            box["lines"].append("Vooruit lezen overgeslagen: mount point nog onbekend.")
        except Exception as error:
            box["lines"].append(f"Vooruit lezen gestopt ({type(error).__name__}: {error}), de opdracht leest zelf.")
        finally:
            transcribe._local.events = transcribe._local.lines = None

    box["thread"] = threading.Thread(target=run, daemon=True)
    upcoming["prefetch"] = box
    box["thread"].start()
    print(f"Vooruit lezen gestart: {upcoming['asset_id']}")


def listen(kind, **data):
    """Krijgt de voortgang van de motor en zet die bij de lopende opdracht."""
    last_progress[0] = time.monotonic()
    job = current[0]
    if job is None:
        return
    if kind == "stage":
        job["stage"], job["fraction"] = data["name"], None
        if data["name"] == "transcriberen":
            start_prefetch()  # het eigen bestand is gelezen, het netwerk is vrij
    elif kind == "progress":
        job["fraction"] = data["fraction"]
    elif kind == "language":
        job["language"] = data["code"]
    elif kind == "tracks":
        job["tracks"] = data["tracks"]


ACTION_TITLE = "Local Transcribe"
ACTION_URL = "iconiktranscribe://transcribe"
ACTION_CONTEXTS = ("ASSET", "COLLECTION", "BULK")
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
               "User-Agent": f"{transcribe.APP_NAME}/{transcribe.APP_VERSION}"}
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


def missing_actions(connection):
    existing = connection.get("assets/v1/custom_actions/?per_page=200").get("objects", [])
    have = {a["context"] for a in existing if a.get("url") == ACTION_URL}
    return [context for context in ACTION_CONTEXTS if context not in have]


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
        for context in missing_actions(connection):
            connection.post(f"assets/v1/custom_actions/{context}/",
                            {"context": context, "title": ACTION_TITLE, "type": "OPEN", "url": ACTION_URL})
        actions["state"] = "aanwezig"
        print("De knop Local Transcribe is in Iconik gezet.")
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


def mount_list(config):
    rows = []
    for storage_id, path in config.get("mounts", {}).items():
        if storage_id not in storage_names:
            storage_names[storage_id] = ""
            threading.Thread(target=fetch_storage_name, args=(storage_id,), daemon=True).start()
        rows.append({"id": storage_id, "name": storage_names[storage_id], "path": path})
    return rows


def change_mount(storage_id, remove=False):
    config = transcribe.load_config()
    mounts = config.get("mounts", {})
    if storage_id not in mounts:
        return
    if remove:
        del mounts[storage_id]
    else:
        name = storage_names.get(storage_id) or storage_id
        folder = transcribe.ask_folder(transcribe.L(
            f"ISG \u201c{name}\u201d: kies het mount point van deze ISG op deze Mac.",
            f"ISG \u201c{name}\u201d: choose the mount point of this ISG on this Mac."), mounts[storage_id])
        if not folder:
            return
        mounts[storage_id] = folder
    transcribe.save_config(config)


def app_state():
    """Alles wat het venster toont, als gegevens."""
    now = datetime.now()
    with jobs_lock:
        rows = list(reversed(jobs))
    out = []
    for job in rows:
        end = job["finished"] or now
        out.append({
            "n": job["n"], "asset_id": job["asset_id"], "title": job["title"],
            "status": job["status"], "message": job["message"],
            "added": job["added"].strftime("%H:%M"), "stage": job["stage"],
            "fraction": job["fraction"], "tracks": job["tracks"],
            "duration": job.get("duration"),
            "result": job.get("result"), "language": job.get("language"),
            "elapsed": (end - job["started"]).total_seconds() if job["started"] else None,
        })
    config = transcribe.load_config()
    connection = client()
    account = ("geen" if connection is None and not config.get("client_id") else
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
    local = (transcribe.LOCAL_SPEAKER_MODEL / "config.yaml").is_file()
    return {
        "jobs": out, "account": account, "user": who.get("name", ""), "actions": actions.get("state", "onbekend"),
        "lang": transcribe.app_language(), "app_language": config.get("app_language", "auto"),
        "version": transcribe.APP_VERSION, "update": update_view(),
        "spoken": settings.get("language") or config.get("language", "auto"),
        "spoken_fixed": bool(settings.get("language")), "has_client_id": bool(config.get("client_id")),
        "mounts": mount_list(config), "model": settings.get("model", ""),
        "speakers": "lokaal" if local else "huggingface" if settings.get("hf_token") else "uit",
        "worker": dict(remote.state, enabled=worker_enabled(), machine=remote.machine_name(),
                       login=login_item_on(), login_available=bundle_path() is not None,
                       iconik_language=worker_languages()[1]),
    }


def clear_finished():
    with jobs_lock:
        keep = [job for job in jobs if job["status"] in ("wachtend", "bezig")]
        cleared[0] += len(jobs) - len(keep)
        jobs[:] = keep


def add_job(asset_id, remote=0):
    """`remote`: de opdracht komt uit de rij in Iconik (werker-modus), het resultaat gaat daar terug.
    Het is het nummer van de poging (1, 2 of 3); 0 betekent een gewone opdracht."""
    with jobs_lock:
        for job in jobs:
            if job["asset_id"] == asset_id and job["status"] in ("wachtend", "bezig"):
                return False
        job = {"n": len(jobs) + 1 + cleared[0], "asset_id": asset_id, "title": "",
               "status": "wachtend", "message": "", "added": datetime.now(), "remote": remote,
               "started": None, "finished": None, "stage": "", "fraction": None,
               "tracks": []}
        jobs.append(job)
    work.put(job)
    return True


def fill_titles(new_jobs):
    """Haalt op de achtergrond de titels op, zodat de rij leesbaar is."""
    for job in new_jobs:
        if job["title"]:
            continue
        try:
            title = client().get(f"assets/v1/assets/{job['asset_id']}/").get("title")
            if title and not job["title"]:
                job["title"] = title
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


def split_ids(values):
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def clock(seconds):
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def running(job):
    """Tekst voor een opdracht die bezig is: hoe lang al, en hoe lang de audio is."""
    if job["status"] != "bezig" or not job["started"]:
        return ""
    text = f"{clock((datetime.now() - job['started']).total_seconds())} bezig"
    if job.get("duration"):
        text += f", audio duurt {clock(job['duration'])}"
    return text


def status_page(note=""):
    """Het venster: een vaste pagina die zelf de gegevens ophaalt bij /state."""
    return UI_FILE.read_text().replace('"__NOTE__"', json.dumps(note))


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

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        first = lambda name: query.get(name, [""])[0]
        try:
            if url.path == "/":
                return self.reply(200, status_page())
            if url.path == "/state":
                activity["poll"] = time.time()
                return self.reply_json(app_state())
            if url.path in ("/mount/change", "/mount/remove"):
                change_mount(first("id"), remove=url.path.endswith("remove"))
                return self.reply_json({"ok": True})
            if url.path == "/update/install":
                if self.headers.get("X-Local-Transcribe") != "1":  # een webpagina kan dit niet meesturen
                    return self.send_error(403)
                install_update()
                return self.reply_json({"ok": True})
            if url.path == "/worker":
                if self.headers.get("X-Local-Transcribe") != "1":  # een webpagina mag dit niet omzetten
                    return self.send_error(403)
                if first("value") in ("on", "off"):
                    config = transcribe.load_config()
                    config["worker_enabled"] = first("value") == "on"
                    transcribe.save_config(config)
                    remote.resume()
                    remote.wake.set()
                return self.reply_json({"ok": True})
            if url.path == "/iconik-language":
                if self.headers.get("X-Local-Transcribe") != "1":
                    return self.send_error(403)
                code = first("value")
                if code not in ("nl", "en"):
                    return self.reply_json({"ok": False, "error": "language"})
                try:
                    connection = client()
                    if connection is None:
                        raise Problem(transcribe.L("Niet ingelogd bij Iconik.", "Not logged in to Iconik."))
                    queue_in_iconik = remote.Queue(connection)
                    queue_in_iconik.setup(create=True, language=code)
                    queue_in_iconik.apply_language(code)
                except Exception as error:
                    return self.reply_json({"ok": False, "error": str(error)[:300]})
                config = transcribe.load_config()
                config["iconik_language"] = code
                transcribe.save_config(config)
                return self.reply_json({"ok": True})
            if url.path == "/login":
                if self.headers.get("X-Local-Transcribe") != "1":
                    return self.send_error(403)
                if first("value") in ("on", "off"):
                    set_login_item(first("value") == "on")
                return self.reply_json({"ok": True})
            if url.path == "/update/open":
                if update.get("url"):
                    subprocess.Popen(["open", update["url"]])  # in de gewone browser, niet in het venster
                return self.reply_json({"ok": True})
            if url.path == "/actions/install":
                install_actions()
                return self.reply_json({"ok": True})
            if url.path == "/applang":
                if first("value") in ("auto", "nl", "en"):
                    config = transcribe.load_config()
                    config["app_language"] = first("value")
                    transcribe.save_config(config)
                return self.reply_json({"ok": True})
            if url.path == "/language":
                value = first("value")
                if re.fullmatch(r"auto|[a-z]{2,3}", value):
                    config = transcribe.load_config()
                    config["language"] = value
                    transcribe.save_config(config)
                return self.reply_json({"ok": True})
            if url.path == "/clear":
                clear_finished()
                return self.reply_json({"ok": True})
            if url.path == "/window/state":
                return self.reply_json(window_state)
            if url.path == "/window/show":
                # De app vraagt het venster een pagina te tonen en naar voren te komen.
                window_state["page"] = "/login" if first("page") == "/login" else "/"
                window_state["n"] += 1
                if show_hook:
                    show_hook(window_state["page"])
                return self.reply_json(window_state)
            if url.path == "/settings":
                return self.save_settings(first("client_id").strip())
            if url.path == "/login":
                address, state, verifier = transcribe.start_login()
                pending_login.clear()
                pending_login[state] = verifier
                return self.redirect(address)
            if url.path == "/oauth/callback":
                return self.finish_login(first("code"), first("state"), first("error"))
            if url.path == "/logout":
                transcribe.clear_login()
                who.clear()
                actions.clear()
                return self.redirect("/")
            return self.transcribe(url, query)
        except Problem as problem:
            return self.reply(200, status_page(str(problem)))

    def save_settings(self, client_id):
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", client_id):
            return self.reply(200, status_page(transcribe.L("Dat lijkt geen geldige client-ID.", "That does not look like a valid client ID.")))
        config = transcribe.load_config()
        config["client_id"] = client_id.lower()
        transcribe.save_config(config)
        return self.redirect("/login")

    def finish_login(self, code, state, error):
        verifier = pending_login.pop(state, None)
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
            self.queue_request(waiting)
        return self.redirect("/")

    def queue_request(self, requests_waiting):
        """Zet opdrachten in de rij; `requests_waiting` is een lijst (asset_ids, collection_ids)."""
        connection = client()
        asset_ids = []
        for assets, collections in requests_waiting:
            asset_ids += assets
            for collection_id in collections:
                asset_ids += collection_assets(connection, collection_id)
        enqueue(asset_ids)

    def transcribe(self, url, query):
        # De sleutel staat in het pad: /transcribe/<sleutel>
        parts = url.path.strip("/").split("/")
        if parts[0] != "transcribe" or len(parts) > 2:
            return self.reply(404, "Niet gevonden.")
        given = parts[1] if len(parts) == 2 else query.get("key", [""])[0]
        if given != settings["key"]:
            print("Geweigerd: verzoek zonder geldige sleutel.")
            return self.reply(403, "Geen geldige sleutel.")

        request = (split_ids(query.get("asset_ids", [])), split_ids(query.get("collection_ids", [])))
        if client() is None:
            # Eerst inloggen; de opdracht gaat daarna alsnog de rij in.
            pending_ids.append(request)
            if transcribe.load_config().get("client_id"):
                return self.redirect("/login")
            return self.redirect("/")
        self.queue_request([request])
        # Doorsturen naar de statuspagina, zodat de sleutel uit de adresbalk verdwijnt
        # en verversen de opdracht niet opnieuw instuurt.
        return self.redirect("/")


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
            remote.report(queue_in_iconik, job)
            return
        except Exception as error:
            print(f"Resultaat terugmelden mislukte ({attempt + 1}/3): {error}")
            time.sleep(5)


def worker_enabled():
    return bool(transcribe.load_config().get("worker_enabled"))


def worker_languages():
    """(taal van het detailveld, taal van de labels in Iconik). De labels volgen de app tot iemand
    een taal kiest: ze gelden voor iedereen in deze Iconik-omgeving, dus ze wisselen niet vanzelf mee."""
    chosen = transcribe.load_config().get("iconik_language")
    return transcribe.app_language(), chosen if chosen in ("nl", "en") else transcribe.app_language()


LOGIN_PLIST = Path.home() / "Library" / "LaunchAgents" / "nl.colorcoded.local-transcribe.plist"
LOGIN_MARKER = transcribe.SUPPORT_DIR / "login-start"  # de LaunchAgent legt dit neer, de app ruimt het op


def bundle_path():
    """De app zelf, alleen als dit de echte app is (niet bij `uv run serve.py`)."""
    res = os.environ.get("LOCALTRANSCRIBE_RES")
    return Path(res).parent.parent if res else None


def login_item_on():
    return LOGIN_PLIST.exists()


def set_login_item(on):
    """Opstarten bij inloggen: een LaunchAgent die de app opent. Gaat in bij de volgende login."""
    import plistlib
    import shlex
    bundle = bundle_path()
    if not on:
        LOGIN_PLIST.unlink(missing_ok=True)
        return
    if bundle is None:
        return
    command = f"touch {shlex.quote(str(LOGIN_MARKER))}; exec /usr/bin/open -a {shlex.quote(str(bundle))}"
    LOGIN_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOGIN_PLIST.write_bytes(plistlib.dumps({
        "Label": LOGIN_PLIST.stem,
        "ProgramArguments": ["/bin/sh", "-c", command],
        "RunAtLoad": True,
    }))


def stall_limit(job):
    """Hoe lang een opdracht niets mag melden. De sprekerherkenning meldt tussendoor niets en duurt
    bij lange audio langer, dus de grens groeit mee met de lengte."""
    return max(STALL_SECONDS, 0.5 * (job.get("duration") or 0))


def progress_ok():
    """Vordert de lopende opdracht nog? Het eenmalige downloaden van het model beoordelen we niet."""
    job = current[0]
    if job is None or job.get("stage") == "model":
        return True
    return time.monotonic() - last_progress[0] <= stall_limit(job)


def leave(code):
    os._exit(code)


def give_up_stalled(job):
    """Een vastgelopen draad is in Python niet te stoppen. De schone weg: de opdracht terugmelden
    (een andere Mac kan hem dan oppakken) en de app herstarten."""
    minutes = max(1, round((time.monotonic() - last_progress[0]) / 60))
    nl, en = ("minuut", "minute") if minutes == 1 else ("minuten", "minutes")
    message = transcribe.L(f"De opdracht meldde {minutes} {nl} niets meer en is gestopt.",
                           f"The job reported nothing for {minutes} {en} and was stopped.")
    print(f"HANGT: {job['asset_id']} meldde {minutes} {nl} niets, de app herstart.", flush=True)
    job.update(abandoned=True, status="fout", local=True, message=message, finished=datetime.now())
    if job.get("remote"):
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
        job["status"] = "bezig"
        job["started"] = datetime.now()
        last_progress[0] = time.monotonic()
        current[0] = job

        def on_info(title, seconds, job=job):
            job["title"], job["duration"] = title or "", seconds
        print(f"\n===== {job['asset_id']} =====")
        options = make_options(job)
        prepared = None
        box = job.pop("prefetch", None)
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
            job["title"] = result["title"] or ""
            job["status"] = "klaar"
            job["message"] = f"{result['segments']} regels, {result['speakers']} spreker(s)"
            job["result"] = {"segments": result["segments"], "speakers": result["speakers"]}
        except transcribe.NeedsFolder as folder:
            job["status"] = "fout"
            job["local"] = True
            job["message"] = transcribe.L(f"Het mount point van ISG {folder} is op deze Mac nog niet ingesteld.",
                                          f"The mount point of ISG {folder} is not set on this Mac yet.")
            print(f"FOUT: {job['message']}")
        except Problem as problem:
            skipped = getattr(problem, "skip", False)
            job["local"] = getattr(problem, "local", False)
            job["status"] = "overgeslagen" if skipped else "fout"
            job["message"] = str(problem)
            print(f"{job['status'].upper()}: {problem}")
        except Exception as error:  # de rij moet blijven lopen
            job["status"] = "fout"
            job["message"] = f"{type(error).__name__}: {error}"
            print(f"FOUT: {job['message']}")
        finally:
            if job.get("abandoned"):
                continue   # de watchdog gaf deze opdracht al op en meldde hem terug
            job["finished"] = datetime.now()
            if job.get("remote"):
                report_remote(job)
            current[0] = None
            activity["done"] = time.time()
            if work.empty():
                try:
                    transcribe.release_models()
                    transcribe.memory_report("vrijgeven")
                except Exception as error:
                    print(f"Modellen vrijgeven mislukte: {error}")


def start(model="mlx-community/whisper-large-v3-mlx", language=None, mount=None, port=PORT, idle_exit=False):
    """Start de ontvanger en de werkrij als draden. Gooit OSError als de poort al bezet is.

    `idle_exit`: stoppen zonder opdracht en zonder venster. Alleen voor gebruik vanuit de
    terminal; de app blijft open tot Cmd+Q.
    """
    settings.update(
        hf_token=os.environ.get("HF_TOKEN"), key=listener_key(), model=model,
        language=language, mount=mount, idle_exit=idle_exit)

    transcribe.LISTENER = listen
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
    remote.start(client, worker_enabled, busy, add_job, languages=worker_languages, healthy=progress_ok)
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
