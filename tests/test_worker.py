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

    def get(self, path):
        if path == "metadata/v1/views/":
            return {"objects": self.views}
        if path == "metadata/v1/fields/":
            return {"objects": [{"name": n} for n in self.fields]}
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
fake.values = {"a": {worker.FIELD: "aangevraagd"}, "b": {worker.FIELD: "klaar"}, "c": {worker.FIELD: "aangevraagd"}}
check("find geeft alleen assets met die status", sorted(queue.find("aangevraagd")) == ["a", "c"])

# --- claimen
fake, queue = ready()
fake.values["a"] = {worker.FIELD: "aangevraagd"}
check("claim lukt als niemand anders ingrijpt (poging 1)", queue.claim("a", "testmac", sleep=lambda s: None) == 1)
status, info, _ = queue.read("a")
check("na claim: status bezig met onze naam", status == "bezig" and info.startswith("Bezig op testmac sinds "))
check("tweede werker kan een bezig asset niet claimen", not queue.claim("a", "othermac", sleep=lambda s: None))

fake, queue = ready()
fake.values["a"] = {worker.FIELD: "aangevraagd"}


def other_wins(seconds):
    fake.values["a"] = {worker.FIELD: "bezig", worker.INFO: "Bezig op othermac sinds 10:00 (ffffff)"}


check("andere werker schrijft in de wachttijd: wij geven op", not queue.claim("a", "testmac", sleep=other_wins))
check("claim van een asset dat al klaar is: nee", not (
    fake.values.update(b={worker.FIELD: "klaar"}) or queue.claim("b", "testmac", sleep=lambda s: None)))

# --- verlopen "bezig"
fake, queue = ready()
fake.values = {
    "old": {worker.FIELD: "bezig", worker.INFO: "Bezig op othermac sinds 10:00 (abc)"},
    "new": {worker.FIELD: "bezig", worker.INFO: "Bezig op othermac sinds 12:00 (abc)"},
    "nodate": {worker.FIELD: "bezig", worker.INFO: "Bezig op othermac sinds 08:00 (abc)"},
}
fake.dates = {"old": (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(),
              "new": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}
released = queue.release_stale("testmac")
check("alleen een te oude bezig-regel gaat terug", released == ["old"])
check("zonder tijd van Iconik blijft de regel staan", fake.values["nodate"][worker.FIELD] == "bezig")
check("verse bezig-regel blijft staan", fake.values["new"][worker.FIELD] == "bezig")
check("teruggezet asset heeft een uitleg",
      fake.values["old"][worker.FIELD] == "aangevraagd" and "othermac reageerde niet meer" in fake.values["old"][worker.INFO])
check("uitleg bevat geen opmaakcode", "|" not in fake.values["old"][worker.INFO] and "T" not in fake.values["old"][worker.INFO].split(": ")[0])

# --- resultaat terugzetten
fake, queue = ready()
for status, message, expect, text in [
    ("klaar", "12 regels, 2 spreker(s)", "klaar", "12 regels"),
    ("overgeslagen", "Geen verstaanbare spraak", "klaar", "Overgeslagen: Geen verstaanbare spraak"),
    ("fout", "x" * 500, "fout", "xxx"),
]:
    fake.values["a"] = {worker.FIELD: "bezig"}
    worker.report(queue, {"asset_id": "a", "status": status, "message": message}, who="testmac")
    got = fake.values["a"]
    check(f"report {status}: status {expect}", got[worker.FIELD] == expect and text in got[worker.INFO])
    check(f"report {status}: begint met naam en lokale tijd",
          re.match(r"^testmac, \d\d-\d\d \d\d:\d\d: ", got[worker.INFO]) is not None, got[worker.INFO][:40])
check("lange foutmelding wordt afgekapt", len(fake.values["a"][worker.INFO].split(": ", 1)[1]) == 300)

# --- een rondje
fake, queue = ready()
fake.values = {"a": {worker.FIELD: "aangevraagd"}, "b": {worker.FIELD: "aangevraagd"}}
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
fake.values["a"] = {worker.FIELD: "aangevraagd"}
nap = lambda s: None
check("claim 1 is poging 1", queue.claim("a", "testmac", sleep=nap) == 1)
check("give_back na poging 1: terug in de rij", queue.give_back("a", "testmac", "Opslag niet gemount.", 1) is True
      and fake.values["a"][worker.FIELD] == "aangevraagd" and "Mislukte poging 1 van 3" in fake.values["a"][worker.INFO])
check("uitleg bevat de reden zonder dubbele punten", ".." not in fake.values["a"][worker.INFO]
      and "Opslag niet gemount. Mislukte" in fake.values["a"][worker.INFO])
check("tweede claim is poging 2 en zegt dat", queue.claim("a", "othermac", sleep=nap) == 2
      and ", poging 2 van 3, laatste teken " in fake.values["a"][worker.INFO])
check("give_back na poging 2: terug in de rij", queue.give_back("a", "othermac", "Geen internet", 2) is True)
check("derde claim is poging 3", queue.claim("a", "thirdmac", sleep=nap) == 3)
check("give_back na poging 3: definitief fout met uitleg", queue.give_back("a", "testmac", "Geen internet", 3) is False
      and fake.values["a"][worker.FIELD] == "fout" and "na 3 pogingen opgegeven" in fake.values["a"][worker.INFO])

# report: fout van de Mac tegenover fout van het asset
fake, queue = ready()
worker._pause[0] = 0.0
fake.values["a"] = {worker.FIELD: "bezig", worker.INFO: "Bezig op testmac sinds 13:00 (abc)"}
worker.report(queue, {"asset_id": "a", "status": "fout", "local": True, "remote": 1,
                      "message": "De map /Volumes/Opslag/Iconik is niet bereikbaar. Is de opslag (ISG) gemount?"}, who="testmac")
check("report: fout van de Mac zet het asset terug in de rij", fake.values["a"][worker.FIELD] == "aangevraagd")
check("  en pauzeert de werker", worker.paused() is True and re.match(r"^\d\d:\d\d$", worker.state["paused_until"]) is not None, worker.state)
fake.values["b"] = {worker.FIELD: "aangevraagd"}
before = len(fake.puts)
check("  tijdens de pauze pakt poll_once niets op", worker.poll_once(queue, "testmac", lambda: False, lambda *a, **k: None, sleep=nap) is None
      and len(fake.puts) == before)
worker._pause[0] = 0.0
check("  na de pauze weer wel", worker.paused() is False and worker.state["paused_until"] is None)
fake.values["c"] = {worker.FIELD: "bezig", worker.INFO: "Bezig op testmac sinds 13:00 (abc)"}
worker.report(queue, {"asset_id": "c", "status": "fout", "local": False, "remote": 1, "message": "Kapot bestand"}, who="testmac")
check("report: fout van het asset blijft fout en pauzeert niet", fake.values["c"][worker.FIELD] == "fout" and worker.paused() is False)
fake.values["d"] = {worker.FIELD: "bezig", worker.INFO: "Bezig op testmac sinds 13:00 (abc)"}
worker.report(queue, {"asset_id": "d", "status": "fout", "local": True, "remote": 3, "message": "Geen internet"}, who="testmac")
check("report: derde mislukte poging wordt fout", fake.values["d"][worker.FIELD] == "fout")
worker._pause[0] = 0.0

# verlopen bezig bij poging 3 is definitief
fake, queue = ready()
fake.values = {"x": {worker.FIELD: "bezig", worker.INFO: "Bezig op othermac sinds 10:00 (abc), poging 3 van 3"},
               "y": {worker.FIELD: "bezig", worker.INFO: "Bezig op othermac sinds 10:00 (abc)"}}
fake.dates = {k: (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat() for k in "xy"}
queue.release_stale("testmac")
check("verlopen bezig bij poging 3: fout", fake.values["x"][worker.FIELD] == "fout")
check("verlopen bezig bij poging 1: terug in de rij als mislukte poging 1",
      fake.values["y"][worker.FIELD] == "aangevraagd" and "Mislukte poging 1 van 3" in fake.values["y"][worker.INFO])

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
    "fresh": {worker.FIELD: "bezig", worker.INFO: worker.busy_info("othermac", "aaaaaa", 1, now=now - timedelta(minutes=10))
              .replace(worker.utc_stamp(now - timedelta(minutes=10)), worker.utc_stamp(now - timedelta(minutes=2)))},
    "dead": {worker.FIELD: "bezig", worker.INFO: worker.busy_info("othermac", "bbbbbb", 1, now=now - timedelta(minutes=30))},
    "legacy": {worker.FIELD: "bezig", worker.INFO: "Bezig op oldmac sinds 10:00 (cccccc)"},
}
fake.dates = {"fresh": (now - timedelta(hours=1)).isoformat(), "dead": now.isoformat(),
              "legacy": (now - timedelta(hours=1)).isoformat()}
released = queue.release_stale("testmac")
check("laatste teken van 30 minuten geleden: terug in de rij, ook al is de status net gezet", released == ["dead"])
check("laatste teken van 2 minuten geleden blijft staan", fake.values["fresh"][worker.FIELD] == "bezig")
check("oudere versie zonder laatste teken: pas na 3 uur", fake.values["legacy"][worker.FIELD] == "bezig")
check("teruggezet asset noemt de Mac die wegviel", "[mislukt op: othermac]" in fake.values["dead"][worker.INFO]
      and "othermac reageerde niet meer" in fake.values["dead"][worker.INFO])

# het hartslagschrijven
fake, queue = ready()
worker.current.clear()
fake.values["a"] = {worker.FIELD: "aangevraagd"}
added = []
taken = worker.poll_once(queue, "testmac", lambda: False, lambda asset_id, remote=False: added.append(asset_id), sleep=nap)
check("poll_once onthoudt het asset voor de heartbeat", taken == "a" and worker.current.get("asset") == "a"
      and worker.current.get("code") == worker.claim_code(fake.values["a"][worker.INFO]))
puts = len(fake.puts)
check("heartbeat meteen na de claim: nog niets schrijven", worker.beat(queue) is False and len(fake.puts) == puts)
worker.current["beat"] -= worker.HEARTBEAT + 1
later = now + timedelta(hours=1)
check("heartbeat na een minuut: schrijft een nieuw laatste teken", worker.beat(queue, now=later) is True
      and worker.utc_stamp(later) in fake.values["a"][worker.INFO] and fake.values["a"][worker.FIELD] == "bezig")
check("  de claimcode blijft gelijk", worker.claim_code(fake.values["a"][worker.INFO]) == worker.current["code"])
check("  het schrijven raakt de status niet", fake.puts[-1][2]["metadata_values"].keys() == {worker.INFO})

fake.values["a"][worker.INFO] = "Bezig op othermac sinds 10:00 (ffffff)"
worker.current["beat"] -= worker.HEARTBEAT + 1
puts = len(fake.puts)
check("asset is van een ander geworden: stoppen met schrijven", worker.beat(queue) is False and not worker.current
      and len(fake.puts) == puts)

fake.values["a"] = {worker.FIELD: "aangevraagd"}
worker.poll_once(queue, "testmac", lambda: False, lambda *a, **k: None, sleep=nap)
worker.current["beat"] -= worker.HEARTBEAT + 1
worker.report(queue, {"asset_id": "a", "status": "klaar", "message": "3 regels"}, who="testmac")
check("report ruimt het asset op", not worker.current)
check("heartbeat na het resultaat overschrijft het niet", worker.beat(queue) is False
      and fake.values["a"][worker.FIELD] == "klaar" and "3 regels" in fake.values["a"][worker.INFO])

# de eigen Mac probeert het niet meteen opnieuw
fake, queue = ready()
worker.current.clear()
fake.values["a"] = {worker.FIELD: "aangevraagd"}
check("claim door testmac", queue.claim("a", "testmac", sleep=nap) == 1)
queue.give_back("a", "testmac", "Opslag niet gemount.", 1)
check("na de fout staat de Mac in de lijst", "[mislukt op: testmac]" in fake.values["a"][worker.INFO])
check("dezelfde Mac slaat het asset over", queue.claim("a", "testmac", sleep=nap) == 0
      and fake.values["a"][worker.FIELD] == "aangevraagd")
check("een andere Mac pakt het wel, de lijst blijft", queue.claim("a", "othermac", sleep=nap) == 2
      and "[mislukt op: testmac]" in fake.values["a"][worker.INFO])
queue.give_back("a", "othermac", "Geen internet", 2)
check("lijst groeit met de tweede Mac", "[mislukt op: testmac; othermac]" in fake.values["a"][worker.INFO])
fake.dates["a"] = (datetime.now(timezone.utc) - timedelta(minutes=worker.RETRY_ELSEWHERE // 60 + 1)).isoformat()
check("na tien minuten mag de eerste Mac het weer proberen (ook als er maar een Mac is)",
      queue.claim("a", "testmac", sleep=nap) == 3)
worker.current.clear()

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

fake.values["q"] = {worker.FIELD: "bezig", worker.INFO: "Bezig op testmac sinds 13:00 (abc)"}
queue.give_back("q", "testmac", "Is de opslag (ISG) gemount?", 1)
check("geen punt na een vraagteken", "gemount? Mislukte poging 1 van 3" in fake.values["q"][worker.INFO], fake.values["q"][worker.INFO])

print()
print(f"{sum(results)} van {len(results)} goed")
sys.exit(0 if all(results) else 1)
