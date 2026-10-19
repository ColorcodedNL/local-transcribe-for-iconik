"""Werker-modus: opdrachten uit de rij in Iconik oppakken voor collega's.

De rij is een metadataveld op het asset (`local_transcribe_status`). Een collega zet het op
"aangevraagd"; een Mac met de werker-modus aan zoekt die assets, claimt er een, laat hem door
dezelfde motor lopen als een klik en zet het resultaat terug op "klaar" of "fout".

Standaard uit. Zet `"worker_enabled": true` in config.json (of straks de schakelaar in
Voorkeuren). Er komt niets binnen op deze Mac: de werker vraagt zelf elke minuut aan Iconik.
Deze module praat alleen met Iconik, niet met de rest van de app: `run` krijgt de functies
die het nodig heeft mee.
"""
import functools
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
# Wat in Iconik wordt opgeslagen is Engels, zodat ook Iconik-support het begrijpt. Wat mensen zien
# zijn de labels, in de taal die is gekozen (Voorkeuren, of door een beheerder in Iconik zelf).
REQUESTED, BUSY, DONE, FAILED = "requested", "in_progress", "done", "failed"
STATUSES = (REQUESTED, BUSY, DONE, FAILED)
LEGACY = {"aangevraagd": REQUESTED, "bezig": BUSY, "klaar": DONE, "fout": FAILED}   # waarden tot 0.3.1
LEGACY_VALUE = {new: old for old, new in LEGACY.items()}
LABELS = {
    "nl": {FIELD: "Transcriptie", INFO: "Transcriptie, details",
           REQUESTED: "Aangevraagd", BUSY: "Bezig", DONE: "Klaar", FAILED: "Fout"},
    "en": {FIELD: "Transcription", INFO: "Transcription details",
           REQUESTED: "Requested", BUSY: "In progress", DONE: "Done", FAILED: "Failed"},
}
DESCRIPTIONS = {
    FIELD: "Queue status of Local Transcribe for Iconik. Set to requested and a Mac will transcribe this asset.",
    INFO: "Details of the Local Transcribe for Iconik queue: which Mac is working on it, the result or the error.",
}

POLL_SECONDS = 60
MAX_ATTEMPTS = 3          # pogingen (ook op verschillende Macs) voor een asset definitief "fout" wordt
COOLDOWN = 10 * 60        # na een fout van deze Mac even niets oppakken
CLAIM_WAIT = 3            # seconden tussen claimen en terugkijken of een andere werker sneller was
HEARTBEAT = 50            # een werker die bezig is ververst zo vaak zijn "laatste teken" (de lus draait elke minuut)
STALE_AFTER = 4 * 60      # laatste teken zo oud (drie gemiste hartslagen): de werker is weg, het asset gaat terug de rij in
STALE_LEGACY = 3 * 3600   # "bezig" van een oudere versie, zonder laatste teken: dan blijft het bij 3 uur
RETRY_ELSEWHERE = 10 * 60  # een Mac waarop een asset mislukte, laat het zo lang aan een andere

state = {"enabled": False, "last_poll": None, "last_asset": None, "error": "", "paused_until": None, "pause_reason": ""}
_pause = [0.0]            # tijdstip (time.time) tot wanneer niets wordt opgepakt
_resumed_at = [0.0]       # wanneer de gebruiker zelf zei dat het weer mag (knop of schakelaar)
wake = threading.Event()   # de schakelaar in Voorkeuren zet hem, zodat de werker meteen kijkt
current = {}              # het asset uit de rij waar deze Mac nu aan werkt: asset, code, beat
_beat_lock = threading.Lock()
lost = set()              # assets die deze Mac kwijt is (overgenomen of gewijzigd): daar schrijft hij niets meer naartoe
language = ["nl"]         # de taal van de tekst die deze Mac in het detailveld schrijft
iconik_language = ["nl"]  # de taal van de labels als deze Mac de velden in Iconik aanmaakt


def set_language(code):
    language[0] = code if code in LABELS else "nl"


def tr(dutch, english):
    """Tekst voor het detailveld, in de taal van de app. Het terugleren herkent beide talen."""
    return dutch if language[0] == "nl" else english


def norm(status):
    """Statuswaarde van een oudere versie (aangevraagd, bezig, ...) naar de huidige."""
    return LEGACY.get(status, status)


class WorkerError(Exception):
    """Iets in de opzet klopt niet (veld of view ontbreekt, geen rechten)."""


@functools.lru_cache(maxsize=1)
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


def failed_attempts(info):
    """Hoeveel pogingen zijn er al mislukt, uit "... Mislukte poging 2 van 3, terug in de rij"."""
    match = re.search(r"(?:[Mm]islukte poging|[Ff]ailed attempt) (\d+)", info or "")
    return int(match.group(1)) if match else 0


def current_attempt(info):
    """Welke poging loopt er, uit "Bezig op <Mac> sinds ... , poging 2 van 3". Zonder: de eerste."""
    match = re.search(r"(?:poging|attempt) (\d+) (?:van|of)", info or "")
    return int(match.group(1)) if match else 1


def pause(seconds=COOLDOWN, reason=""):
    """Deze Mac heeft een probleem (opslag weg, geen inlog): even niets oppakken. `reden` is wat de
    gebruiker te zien krijgt, zonder paden."""
    _pause[0] = time.time() + seconds
    state["paused_until"] = stamp(datetime.fromtimestamp(_pause[0]))[-5:]
    state["pause_reason"] = scrub(reason).strip()[:160]


def resume():
    """De gebruiker zegt dat het weer mag (knop Nu hervatten of de schakelaar): de pauze vervalt, en de
    Mac mag ook meteen weer pakken waar hij net op mislukte. Hij heeft het probleem immers opgelost."""
    _pause[0] = 0.0
    _resumed_at[0] = time.time()
    state["paused_until"] = None
    state["pause_reason"] = ""


def paused():
    if time.time() < _pause[0]:
        return True
    state["paused_until"] = None
    state["pause_reason"] = ""
    return False


def busy_owner(info):
    """Wie heeft het asset geclaimd, uit "Bezig op <Mac> sinds ..."."""
    match = re.match(r"(?:Bezig op|Working on) (.+?) (?:sinds|since) ", info or "")
    return match.group(1) if match else ""


def utc_stamp(now=None):
    """UTC, 24 uur, met jaar: voor de machine (het laatste teken), niet om mooi te lezen."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def last_sign(info):
    """Het laatste teken van leven uit "..., laatste teken 2026-10-14 14:17Z". None als het ontbreekt
    (een oudere versie van de app schrijft het niet)."""
    match = re.search(r"(?:laatste teken|last sign) (\d{4}-\d\d-\d\d \d\d:\d\d)Z", info or "")
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def claim_code(info):
    """De willekeurige code uit "Bezig op <Mac> sinds 14:02 (a1b2c3)": hiermee herken je je eigen claim."""
    match = re.search(r"\(([0-9a-f]{6})\)", info or "")
    return match.group(1) if match else ""


def scrub(text):
    """Haalt paden uit een tekst voor het detailveld. Iedereen die de asset ziet, ziet dat veld, en
    een pad verraadt hoe de opslag heet en waar hij staat. In de app zelf blijft de volledige tekst staan."""
    text = re.sub(r"(Bestand niet gevonden|File not found): .*", lambda m: m.group(1) + ".", text or "")
    text = re.sub(r"(De map|The folder) .+?( is niet bereikbaar| cannot be reached)",
                  lambda m: m.group(1) + m.group(2), text)
    text = re.sub(r"""\s*(['"])/[^'"]*\1""", " \u2026", text)
    return re.sub(r"(?<![\w.])/(?:Volumes|Users|private|mnt|Library|System|tmp|var)/\S+", "\u2026", text)


def clean_name(name):
    """Een Mac-naam zonder tekens die de lijst "mislukt op" kapot maken."""
    return (name or "").replace(";", "").replace("[", "").replace("]", "").strip()


def failed_on(info):
    """De Macs waarop dit asset al mislukte, uit "... [mislukt op: Mac A; Mac B]"."""
    match = re.search(r"\[(?:mislukt op|failed on): ([^\]]*)\]", info or "")
    return [n.strip() for n in match.group(1).split(";") if n.strip()] if match else []


def busy_info(who, code, attempt, failed=(), now=None):
    text = tr(f"Bezig op {who} sinds {stamp(now)[-5:]} ({code})", f"Working on {who} since {stamp(now)[-5:]} ({code})")
    if attempt > 1:
        text += tr(f", poging {attempt} van {MAX_ATTEMPTS}", f", attempt {attempt} of {MAX_ATTEMPTS}")
    text += tr(", laatste teken ", ", last sign ") + utc_stamp(now)
    if failed:
        text += tr(" [mislukt op: ", " [failed on: ") + "; ".join(failed) + "]"
    return text


def refresh_sign(info, now=None):
    return re.sub(r"(laatste teken|last sign) \d{4}-\d\d-\d\d \d\d:\d\dZ",
                  lambda m: f"{m.group(1)} {utc_stamp(now)}", info)


class Queue:
    """De rij in Iconik. `iconik` is de verbinding uit transcribe.make_client()."""

    def __init__(self, iconik, view_id=None):
        self.iconik = iconik
        self.view_id = view_id

    def _view(self):
        views = self.iconik.get("metadata/v1/views/").get("objects", [])
        return next((v for v in views if v.get("name") == VIEW), None)

    def _field(self, name):
        return self.iconik.get(f"metadata/v1/fields/{name}/")

    def update_field(self, name, **changes):
        """Wijzigt een bestaand veld. Iconik wil de hele definitie terug, zonder lege waarden en datums."""
        field = self._field(name)
        body = {k: v for k, v in field.items() if k not in ("date_created", "date_modified") and v is not None}
        body.update(changes)
        self.iconik._call("PUT", f"metadata/v1/fields/{name}/", json=body)

    @staticmethod
    def _options(labels):
        return [{"label": labels[s], "value": s} for s in STATUSES]

    def _put_view(self, view_id, labels):
        self.iconik._call("PUT", f"metadata/v1/views/{view_id}/", json={
            "name": VIEW, "write_access_for_everyone": True,
            "view_fields": [{"name": n, "label": labels[n]} for n in (FIELD, INFO)]})

    def setup(self, create=False, language="nl"):
        """Zoekt de view. Met `create` worden ontbrekende velden en de view aangemaakt (dat vraagt
        beheerrechten in Iconik). Een veld met de Nederlandse waarden van 0.3.1 wordt omgezet."""
        labels = LABELS.get(language, LABELS["nl"])
        view = self._view()
        if create:
            have = {f.get("name") for f in self.iconik.get("metadata/v1/fields/").get("objects", [])}
            if FIELD not in have:
                self.iconik.post("metadata/v1/fields/", {
                    "name": FIELD, "label": labels[FIELD], "field_type": "drop_down",
                    "description": DESCRIPTIONS[FIELD], "options": self._options(labels)})
            elif not set(STATUSES) <= {o.get("value") for o in self._field(FIELD).get("options") or []}:
                current = self._field(FIELD).get("options") or []
                try:
                    self.update_field(FIELD, options=self._options(labels))
                except Exception:
                    # Iconik weigert opties weg te halen die in gebruik zijn: de nieuwe erbij, de oude blijven staan
                    kept = [o for o in current if o.get("value") not in STATUSES]
                    self.update_field(FIELD, options=self._options(labels) + kept)
            if INFO not in have:
                self.iconik.post("metadata/v1/fields/", {
                    "name": INFO, "label": labels[INFO], "field_type": "string",
                    "description": DESCRIPTIONS[INFO]})
            wanted = [{"name": n, "label": labels[n]} for n in (FIELD, INFO)]
            if view is None:
                view = self.iconik.post("metadata/v1/views/", {
                    "name": VIEW, "write_access_for_everyone": True, "view_fields": wanted})
            else:
                have_in_view = {f.get("name") for f in view.get("view_fields", [])}
                if not {FIELD, INFO} <= have_in_view:
                    self._put_view(view["id"], labels)
        if view is None:
            raise WorkerError(f"De view '{VIEW}' ontbreekt in Iconik. Laat een beheerder hem aanmaken.")
        self.view_id = view["id"]
        return self.view_id

    def apply_language(self, language):
        """Zet de labels van de velden, de opties en de view op een taal. De waarden veranderen niet.
        Dit geldt voor alle gebruikers van deze Iconik-omgeving en vraagt beheerrechten. Andere views
        die de velden bevatten houden hun eigen kopie: daar moet een beheerder het veld opnieuw toevoegen."""
        labels = LABELS[language]
        self.update_field(FIELD, label=labels[FIELD], options=self._options(labels))
        self.update_field(INFO, label=labels[INFO])
        view = self._view()
        if view is not None:
            self._put_view(view["id"], labels)

    def _path(self, asset_id):
        return f"metadata/v1/assets/{asset_id}/assets/{asset_id}/views/{self.view_id}/"

    def find(self, status, limit=5):
        """Assets met deze status, ook met de Nederlandse waarde van 0.3.1."""
        ids = []
        for value in (status, LEGACY_VALUE[status]):
            body = {"doc_types": ["assets"], "query": "",
                    "filter": {"operator": "AND", "terms": [{"name": f"metadata.{FIELD}", "value": value}]}}
            result = self.iconik.post(f"search/v1/search/?per_page={limit}", body)
            ids += [o["id"] for o in result.get("objects", []) if o.get("id") and o["id"] not in ids]
        return ids[:limit]

    def read(self, asset_id):
        values = self.iconik.get(self._path(asset_id)).get("metadata_values", {})

        def first(name):
            items = (values.get(name) or {}).get("field_values") or []
            return items[0].get("value") if items else None
        since = (values.get(FIELD) or {}).get("date_created")  # wanneer Iconik de status zette
        return norm(first(FIELD)), first(INFO) or "", since

    def write(self, asset_id, status=None, info=None):
        values = {}
        if status is not None:
            values[FIELD] = {"field_values": [{"value": status}], "mode": "overwrite"}
        if info is not None:
            values[INFO] = {"field_values": [{"value": info}], "mode": "overwrite"}
        self.iconik._call("PUT", self._path(asset_id), json={"metadata_values": values})

    def claim(self, asset_id, who, wait=CLAIM_WAIT, sleep=time.sleep):
        """Probeert het asset te claimen. Iconik heeft geen atomaire handeling, dus:
        schrijf, wacht even, lees terug. Staat er dan nog onze eigen regel, dan is het van ons.
        Een asset dat net op deze Mac mislukte laat hij even aan een andere Mac."""
        status, previous, since = self.read(asset_id)
        if status != REQUESTED:
            return 0
        failed = failed_on(previous)
        recent = age_seconds(since)
        resumed_since = time.time() - _resumed_at[0] if _resumed_at[0] else None
        if (clean_name(who) in failed and recent is not None and recent < RETRY_ELSEWHERE
                and not (resumed_since is not None and resumed_since < recent)):   # zelf hervat na het zetten van de status: laten gaan
            return 0
        attempt = failed_attempts(previous) + 1
        mine = busy_info(who, secrets.token_hex(3), attempt, failed)
        self.write(asset_id, BUSY, mine)
        sleep(wait)
        status, info, _ = self.read(asset_id)
        if status == BUSY and info == mine:
            self.claimed_info = mine
            return attempt
        return 0

    def release_stale(self, who, now=None):
        """Assets waarvan de werker niet meer reageert gaan terug in de rij. Met een laatste teken
        is dat na 4 minuten stilte; een regel van een oudere versie zonder laatste teken na 3 uur."""
        released = []
        for asset_id in self.find(BUSY, 20):
            status, info, since = self.read(asset_id)
            if status != BUSY:
                continue
            sign = last_sign(info)
            if sign is not None:
                age, limit = age_seconds(sign.isoformat(), now), STALE_AFTER
            else:
                age, limit = age_seconds(since, now), STALE_LEGACY
            if age is not None and age > limit:
                owner = busy_owner(info)
                self.give_back(asset_id, who, tr(f"{owner or 'De werker'} reageerde niet meer",
                                                 f"{owner or 'The worker'} stopped responding"),
                               current_attempt(info), culprit=owner)
                released.append(asset_id)
        return released

    def give_back(self, asset_id, who, reason, attempt, culprit=None):
        """Het ligt niet aan het asset maar aan de Mac: terug in de rij voor een andere werker,
        tot MAX_ATTEMPTS. De Mac waarop het misging komt in "[mislukt op: ...]". Geeft True als het
        asset terug in de rij staat."""
        reason = scrub(reason).rstrip(". ")[:250]
        _, previous, _ = self.read(asset_id)
        failed = failed_on(previous)
        name = clean_name(culprit or who)
        if name and name not in failed:
            failed.append(name)
        tag = (tr(" [mislukt op: ", " [failed on: ") + "; ".join(failed) + "]") if failed else ""
        if attempt >= MAX_ATTEMPTS:
            self.write(asset_id, FAILED, f"{who}, {stamp()}: {reason} "
                       + tr(f"(na {MAX_ATTEMPTS} pogingen opgegeven)", f"(gave up after {MAX_ATTEMPTS} attempts)") + tag)
            return False
        self.write(asset_id, REQUESTED, f"{who}, {stamp()}: {reason}{'' if reason[-1:] in '?!' else '.'} "
                   + tr(f"Mislukte poging {attempt} van {MAX_ATTEMPTS}, terug in de rij",
                        f"Failed attempt {attempt} of {MAX_ATTEMPTS}, back in the queue") + tag)
        return True

    def finish(self, asset_id, who, ok, message):
        """Schrijft het resultaat. Waren er eerdere pogingen die mislukten, dan blijft dat in één korte
        regel achter het resultaat staan, zodat je later nog ziet waarom het zo lang duurde."""
        text = f"{who}, {stamp()}: {scrub(message)[:300]}"
        try:
            _, previous, _ = self.read(asset_id)
            failed = failed_on(previous)
            if failed:
                text += tr(f" Eerder: mislukt op {', '.join(failed)}.", f" Earlier: failed on {', '.join(failed)}.")
        except Exception:     # de geschiedenis is bijzaak: het resultaat gaat voor
            pass
        self.write(asset_id, DONE if ok else FAILED, text)


def report(queue, job, who=None, log=print):
    """Zet het resultaat van een afgeronde opdracht uit de rij terug in Iconik. Geeft "requeued" als het
    asset terug in de rij is gezet voor een andere Mac, "lost" als het niet meer van ons was, anders None."""
    who = who or machine_name()
    asset_id = job["asset_id"]
    code = None
    with _beat_lock:          # het asset is klaar: de heartbeat mag er niet meer aan komen
        if current.get("asset") == asset_id:
            code = current.get("code")
            current.clear()
        was_lost = asset_id in lost
        lost.discard(asset_id)
    if not was_lost and code:
        try:
            status, info, _ = queue.read(asset_id)
            was_lost = status != BUSY or claim_code(info) != code
        except Exception:     # niet te lezen: laat het schrijven zelf maar falen of slagen
            pass
    if was_lost:
        # Het asset is niet meer van ons (door een andere Mac overgenomen of door een mens gewijzigd).
        # Status en details zijn van de nieuwe eigenaar, dus daar schrijven we niet overheen.
        log(f"Werker: {asset_id} is niet meer van deze Mac, resultaat niet in de rij gezet")
        job["message"] = (job.get("message") or "") + tr(" (asset is door een andere Mac overgenomen, status niet aangepast)",
                                                          " (asset was taken over by another Mac, status not changed)")
        if job["status"] == "fout" and job.get("local"):
            pause(reason=job.get("message", ""))
        return "lost"
    if job["status"] == "fout" and job.get("local"):
        if queue.give_back(job["asset_id"], who, job.get("message", ""), int(job.get("remote") or 1)):
            pause(reason=job.get("message", ""))
            return "requeued"
        return None
    if job["status"] == "klaar":
        queue.finish(job["asset_id"], who, True, job.get("message", ""))
    elif job["status"] == "overgeslagen":
        queue.finish(job["asset_id"], who, True, tr("Overgeslagen: ", "Skipped: ") + job.get("message", ""))
    else:
        queue.finish(job["asset_id"], who, False, job.get("message", ""))


def beat(queue, now=None, healthy=None):
    """Ververst het laatste teken van het asset waar deze Mac aan werkt, zodat een andere Mac
    niet denkt dat het blijft hangen. Geeft True als het is geschreven. Stopt zodra het asset
    niet meer van ons is (door een andere werker overgenomen of door een mens gewijzigd).
    `healthy` (optioneel) zegt of de job nog vordert. Zo niet, dan schrijft hij niets, ook al leeft de
    app: het laatste teken blijft staan en na vier minuten neemt een andere Mac het asset over."""
    with _beat_lock:
        if not current or time.time() - current["beat"] < HEARTBEAT:
            return False
        if healthy is not None and not healthy():
            return False
        status, info, _ = queue.read(current["asset"])
        if status != BUSY or claim_code(info) != current["code"]:
            lost.add(current["asset"])
            current.clear()
            return False
        queue.write(current["asset"], info=refresh_sign(info, now))
        current["beat"] = time.time()
        return True


def poll_once(queue, who, busy, add_job, sleep=time.sleep):
    """Een rondje. Geeft het aangenomen asset-ID terug, of None."""
    if busy() or paused():
        return None
    queue.release_stale(who)
    for asset_id in queue.find(REQUESTED, 5):
        attempt = queue.claim(asset_id, who, sleep=sleep)
        if attempt:
            with _beat_lock:
                current.clear()
                lost.discard(asset_id)
                current.update(asset=asset_id, code=claim_code(queue.claimed_info), beat=time.time())
            add_job(asset_id, remote=attempt)   # het nummer van de poging, voor het terugmelden
            return asset_id
    return None


def run(get_client, enabled, busy, add_job, stop, log=print, interval=POLL_SECONDS, languages=None, healthy=None):
    """De lus van de werker. `enabled()` wordt elk rondje opnieuw gevraagd, zodat de schakelaar
    zonder herstart werkt. Fouten worden gelogd en het volgende rondje probeert het opnieuw."""
    view_id = None
    while not stop.is_set():
        if languages:                 # (taal van het detailveld, taal van de labels in Iconik)
            text, fields = languages()
            set_language(text)
            iconik_language[0] = fields if fields in LABELS else "nl"
        state["enabled"] = bool(enabled())
        if current and view_id:       # ook als de schakelaar uit gaat: de lopende job blijft van ons
            try:
                connection = get_client()
                if connection is not None:
                    beat(Queue(connection, view_id), healthy=healthy)
            except Exception as error:
                log(f"Werker: laatste teken schrijven mislukte: {str(error)[:200]}")
        if state["enabled"]:
            try:
                connection = get_client()
                if connection is None:
                    state["error"] = "Niet ingelogd bij Iconik."
                else:
                    queue = Queue(connection, view_id)
                    if view_id is None:
                        view_id = queue.setup(create=True, language=iconik_language[0])
                    state["last_poll"] = stamp()
                    taken = poll_once(queue, machine_name(), busy, add_job)
                    state["error"] = ""
                    if taken:
                        state["last_asset"] = taken
                        log(f"Werker: asset {taken} aangenomen uit de rij.")
            except Exception as error:
                state["error"] = str(error)[:300]
                log(f"Werker: {state['error']}")
        if wake.wait(interval):
            wake.clear()


def start(get_client, enabled, busy, add_job, log=print, languages=None, healthy=None):
    stop = threading.Event()
    threading.Thread(target=run, args=(get_client, enabled, busy, add_job, stop, log, POLL_SECONDS, languages, healthy),
                     daemon=True, name="werker").start()
    return stop
