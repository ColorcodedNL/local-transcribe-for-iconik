"""Tests voor worker.py met een nagemaakte Iconik. Geen netwerk.

Draaien: python3 -B tests/test_worker.py
"""
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import worker  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(("OK    " if ok else "FOUT  ") + label + (f"  ({detail})" if detail and not ok else ""))


class Fake:
    """Doet alsof het Iconik is, voor de calls die worker.py gebruikt."""

    def __init__(self):
        self.fields = {}
        self.views = []
        self.values = {}      # asset -> {veld: waarde}
        self.dates = {}       # asset -> tijdstip waarop Iconik de status zette
        self.posts = []
        self.puts = []
        self.refuse_removal = False

    def get(self, path):
        if path == "metadata/v1/views/":
            return {"objects": self.views}
        if path == "metadata/v1/fields/":
            return {"objects": [{"name": n} for n in self.fields]}
        m = re.match(r"metadata/v1/fields/(.+?)/$", path)
        if m:
            return dict(self.fields[m.group(1)])
        m = re.match(r"metadata/v1/assets/(.+?)/assets/.+?/views/(.+?)/$", path)
        if m:
            vals = self.values.get(m.group(1), {})
            out = {k: {"field_values": [{"value": v}]} for k, v in vals.items()}
            if worker.FIELD in out and m.group(1) in self.dates:
                out[worker.FIELD]["date_created"] = self.dates[m.group(1)]
            return {"metadata_values": out}
        raise AssertionError("onbekende GET " + path)

    def post(self, path, body):
        self.posts.append((path, body))
        if path == "metadata/v1/fields/":
            self.fields[body["name"]] = body
            return body
        if path == "metadata/v1/views/":
            view = dict(body, id="view-1")
            self.views.append(view)
            return view
        if path.startswith("search/v1/search/"):
            term = body["filter"]["terms"][0]
            assert term["name"] == "metadata." + worker.FIELD
            ids = [a for a, v in self.values.items() if v.get(worker.FIELD) == term["value"]]
            return {"objects": [{"id": i} for i in ids]}
        raise AssertionError("onbekende POST " + path)

    def _call(self, method, path, json=None):
        self.puts.append((method, path, json))
        m = re.match(r"metadata/v1/fields/(.+?)/$", path)
        if m:
            if self.refuse_removal:
                old = {o["value"] for o in self.fields[m.group(1)].get("options") or []}
                if not old <= {o["value"] for o in json.get("options") or []}:
                    raise RuntimeError("HTTP 400: option in use")
            self.fields[m.group(1)].update(json)
            return {}
        if path.startswith("metadata/v1/views/"):
            for v in self.views:
                if path.endswith(v["id"] + "/"):
                    v.update(json)
            return {}
        m = re.match(r"metadata/v1/assets/(.+?)/assets/.+?/views/", path)
        assert method == "PUT" and m, path
        asset = self.values.setdefault(m.group(1), {})
        for name, item in json["metadata_values"].items():
            assert item["mode"] == "overwrite"
            asset[name] = item["field_values"][0]["value"]
        self.dates[m.group(1)] = datetime.now(timezone.utc).isoformat()
        return {}


def ready():
    fake = Fake()
    queue = worker.Queue(fake)
    queue.setup(create=True)
    return fake, queue


# --- opzet
fake = Fake()
queue = worker.Queue(fake)
try:
    queue.setup(create=False)
    check("zonder view en zonder create: fout", False)
except worker.WorkerError:
    check("zonder view en zonder create: WorkerError", True)

view_id = queue.setup(create=True)
check("create maakt beide velden", set(fake.fields) == {worker.FIELD, worker.INFO})
check("statusveld is een dropdown met vier opties",
      fake.fields[worker.FIELD]["field_type"] == "drop_down"
      and [o["value"] for o in fake.fields[worker.FIELD]["options"]] == list(worker.STATUSES))
check("create maakt de view met beide velden",
      view_id == "view-1" and [f["name"] for f in fake.views[0]["view_fields"]] == [worker.FIELD, worker.INFO])
before = len(fake.posts)
worker.Queue(fake).setup(create=True)
check("tweede keer create maakt niets opnieuw", len(fake.posts) == before)

fake2 = Fake()
fake2.fields[worker.FIELD] = {"name": worker.FIELD}
fake2.views.append({"id": "v9", "name": worker.VIEW, "view_fields": [{"name": worker.FIELD}]})
worker.Queue(fake2).setup(create=True)
check("bestaande view met een veld krijgt het tweede erbij",
      [f["name"] for f in fake2.views[0]["view_fields"]] == [worker.FIELD, worker.INFO]
      and worker.INFO in fake2.fields)

# --- zoeken
fake, queue = ready()
fake.values = {"a": {worker.FIELD: worker.REQUESTED}, "b": {worker.FIELD: worker.DONE}, "c": {worker.FIELD: worker.REQUESTED}}
check("find geeft alleen assets met die status", sorted(queue.find(worker.REQUESTED)) == ["a", "c"])

# --- claimen
fake, queue = ready()
fake.values["a"] = {worker.FIELD: worker.REQUESTED}
check("claim lukt als niemand anders ingrijpt (poging 1)", queue.claim("a", "testmac", sleep=lambda s: None) == 1)
status, info, _ = queue.read("a")
check("na claim: status bezig met onze naam", status == worker.BUSY and info.startswith("Bezig op testmac sinds "))
check("tweede werker kan een bezig asset niet claimen", not queue.claim("a", "othermac", sleep=lambda s: None))

fake, queue = ready()
fake.values["a"] = {worker.FIELD: worker.REQUESTED}


def other_wins(seconds):
    fake.values["a"] = {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op othermac sinds 10:00 (ffffff)"}


check("andere werker schrijft in de wachttijd: wij geven op", not queue.claim("a", "testmac", sleep=other_wins))
check("claim van een asset dat al klaar is: nee", not (
    fake.values.update(b={worker.FIELD: worker.DONE}) or queue.claim("b", "testmac", sleep=lambda s: None)))

# --- verlopen "bezig"
fake, queue = ready()
fake.values = {
    "old": {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op othermac sinds 10:00 (abc)"},
    "new": {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op othermac sinds 12:00 (abc)"},
    "nodate": {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op othermac sinds 08:00 (abc)"},
}
fake.dates = {"old": (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(),
              "new": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}
released = queue.release_stale("testmac")
check("alleen een te oude bezig-regel gaat terug", released == ["old"])
check("zonder tijd van Iconik blijft de regel staan", fake.values["nodate"][worker.FIELD] == worker.BUSY)
check("verse bezig-regel blijft staan", fake.values["new"][worker.FIELD] == worker.BUSY)
check("teruggezet asset heeft een uitleg",
      fake.values["old"][worker.FIELD] == worker.REQUESTED and "othermac reageerde niet meer" in fake.values["old"][worker.INFO])
check("uitleg bevat geen opmaakcode", "|" not in fake.values["old"][worker.INFO] and "T" not in fake.values["old"][worker.INFO].split(": ")[0])

# --- resultaat terugzetten
fake, queue = ready()
for status, message, expect, text in [
    ("klaar", "12 regels, 2 spreker(s)", worker.DONE, "12 regels"),
    ("overgeslagen", "Geen verstaanbare spraak", worker.DONE, "Overgeslagen: Geen verstaanbare spraak"),
    ("fout", "x" * 500, worker.FAILED, "xxx"),
]:
    fake.values["a"] = {worker.FIELD: worker.BUSY}
    worker.report(queue, {"asset_id": "a", "status": status, "message": message}, who="testmac")
    got = fake.values["a"]
    check(f"report {status}: status {expect}", got[worker.FIELD] == expect and text in got[worker.INFO])
    check(f"report {status}: begint met naam en lokale tijd",
          re.match(r"^testmac, \d\d-\d\d \d\d:\d\d: ", got[worker.INFO]) is not None, got[worker.INFO][:40])
check("lange foutmelding wordt afgekapt", len(fake.values["a"][worker.INFO].split(": ", 1)[1]) == 300)

# --- een rondje
fake, queue = ready()
fake.values = {"a": {worker.FIELD: worker.REQUESTED}, "b": {worker.FIELD: worker.REQUESTED}}
added = []
check("bezig: niets doen", worker.poll_once(queue, "testmac", lambda: True, lambda *a, **k: added.append(a)) is None
      and not added and not fake.puts)

real_claim = queue.claim
calls = []


def claim_skipping_first(asset_id, who, **kw):
    calls.append(asset_id)
    return False if len(calls) == 1 else real_claim(asset_id, who, **kw)


queue.claim = claim_skipping_first
taken = worker.poll_once(queue, "testmac", lambda: False, lambda asset_id, remote=False: added.append((asset_id, remote)),
                         sleep=lambda s: None)
check("rondje: eerste claim mislukt, tweede lukt", len(calls) == 2 and taken == calls[1])
check("opdracht wordt als remote toegevoegd, met het nummer van de poging", added == [(taken, 1)])

# --- fout van de Mac: terug de rij in, tot drie pogingen
fake, queue = ready()
fake.values["a"] = {worker.FIELD: worker.REQUESTED}
nap = lambda s: None
check("claim 1 is poging 1", queue.claim("a", "testmac", sleep=nap) == 1)
check("give_back na poging 1: terug in de rij", queue.give_back("a", "testmac", "Opslag niet gemount.", 1) is True
      and fake.values["a"][worker.FIELD] == worker.REQUESTED and "Mislukte poging 1 van 3" in fake.values["a"][worker.INFO])
check("uitleg bevat de reden zonder dubbele punten", ".." not in fake.values["a"][worker.INFO]
      and "Opslag niet gemount. Mislukte" in fake.values["a"][worker.INFO])
check("tweede claim is poging 2 en zegt dat", queue.claim("a", "othermac", sleep=nap) == 2
      and ", poging 2 van 3, laatste teken " in fake.values["a"][worker.INFO])
check("give_back na poging 2: terug in de rij", queue.give_back("a", "othermac", "Geen internet", 2) is True)
check("derde claim is poging 3", queue.claim("a", "thirdmac", sleep=nap) == 3)
check("give_back na poging 3: definitief fout met uitleg", queue.give_back("a", "testmac", "Geen internet", 3) is False
      and fake.values["a"][worker.FIELD] == worker.FAILED and "na 3 pogingen opgegeven" in fake.values["a"][worker.INFO])

# report: fout van de Mac tegenover fout van het asset
fake, queue = ready()
worker._pause[0] = 0.0
fake.values["a"] = {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op testmac sinds 13:00 (abc)"}
worker.report(queue, {"asset_id": "a", "status": "fout", "local": True, "remote": 1,
                      "message": "De map /Volumes/Opslag/Iconik is niet bereikbaar. Is de opslag (ISG) gemount?"}, who="testmac")
check("report: fout van de Mac zet het asset terug in de rij", fake.values["a"][worker.FIELD] == worker.REQUESTED)
check("  en pauzeert de werker", worker.paused() is True and re.match(r"^\d\d:\d\d$", worker.state["paused_until"]) is not None, worker.state)
fake.values["b"] = {worker.FIELD: worker.REQUESTED}
before = len(fake.puts)
check("  tijdens de pauze pakt poll_once niets op", worker.poll_once(queue, "testmac", lambda: False, lambda *a, **k: None, sleep=nap) is None
      and len(fake.puts) == before)
worker._pause[0] = 0.0
check("  na de pauze weer wel", worker.paused() is False and worker.state["paused_until"] is None)
fake.values["c"] = {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op testmac sinds 13:00 (abc)"}
worker.report(queue, {"asset_id": "c", "status": "fout", "local": False, "remote": 1, "message": "Kapot bestand"}, who="testmac")
check("report: fout van het asset blijft fout en pauzeert niet", fake.values["c"][worker.FIELD] == worker.FAILED and worker.paused() is False)
fake.values["d"] = {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op testmac sinds 13:00 (abc)"}
worker.report(queue, {"asset_id": "d", "status": "fout", "local": True, "remote": 3, "message": "Geen internet"}, who="testmac")
check("report: derde mislukte poging wordt fout", fake.values["d"][worker.FIELD] == worker.FAILED)
worker._pause[0] = 0.0

# verlopen bezig bij poging 3 is definitief
fake, queue = ready()
fake.values = {"x": {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op othermac sinds 10:00 (abc), poging 3 van 3"},
               "y": {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op othermac sinds 10:00 (abc)"}}
fake.dates = {k: (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat() for k in "xy"}
queue.release_stale("testmac")
check("verlopen bezig bij poging 3: fout", fake.values["x"][worker.FIELD] == worker.FAILED)
check("verlopen bezig bij poging 1: terug in de rij als mislukte poging 1",
      fake.values["y"][worker.FIELD] == worker.REQUESTED and "Mislukte poging 1 van 3" in fake.values["y"][worker.INFO])

# --- laatste teken en sneller overnemen
now = datetime.now(timezone.utc)
check("busy_info zet een laatste teken in UTC, 24 uur",
      re.search(r", laatste teken \d{4}-\d\d-\d\d \d\d:\d\dZ$", worker.busy_info("m", "abc123", 1)) is not None)
check("last_sign leest het terug", worker.last_sign("x, laatste teken 2026-10-14 14:17Z [mislukt op: a]")
      == datetime(2026, 10, 14, 14, 17, tzinfo=timezone.utc) and worker.last_sign("Bezig op m sinds 10:00 (abc123)") is None)
check("utc_stamp rekent een lokale tijd om naar UTC",
      worker.utc_stamp(datetime(2026, 10, 14, 16, 17, tzinfo=timezone(timedelta(hours=2)))) == "2026-10-14 14:17Z")
check("failed_on en clean_name", worker.failed_on("... [mislukt op: Mac A; Mac B]") == ["Mac A", "Mac B"]
      and worker.failed_on("niets") == [] and worker.clean_name("Mac; [x]") == "Mac x")

fake, queue = ready()
fake.values = {
    "fresh": {worker.FIELD: worker.BUSY, worker.INFO: worker.busy_info("othermac", "aaaaaa", 1, now=now - timedelta(minutes=10))
              .replace(worker.utc_stamp(now - timedelta(minutes=10)), worker.utc_stamp(now - timedelta(minutes=2)))},
    "dead": {worker.FIELD: worker.BUSY, worker.INFO: worker.busy_info("othermac", "bbbbbb", 1, now=now - timedelta(minutes=30))},
    "legacy": {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op oldmac sinds 10:00 (cccccc)"},
}
fake.dates = {"fresh": (now - timedelta(hours=1)).isoformat(), "dead": now.isoformat(),
              "legacy": (now - timedelta(hours=1)).isoformat()}
released = queue.release_stale("testmac")
check("laatste teken van 30 minuten geleden: terug in de rij, ook al is de status net gezet", released == ["dead"])
check("laatste teken van 2 minuten geleden blijft staan", fake.values["fresh"][worker.FIELD] == worker.BUSY)
check("oudere versie zonder laatste teken: pas na 3 uur", fake.values["legacy"][worker.FIELD] == worker.BUSY)
check("teruggezet asset noemt de Mac die wegviel", "[mislukt op: othermac]" in fake.values["dead"][worker.INFO]
      and "othermac reageerde niet meer" in fake.values["dead"][worker.INFO])

# het hartslagschrijven
fake, queue = ready()
worker.current.clear()
fake.values["a"] = {worker.FIELD: worker.REQUESTED}
added = []
taken = worker.poll_once(queue, "testmac", lambda: False, lambda asset_id, remote=False: added.append(asset_id), sleep=nap)
check("poll_once onthoudt het asset voor de heartbeat", taken == "a" and worker.current.get("asset") == "a"
      and worker.current.get("code") == worker.claim_code(fake.values["a"][worker.INFO]))
puts = len(fake.puts)
check("heartbeat meteen na de claim: nog niets schrijven", worker.beat(queue) is False and len(fake.puts) == puts)
worker.current["beat"] -= worker.HEARTBEAT + 1
later = now + timedelta(hours=1)
check("heartbeat na een minuut: schrijft een nieuw laatste teken", worker.beat(queue, now=later) is True
      and worker.utc_stamp(later) in fake.values["a"][worker.INFO] and fake.values["a"][worker.FIELD] == worker.BUSY)
check("  de claimcode blijft gelijk", worker.claim_code(fake.values["a"][worker.INFO]) == worker.current["code"])
check("  het schrijven raakt de status niet", fake.puts[-1][2]["metadata_values"].keys() == {worker.INFO})

fake.values["a"][worker.INFO] = "Bezig op othermac sinds 10:00 (ffffff)"
worker.current["beat"] -= worker.HEARTBEAT + 1
puts = len(fake.puts)
check("asset is van een ander geworden: stoppen met schrijven", worker.beat(queue) is False and not worker.current
      and len(fake.puts) == puts)

fake.values["a"] = {worker.FIELD: worker.REQUESTED}
worker.poll_once(queue, "testmac", lambda: False, lambda *a, **k: None, sleep=nap)
worker.current["beat"] -= worker.HEARTBEAT + 1
worker.report(queue, {"asset_id": "a", "status": "klaar", "message": "3 regels"}, who="testmac")
check("report ruimt het asset op", not worker.current)
check("heartbeat na het resultaat overschrijft het niet", worker.beat(queue) is False
      and fake.values["a"][worker.FIELD] == worker.DONE and "3 regels" in fake.values["a"][worker.INFO])

# paden komen niet in het detailveld
check("scrub: bestand niet gevonden", "/Volumes" not in worker.scrub("Bestand niet gevonden: /Volumes/Archief/Iconik/Map met spatie/clip.mp4")
      and worker.scrub("Bestand niet gevonden: /Volumes/A/b c.mp4") == "Bestand niet gevonden.")
check("scrub: map niet bereikbaar", worker.scrub("De map /Volumes/Archief/Iconik is niet bereikbaar. Is de opslag (ISG) gemount?")
      == "De map is niet bereikbaar. Is de opslag (ISG) gemount?" and "Archief" not in worker.scrub("The folder /Volumes/Archief/Iconik cannot be reached. Is it mounted?"))
check("scrub: pad tussen aanhalingstekens en los pad", "/Users" not in worker.scrub("[Errno 2] No such file or directory: '/Users/x y/z.wav'")
      and "/private" not in worker.scrub("Fout in /private/var/folders/ab/cd.tmp daarna") and worker.scrub("Geen internet.") == "Geen internet.")
fake.values["p"] = {worker.FIELD: worker.BUSY, worker.INFO: "x"}
queue.finish("p", "testmac", False, "Bestand niet gevonden: /Volumes/Archief/Iconik/geheim/clip.mp4")
check("finish schrijft geen pad", "/Volumes" not in fake.values["p"][worker.INFO] and "testmac" in fake.values["p"][worker.INFO])
queue.give_back("p", "testmac", "De map /Volumes/Archief/Iconik is niet bereikbaar. Is de opslag (ISG) gemount?", 1)
check("give_back schrijft geen pad", "/Volumes" not in fake.values["p"][worker.INFO] and "Archief" not in fake.values["p"][worker.INFO])

# een Mac die zijn asset kwijt is schrijft er niet meer overheen
def claim_a():
    fake.values["a"] = {worker.FIELD: worker.REQUESTED}
    worker.poll_once(queue, "testmac", lambda: False, lambda *a, **k: None, sleep=nap)

claim_a()
fake.values["a"] = {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op othermac sinds 10:00 (ffffff)"}
puts = len(fake.puts)
job = {"asset_id": "a", "status": "klaar", "message": "3 regels"}
worker.report(queue, job, who="testmac")
check("overgenomen asset: report schrijft niets", len(fake.puts) == puts
      and fake.values["a"][worker.FIELD] == worker.BUSY and "othermac" in fake.values["a"][worker.INFO])
check("  de job krijgt een notitie en de heartbeat staat uit", ("overgenomen" in job["message"] or "taken over" in job["message"])
      and not worker.current)

claim_a()
fake.values["a"] = {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op othermac sinds 10:00 (ffffff)"}
worker.current["beat"] -= worker.HEARTBEAT + 1
check("heartbeat merkt het verlies op", worker.beat(queue) is False and "a" in worker.lost)
puts = len(fake.puts)
worker.report(queue, {"asset_id": "a", "status": "fout", "local": True, "remote": 1, "message": "Geen internet"}, who="testmac")
check("  daarna schrijft report ook niets, maar de Mac pauzeert wel", len(fake.puts) == puts and worker.paused()
      and "a" not in worker.lost)
worker.resume()

claim_a()
puts = len(fake.puts)
worker.report(queue, {"asset_id": "a", "status": "klaar", "message": "5 regels"}, who="testmac")
check("nog van ons: report schrijft het resultaat", len(fake.puts) == puts + 1 and fake.values["a"][worker.FIELD] == worker.DONE
      and "5 regels" in fake.values["a"][worker.INFO])

# een job die niets meer meldt: de heartbeat stopt, het laatste teken blijft staan
fake, queue = ready()
worker.current.clear()
fake.values["a"] = {worker.FIELD: worker.REQUESTED}
worker.poll_once(queue, "testmac", lambda: False, lambda *a, **k: None, sleep=nap)
worker.current["beat"] -= worker.HEARTBEAT + 1
puts = len(fake.puts)
check("heartbeat zonder voortgang: niets schrijven, het asset blijft van ons",
      worker.beat(queue, now=later, healthy=lambda: False) is False and len(fake.puts) == puts and worker.current.get("asset") == "a")
check("heartbeat met voortgang: wel schrijven",
      worker.beat(queue, now=later, healthy=lambda: True) is True and len(fake.puts) == puts + 1)
worker.current.clear()

# de eigen Mac probeert het niet meteen opnieuw
fake, queue = ready()
worker.current.clear()
fake.values["a"] = {worker.FIELD: worker.REQUESTED}
check("claim door testmac", queue.claim("a", "testmac", sleep=nap) == 1)
queue.give_back("a", "testmac", "Opslag niet gemount.", 1)
check("na de fout staat de Mac in de lijst", "[mislukt op: testmac]" in fake.values["a"][worker.INFO])
check("dezelfde Mac slaat het asset over", queue.claim("a", "testmac", sleep=nap) == 0
      and fake.values["a"][worker.FIELD] == worker.REQUESTED)
check("een andere Mac pakt het wel, de lijst blijft", queue.claim("a", "othermac", sleep=nap) == 2
      and "[mislukt op: testmac]" in fake.values["a"][worker.INFO])
queue.give_back("a", "othermac", "Geen internet", 2)
check("lijst groeit met de tweede Mac", "[mislukt op: testmac; othermac]" in fake.values["a"][worker.INFO])
fake.dates["a"] = (datetime.now(timezone.utc) - timedelta(minutes=worker.RETRY_ELSEWHERE // 60 + 1)).isoformat()
check("na tien minuten mag de eerste Mac het weer proberen (ook als er maar een Mac is)",
      queue.claim("a", "testmac", sleep=nap) == 3)
worker.current.clear()

# --- Engelse waarden, labels per taal en omzetten van 0.3.1
fake, queue = ready()
check("nieuw veld: Engelse waarden met Nederlandse labels",
      [(o["value"], o["label"]) for o in fake.fields[worker.FIELD]["options"]]
      == [("requested", "Aangevraagd"), ("in_progress", "Bezig"), ("done", "Klaar"), ("failed", "Fout")])
check("nieuw veld: Engelse omschrijving en label", fake.fields[worker.FIELD]["label"] == "Transcriptie"
      and fake.fields[worker.FIELD]["description"].startswith("Queue status"))
fake_en = Fake()
worker.Queue(fake_en).setup(create=True, language="en")
check("in het Engels: labels en veldnaam",
      [o["label"] for o in fake_en.fields[worker.FIELD]["options"]] == ["Requested", "In progress", "Done", "Failed"]
      and fake_en.fields[worker.FIELD]["label"] == "Transcription"
      and fake_en.fields[worker.INFO]["label"] == "Transcription details"
      and [f["label"] for f in fake_en.views[0]["view_fields"]] == ["Transcription", "Transcription details"])

old = Fake()
old.fields[worker.FIELD] = {"name": worker.FIELD, "label": "Transcriptie", "description": None, "max_value": None,
                            "options": [{"label": v, "value": v} for v in ("aangevraagd", "bezig", "klaar", "fout")]}
old.fields[worker.INFO] = {"name": worker.INFO}
old.views.append({"id": "v1", "name": worker.VIEW, "view_fields": [{"name": worker.FIELD}, {"name": worker.INFO}]})
worker.Queue(old).setup(create=True)
check("veld van 0.3.1 wordt omgezet naar de nieuwe waarden",
      [o["value"] for o in old.fields[worker.FIELD]["options"]] == list(worker.STATUSES)
      and old.fields[worker.FIELD]["options"][0]["label"] == "Aangevraagd")
put_body = [p for p in old.puts if p[1].endswith(worker.FIELD + "/")][0][2]
check("bijwerken stuurt geen lege waarden mee", None not in put_body.values() and "date_created" not in put_body, put_body)
before = len(old.puts)
worker.Queue(old).setup(create=True)
check("een tweede keer opstarten schrijft niets meer", len(old.puts) == before)

stubborn = Fake()
stubborn.refuse_removal = True
stubborn.fields[worker.FIELD] = {"name": worker.FIELD, "options": [{"label": v, "value": v} for v in ("aangevraagd", "bezig", "klaar", "fout")]}
stubborn.fields[worker.INFO] = {"name": worker.INFO}
stubborn.views.append({"id": "v1", "name": worker.VIEW, "view_fields": [{"name": worker.FIELD}, {"name": worker.INFO}]})
worker.Queue(stubborn).setup(create=True)
values = [o["value"] for o in stubborn.fields[worker.FIELD]["options"]]
check("weigert Iconik opties weg te halen: de nieuwe komen erbij, de oude blijven",
      values[:4] == list(worker.STATUSES) and set(values[4:]) == {"aangevraagd", "bezig", "klaar", "fout"}, values)
before = len(stubborn.puts)
worker.Queue(stubborn).setup(create=True)
check("  en een tweede keer opstarten schrijft niets meer", len(stubborn.puts) == before)

fake, queue = ready()
fake.values = {"oud": {worker.FIELD: "aangevraagd"}, "nieuw": {worker.FIELD: worker.REQUESTED}, "klaar": {worker.FIELD: "klaar"}}
check("zoeken vindt ook de waarde van 0.3.1", sorted(queue.find(worker.REQUESTED)) == ["nieuw", "oud"])
check("lezen zet de oude waarde om", queue.read("oud")[0] == worker.REQUESTED and queue.read("klaar")[0] == worker.DONE)
check("claimen van een oude aanvraag schrijft de nieuwe waarde", queue.claim("oud", "testmac", sleep=nap) == 1
      and fake.values["oud"][worker.FIELD] == worker.BUSY)
fake.values["oud2"] = {worker.FIELD: "bezig", worker.INFO: "Bezig op oldmac sinds 10:00 (cccccc)"}
fake.dates["oud2"] = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
check("een oude bezig-regel van 4 uur gaat terug", "oud2" in queue.release_stale("testmac")
      and fake.values["oud2"][worker.FIELD] == worker.REQUESTED)

fake, queue = ready()
queue.apply_language("en")
check("taal omzetten: veld, opties en view",
      fake.fields[worker.FIELD]["label"] == "Transcription"
      and [o["label"] for o in fake.fields[worker.FIELD]["options"]] == ["Requested", "In progress", "Done", "Failed"]
      and [o["value"] for o in fake.fields[worker.FIELD]["options"]] == list(worker.STATUSES)
      and fake.fields[worker.INFO]["label"] == "Transcription details"
      and [f["label"] for f in fake.views[0]["view_fields"]] == ["Transcription", "Transcription details"])

# --- tekst in het detailveld in het Engels, en gemengd lezen
worker.set_language("en")
try:
    info = worker.busy_info("othermac", "abc123", 2, ["macone"])
    check("Engelse claimtekst", info.startswith("Working on othermac since ") and ", attempt 2 of 3, last sign " in info
          and info.endswith("[failed on: macone]"), info)
    check("Engelse tekst terug te lezen", worker.busy_owner(info) == "othermac" and worker.current_attempt(info) == 2
          and worker.last_sign(info) is not None and worker.failed_on(info) == ["macone"] and worker.claim_code(info) == "abc123")
    check("Nederlandse tekst terug te lezen met Engels aan", worker.busy_owner("Bezig op Mac Studio sinds 12:59 (a1b2c3)") == "Mac Studio"
          and worker.failed_attempts("x. Mislukte poging 2 van 3, terug in de rij") == 2)
    fake, queue = ready()
    fake.values["a"] = {worker.FIELD: worker.REQUESTED}
    check("claim in het Engels", queue.claim("a", "enmac", sleep=nap) == 1 and fake.values["a"][worker.INFO].startswith("Working on enmac"))
    queue.give_back("a", "enmac", "Storage not mounted", 1)
    check("give_back in het Engels", "Failed attempt 1 of 3, back in the queue [failed on: enmac]" in fake.values["a"][worker.INFO]
          and worker.failed_attempts(fake.values["a"][worker.INFO]) == 1, fake.values["a"][worker.INFO])
    check("een Nederlandse Mac telt de Engelse mislukte poging mee", worker.failed_on(fake.values["a"][worker.INFO]) == ["enmac"])
    fake.values["a"][worker.FIELD] = worker.BUSY
    fake.values["a"][worker.INFO] = worker.busy_info("enmac", "dddddd", 3)
    fake.dates["a"] = datetime.now(timezone.utc).isoformat()
    queue.give_back("a", "enmac", "Out of memory", 3)
    check("derde poging in het Engels", fake.values["a"][worker.FIELD] == worker.FAILED
          and "gave up after 3 attempts" in fake.values["a"][worker.INFO])
    fake.values["b"] = {worker.FIELD: worker.BUSY, worker.INFO: worker.busy_info("deadmac", "eeeeee", 1,
                        now=datetime.now(timezone.utc) - timedelta(minutes=30))}
    worker.set_language("nl")
    queue.release_stale("nlmac")
    check("overname door een Nederlandse Mac van een Engelse regel", fake.values["b"][worker.FIELD] == worker.REQUESTED
          and "deadmac reageerde niet meer" in fake.values["b"][worker.INFO])
    worker.set_language("en")
    fake.values["c"] = {worker.FIELD: worker.BUSY, worker.INFO: worker.busy_info("deadmac", "ffffff", 1,
                        now=datetime.now(timezone.utc) - timedelta(minutes=30))}
    queue.release_stale("enmac")
    check("overname in het Engels", "deadmac stopped responding" in fake.values["c"][worker.INFO])
    fake.values["d"] = {worker.FIELD: worker.BUSY, worker.INFO: "x"}
    worker.report(queue, {"asset_id": "d", "status": "overgeslagen", "message": "No speech"}, who="enmac")
    check("overgeslagen in het Engels", "Skipped: No speech" in fake.values["d"][worker.INFO])
finally:
    worker.set_language("nl")
check("onbekende taal valt terug op Nederlands", (worker.set_language("fr"), worker.language[0])[1] == "nl")

# --- de lus
log = []
stop = threading.Event()
count = [0]
asked = []


def enabled_off():
    count[0] += 1
    if count[0] >= 2:
        stop.set()
    return False


worker.run(lambda: asked.append(1), enabled_off, lambda: False, lambda *a, **k: None, stop, log.append, interval=0)
check("lus uit: get_client nooit aangeroepen", not asked and worker.state["enabled"] is False)

stop = threading.Event()
count = [0]


def enabled_on():
    count[0] += 1
    if count[0] >= 2:
        stop.set()
    return True


worker.run(lambda: None, enabled_on, lambda: False, lambda *a, **k: None, stop, log.append, interval=0)
check("lus aan maar niet ingelogd: fout in de status", "Niet ingelogd" in worker.state["error"])

stop = threading.Event()
count = [0]


def boom():
    raise RuntimeError("netwerk weg")


worker.run(boom, enabled_on, lambda: False, lambda *a, **k: None, stop, log.append, interval=0)
check("fout in een rondje stopt de lus niet", count[0] >= 2 and "netwerk weg" in worker.state["error"] and any("netwerk weg" in l for l in log))

# --- hulpfuncties
check("age_seconds onleesbaar is None", worker.age_seconds("niet") is None and worker.age_seconds(None) is None)
check("age_seconds leest een tijd van Iconik", abs(worker.age_seconds("2026-10-14T10:37:36.882000+00:00",
      datetime(2026, 10, 14, 10, 47, 36, 882000, tzinfo=timezone.utc)) - 600) < 0.01)
check("busy_owner leest de naam", worker.busy_owner("Bezig op Mac Studio sinds 12:59 (a1b2c3)") == "Mac Studio" and worker.busy_owner("x") == "")
check("stamp is lokale tijd, dag-maand uur:minuut", worker.stamp(datetime(2026, 10, 14, 12, 59)) == "14-10 12:59")
name = worker.machine_name()
check("machine_name is een leesbare naam", bool(name) and name.isprintable())

fake.values["q"] = {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op testmac sinds 13:00 (abc)"}
queue.give_back("q", "testmac", "Is de opslag (ISG) gemount?", 1)
check("geen punt na een vraagteken", "gemount? Mislukte poging 1 van 3" in fake.values["q"][worker.INFO], fake.values["q"][worker.INFO])

print()
print(f"{sum(results)} van {len(results)} goed")
sys.exit(0 if all(results) else 1)
