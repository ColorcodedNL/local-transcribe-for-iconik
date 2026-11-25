"""De lijst met opdrachten: een Job per regel, één grendel voor de lijst en voor alle velden van elke opdracht,
bewaard in jobs.json.

Lezen mag altijd (één veld lezen is in Python heel). Veranderen gaat via job.set(...) of met de grendel vast,
zodat bewaren en /state nooit een opdracht half veranderd zien (status klaar zonder eindtijd).

Elke opdracht hoort bij een item (items.py): iconik-<exemplaar>:<asset>, later local:<id>. jobs.json heeft een
formaatversie; een bestand zonder versie (tot stap 4) had alleen asset_id en wordt bij het lezen omgezet naar de
enige Iconik.

De lijst blijft bewaard tot de gebruiker hem zelf opruimt. Wat bij een herstart nog liep of wachtte, komt terug als
"afgebroken": de opdracht wordt niet hervat. Opdrachten uit de rij in Iconik komen vanzelf terug, want die staan
daar nog.
"""
import json
import os
import threading
from datetime import datetime

import base   # base.L bij het aanroepen, zodat een test die transcribe.L vervangt ook hier telt
import items

ACTIVE = ("wachtend", "bezig")
MAX_KEPT = 500
VERSION = 2
# asset_id gaat mee voor een oudere versie van de app die dezelfde supportmap leest (0.6 slaat een regel zonder
# asset_id over en zou de lijst leeg terugschrijven); bij het lezen van versie 2 telt alleen item.
KEPT_FIELDS = ("n", "item", "asset_id", "title", "status", "message", "duration", "result", "language", "tracks", "auto")
TIME_FIELDS = ("added", "started", "finished")

lock = threading.RLock()     # één grendel: de lijst en alle velden van alle opdrachten
_save_lock = threading.Lock()


class Job:
    """Eén regel in de lijst. `item`: waar de opdracht over gaat; `asset_id` is het asset in Iconik (leeg voor
    een item dat niet uit Iconik komt). `remote`: het nummer van de poging als de opdracht uit de rij in Iconik komt
    (0 is een gewone opdracht). `auto`: door auto transcribe opgepakt. `local`: de fout ligt aan deze Mac.
    `abandoned`: de watchdog gaf de opdracht op. `prefetch`: wat het vooruit lezen voor deze opdracht klaarzet."""

    __slots__ = ("n", "item", "asset_id", "title", "status", "message", "added", "remote", "started", "finished", "stage",
                 "fraction", "tracks", "duration", "result", "language", "auto", "local", "abandoned", "prefetch")

    def __init__(self, n=0, item="", asset_id="", title="", status="wachtend", message="", added=None, remote=0, started=None,
                 finished=None, stage="", fraction=None, tracks=None, duration=None, result=None, language=None,
                 auto=False, local=False, abandoned=False, prefetch=None):
        self.n, self.item, self.title, self.status, self.message = n, item, title, status, message
        self.asset_id = asset_id or (items.asset_id(item) if item else "")
        self.added = added or datetime.now()
        self.remote, self.started, self.finished, self.stage, self.fraction = remote, started, finished, stage, fraction
        self.tracks = list(tracks or [])
        self.duration, self.result, self.language = duration, result, language
        self.auto, self.local, self.abandoned, self.prefetch = bool(auto), bool(local), bool(abandoned), prefetch

    def set(self, **changes):
        """Verandert een of meer velden in één keer, onder de grendel."""
        with lock:
            for key, value in changes.items():
                setattr(self, key, value)

    @property
    def active(self):
        return self.status in ACTIVE

    def view(self):
        """Een kopie van alle velden als gewone gegevens (met de grendel vast aanroepen voor een samenhangend beeld)."""
        return {key: getattr(self, key) for key in self.__slots__ if key != "prefetch"}

    def __repr__(self):
        return f"Job(n={self.n}, item={self.item!r}, status={self.status!r})"


def _time(text):
    try:
        return datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None


class JobList:
    """De opdrachten, nieuwste achteraan. `cleared` telt opgeruimde regels, zodat nummers uniek blijven."""

    def __init__(self):
        self.lock = lock
        self._items = []
        self.cleared = 0

    # --- lezen zoals een lijst (een kopie, dus veilig om doorheen te lopen) ---
    def __iter__(self):
        with lock:
            return iter(list(self._items))

    def __len__(self):
        return len(self._items)

    def __getitem__(self, index):
        with lock:
            return self._items[index]

    def find(self, number):
        with lock:
            return next((j for j in self._items if j.n == number), None)

    def views(self, newest_first=True):
        """Alle opdrachten als gewone gegevens, samenhangend gelezen."""
        with lock:
            rows = [job.view() for job in self._items]
        return rows[::-1] if newest_first else rows

    # --- veranderen ---
    def next_number(self):
        """Volgnummer voor een nieuwe regel."""
        with lock:
            return max(len(self._items) + self.cleared, max((j.n for j in self._items), default=0)) + 1

    def append(self, job):
        """Een kant-en-klare opdracht achteraan (tests, en de rode regels hieronder)."""
        with lock:
            self._items.append(job)
        return job

    def clear(self):
        """Alle regels weg, zonder ze als opgeruimd te tellen (zoals list.clear; voor tests en het opnieuw laden)."""
        with lock:
            self._items.clear()

    def add(self, item, remote=0, auto=False):
        """Een nieuwe wachtende opdracht, of None als dit item al wacht of loopt."""
        with lock:
            if any(j.item == item and j.active for j in self._items):
                return None
            return self.append(Job(n=self.next_number(), item=item, remote=remote, auto=auto))

    def add_failed(self, title, message):
        """Een rode regel zonder opdracht erachter (bijvoorbeeld een collectie die niet uit te pakken was)."""
        now = datetime.now()
        with lock:
            return self.append(Job(n=self.next_number(), title=title, status="fout", message=message,
                                   added=now, finished=now))

    def clear_finished(self):
        with lock:
            keep = [j for j in self._items if j.active]
            self.cleared += len(self._items) - len(keep)
            self._items[:] = keep

    def remove(self, number, everything=False):
        """Eén regel weg; met everything ook de andere afgeronde regels van hetzelfde item. Een opdracht die wacht
        of loopt blijft staan. Geeft (weg, item, foutmelding): de verwijderde opdrachten, het item en bij een
        weigering de reden in gewone woorden (dan is er niets weg)."""
        with lock:
            job = self.find(number)
            if job is None:
                return [], "", base.L("Deze opdracht staat niet (meer) in de lijst.", "This job is not in the list (any more).")
            if job.active:
                return [], "", base.L("Een opdracht die wacht of loopt, haal je zo niet weg.",
                                 "A job that is waiting or running cannot be removed this way.")
            item = job.item or ""
            if everything and item and any(j.item == item and j.active for j in self._items):
                return [], "", base.L("Er wacht of loopt nog een opdracht voor dit asset.",
                                 "A job for this asset is still waiting or running.")
            gone = [j for j in self._items if j is job or (everything and item and j.item == item)]
            self._items[:] = [j for j in self._items if not any(j is g for g in gone)]
            self.cleared += len(gone)
        return gone, item, ""

    def remove_source(self, source):
        """Alle regels van één bron weg (een Iconik die wordt weggehaald, met "ook van deze Mac"). Een opdracht die
        wacht of loopt blijft staan. Geeft het aantal."""
        prefix = source + ":"
        with lock:
            gone = [j for j in self._items if (j.item or "").startswith(prefix) and not j.active]
            self._items[:] = [j for j in self._items if not any(j is g for g in gone)]
            self.cleared += len(gone)
        return len(gone)

    # --- bewaren ---
    def payload(self):
        """Wat er in jobs.json komt. Wat buiten de bewaarde 500 valt telt mee als opgeruimd, anders krijgt een
        nieuwe opdracht na een herstart een nummer dat al bestaat."""
        with lock:
            rows = []
            for job in self._items[-MAX_KEPT:]:
                row = {key: getattr(job, key) for key in KEPT_FIELDS}
                for key in TIME_FIELDS:
                    value = getattr(job, key)
                    row[key] = value.isoformat() if value else None
                rows.append(row)
            dropped = max(0, len(self._items) - MAX_KEPT)
            return {"version": VERSION, "saved": datetime.now().isoformat(), "cleared": self.cleared + dropped, "jobs": rows}

    def save(self, path):
        """Schrijft de lijst weg. Mislukt dat, dan is de lijst alleen niet bewaard: nooit een reden om te stoppen."""
        try:
            payload = self.payload()
            with _save_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + ".tmp")
                temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                os.replace(temp, path)
        except Exception as error:
            print(f"De lijst met opdrachten bewaren mislukte: {error}")

    def load(self, path):
        """Haalt de bewaarde lijst terug, bij de start, vóór wat er nu al in staat. Wat nog liep of wachtte is
        afgebroken. Een kapot of ontbrekend bestand geeft niets. Zonder versie (tot stap 4): asset_id wordt het
        item van dat asset in de enige Iconik; een rode regel zonder asset blijft zonder item."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or not isinstance(data.get("jobs", []), list):
            return
        saved = _time(data.get("saved")) or datetime.now()
        old = data.get("version") is None
        loaded = []
        for row in data.get("jobs", []):
            try:
                if old:
                    asset = str(row["asset_id"])
                    item = (items.iconik(asset) or "") if asset else ""
                else:
                    item = str(row["item"] or "")
                    if item and not items.valid(item):
                        continue
                job = Job(n=int(row["n"]), item=item, title=row.get("title") or "",
                          status=row.get("status") or "fout", message=row.get("message") or "",
                          added=_time(row.get("added")) or saved, started=_time(row.get("started")),
                          finished=_time(row.get("finished")), tracks=row.get("tracks") or [],
                          duration=row.get("duration"), result=row.get("result"), language=row.get("language"),
                          auto=bool(row.get("auto")))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if job.active:
                job.status, job.finished = "afgebroken", saved
                job.message = base.L("Afgebroken: de app is herstart.", "Interrupted: the app was restarted.")
            loaded.append(job)
        with lock:
            self._items[:0] = loaded
            try:
                self.cleared = max(self.cleared, int(data.get("cleared") or 0))
            except (TypeError, ValueError):
                pass
