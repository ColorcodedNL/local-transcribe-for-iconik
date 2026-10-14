"""Werker-modus: opdrachten uit de rij in Iconik oppakken voor collega's.

De rij is een metadataveld op het asset (`local_transcribe_status`). Een collega zet het op
"aangevraagd"; een Mac met de werker-modus aan zoekt die assets, claimt er een, laat hem door
dezelfde motor lopen als een klik en zet het resultaat terug op "klaar" of "fout".

Standaard uit. Zet `"worker_enabled": true` in config.json (of straks de schakelaar in
Voorkeuren). Er komt niets binnen op deze Mac: de werker vraagt zelf elke minuut aan Iconik.
Deze module praat alleen met Iconik, niet met de rest van de app: `run` krijgt de functies
die het nodig heeft mee.
"""
import re
import secrets
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone

FIELD = "local_transcribe_status"
INFO = "local_transcribe_info"
VIEW = "Local Transcribe"
LABELS = {FIELD: "Transcriptie", INFO: "Transcriptie, details"}
REQUESTED, BUSY, DONE, FAILED = "aangevraagd", "bezig", "klaar", "fout"
STATUSES = (REQUESTED, BUSY, DONE, FAILED)

POLL_SECONDS = 60
CLAIM_WAIT = 3            # seconden tussen claimen en terugkijken of een andere werker sneller was
STALE_AFTER = 3 * 3600    # "bezig" zonder levensteken: de werker is weg, terug de rij in

state = {"enabled": False, "last_poll": None, "last_asset": None, "error": ""}


class WorkerError(Exception):
    """Iets in de opzet klopt niet (veld of view ontbreekt, geen rechten)."""


def machine_name():
    """De computernaam uit Systeeminstellingen. De hostnaam van het netwerk kan er vreemd uitzien."""
    for args in (["scutil", "--get", "ComputerName"], ["scutil", "--get", "LocalHostName"]):
        try:
            name = subprocess.run(args, capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if name:
            return name
    name = socket.gethostname().split(".")[0]
    return "".join(c for c in name if c.isprintable() and c.isascii()) or "Mac"


def stamp(now=None):
    """Lokale tijd, zoals een collega hem wil lezen."""
    return (now or datetime.now()).strftime("%d-%m %H:%M")


def age_seconds(iso, now=None):
    """Hoe oud is een tijd die Iconik meegeeft? None als hij niet te lezen is."""
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - then).total_seconds()


def busy_owner(info):
    """Wie heeft het asset geclaimd, uit "Bezig op <Mac> sinds ..."."""
    match = re.match(r"Bezig op (.+?) sinds ", info or "")
    return match.group(1) if match else ""


class Queue:
    """De rij in Iconik. `iconik` is de verbinding uit transcribe.make_client()."""

    def __init__(self, iconik, view_id=None):
        self.iconik = iconik
        self.view_id = view_id

    def _view(self):
        views = self.iconik.get("metadata/v1/views/").get("objects", [])
        return next((v for v in views if v.get("name") == VIEW), None)

    def setup(self, create=False):
        """Zoekt de view. Met `create` worden ontbrekende velden en de view aangemaakt
        (dat vraagt beheerrechten in Iconik)."""
        view = self._view()
        if create:
            have_fields = {f.get("name") for f in self.iconik.get("metadata/v1/fields/").get("objects", [])}
            if FIELD not in have_fields:
                self.iconik.post("metadata/v1/fields/", {
                    "name": FIELD, "label": LABELS[FIELD], "field_type": "drop_down",
                    "options": [{"label": s, "value": s} for s in STATUSES]})
            if INFO not in have_fields:
                self.iconik.post("metadata/v1/fields/", {
                    "name": INFO, "label": LABELS[INFO], "field_type": "string"})
            wanted = [{"name": n, "label": LABELS[n]} for n in (FIELD, INFO)]
            if view is None:
                view = self.iconik.post("metadata/v1/views/", {
                    "name": VIEW, "write_access_for_everyone": True, "view_fields": wanted})
            else:
                have_in_view = {f.get("name") for f in view.get("view_fields", [])}
                if not {FIELD, INFO} <= have_in_view:
                    self.iconik._call("PUT", f"metadata/v1/views/{view['id']}/", json={
                        "name": VIEW, "write_access_for_everyone": True, "view_fields": wanted})
        if view is None:
            raise WorkerError(f"De view '{VIEW}' ontbreekt in Iconik. Laat een beheerder hem aanmaken.")
        self.view_id = view["id"]
        return self.view_id

    def _path(self, asset_id):
        return f"metadata/v1/assets/{asset_id}/assets/{asset_id}/views/{self.view_id}/"

    def find(self, status, limit=5):
        body = {"doc_types": ["assets"], "query": "",
                "filter": {"operator": "AND", "terms": [{"name": f"metadata.{FIELD}", "value": status}]}}
        result = self.iconik.post(f"search/v1/search/?per_page={limit}", body)
        return [o["id"] for o in result.get("objects", []) if o.get("id")]

    def read(self, asset_id):
        values = self.iconik.get(self._path(asset_id)).get("metadata_values", {})

        def first(name):
            items = (values.get(name) or {}).get("field_values") or []
            return items[0].get("value") if items else None
        since = (values.get(FIELD) or {}).get("date_created")  # wanneer Iconik de status zette
        return first(FIELD), first(INFO) or "", since

    def write(self, asset_id, status=None, info=None):
        values = {}
        if status is not None:
            values[FIELD] = {"field_values": [{"value": status}], "mode": "overwrite"}
        if info is not None:
            values[INFO] = {"field_values": [{"value": info}], "mode": "overwrite"}
        self.iconik._call("PUT", self._path(asset_id), json={"metadata_values": values})

    def claim(self, asset_id, who, wait=CLAIM_WAIT, sleep=time.sleep):
        """Probeert het asset te claimen. Iconik heeft geen atomaire handeling, dus:
        schrijf, wacht even, lees terug. Staat er dan nog onze eigen regel, dan is het van ons."""
        status = self.read(asset_id)[0]
        if status != REQUESTED:
            return False
        mine = f"Bezig op {who} sinds {stamp()[-5:]} ({secrets.token_hex(3)})"
        self.write(asset_id, BUSY, mine)
        sleep(wait)
        status, info, _ = self.read(asset_id)
        return status == BUSY and info == mine

    def release_stale(self, who, now=None):
        """Assets die te lang op "bezig" staan (de werker is weg) gaan terug in de rij."""
        released = []
        for asset_id in self.find(BUSY, 20):
            status, info, since = self.read(asset_id)
            if status != BUSY:
                continue
            age = age_seconds(since, now)
            if age is not None and age > STALE_AFTER:
                self.write(asset_id, REQUESTED,
                           f"{who}, {stamp()}: teruggezet, {busy_owner(info) or 'de werker'} reageerde niet meer")
                released.append(asset_id)
        return released

    def finish(self, asset_id, who, ok, message):
        self.write(asset_id, DONE if ok else FAILED, f"{who}, {stamp()}: {(message or '')[:300]}")


def report(queue, job, who=None):
    """Zet het resultaat van een afgeronde opdracht uit de rij terug in Iconik."""
    who = who or machine_name()
    if job["status"] == "klaar":
        queue.finish(job["asset_id"], who, True, job.get("message", ""))
    elif job["status"] == "overgeslagen":
        queue.finish(job["asset_id"], who, True, "Overgeslagen: " + job.get("message", ""))
    else:
        queue.finish(job["asset_id"], who, False, job.get("message", ""))


def poll_once(queue, who, busy, add_job, sleep=time.sleep):
    """Een rondje. Geeft het aangenomen asset-ID terug, of None."""
    if busy():
        return None
    queue.release_stale(who)
    for asset_id in queue.find(REQUESTED, 5):
        if queue.claim(asset_id, who, sleep=sleep):
            add_job(asset_id, remote=True)
            return asset_id
    return None


def run(get_client, enabled, busy, add_job, stop, log=print, interval=POLL_SECONDS):
    """De lus van de werker. `enabled()` wordt elk rondje opnieuw gevraagd, zodat de schakelaar
    zonder herstart werkt. Fouten worden gelogd en het volgende rondje probeert het opnieuw."""
    view_id = None
    while not stop.is_set():
        state["enabled"] = bool(enabled())
        if state["enabled"]:
            try:
                connection = get_client()
                if connection is None:
                    state["error"] = "Niet ingelogd bij Iconik."
                else:
                    queue = Queue(connection, view_id)
                    if view_id is None:
                        view_id = queue.setup(create=True)
                    state["last_poll"] = stamp()
                    taken = poll_once(queue, machine_name(), busy, add_job)
                    state["error"] = ""
                    if taken:
                        state["last_asset"] = taken
                        log(f"Werker: asset {taken} aangenomen uit de rij.")
            except Exception as error:
                state["error"] = str(error)[:300]
                log(f"Werker: {state['error']}")
        stop.wait(interval)


def start(get_client, enabled, busy, add_job, log=print):
    stop = threading.Event()
    threading.Thread(target=run, args=(get_client, enabled, busy, add_job, stop, log),
                     daemon=True, name="werker").start()
    return stop
