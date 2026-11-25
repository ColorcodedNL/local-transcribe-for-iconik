"""Tests voor de wachtrij in Iconik (iconik_queue.py, tot stap 5 worker.py) en de lus in worker.py, met een
nagemaakte Iconik. Geen netwerk.

Draaien: python3 -B tests/test_worker.py
"""
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import iconik_queue as worker  # noqa: E402  (de rij; de naam worker bleef om de tests leesbaar te houden)
import worker as loop  # noqa: E402  (de lus en de naam van deze Mac)
from jobs import Job  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from fake_queue_iconik import Fake  # noqa: E402  (de nagemaakte Iconik, ook voor test_two_iconiks.py)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(("OK    " if ok else "FOUT  ") + label + (f"  ({detail})" if detail and not ok else ""))


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
    worker.report(queue, Job(**{"asset_id": "a", "status": status, "message": message}), who="testmac")
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
requeued = worker.report(queue, Job(**{"asset_id": "a", "status": "fout", "local": True, "remote": 1,
                      "message": "De map /Volumes/Opslag/Iconik is niet bereikbaar. Is de opslag (ISG) gemount?"}), who="testmac")
check("report: fout van de Mac zet het asset terug in de rij", fake.values["a"][worker.FIELD] == worker.REQUESTED)
check("  report zegt dat het asset teruggezet is", requeued == "requeued")
check("  en pauzeert de werker", worker.paused() is True and re.match(r"^\d\d:\d\d$", worker.state["paused_until"]) is not None, worker.state)
fake.values["b"] = {worker.FIELD: worker.REQUESTED}
before = len(fake.puts)
check("  tijdens de pauze pakt poll_once niets op", worker.poll_once(queue, "testmac", lambda: False, lambda *a, **k: None, sleep=nap) is None
      and len(fake.puts) == before)
worker._pause[0] = 0.0
check("  na de pauze weer wel", worker.paused() is False and worker.state["paused_until"] is None)
fake.values["c"] = {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op testmac sinds 13:00 (abc)"}
worker.report(queue, Job(**{"asset_id": "c", "status": "fout", "local": False, "remote": 1, "message": "Kapot bestand"}), who="testmac")
check("report: fout van het asset blijft fout en pauzeert niet", fake.values["c"][worker.FIELD] == worker.FAILED and worker.paused() is False)
fake.values["d"] = {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op testmac sinds 13:00 (abc)"}
gave_up = worker.report(queue, Job(**{"asset_id": "d", "status": "fout", "local": True, "remote": 3, "message": "Geen internet"}), who="testmac")
check("report: derde mislukte poging wordt fout, niet teruggezet", fake.values["d"][worker.FIELD] == worker.FAILED and gave_up is None)
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
worker.report(queue, Job(**{"asset_id": "a", "status": "klaar", "message": "3 regels"}), who="testmac")
check("report ruimt het asset op", not worker.current)
check("heartbeat na het resultaat overschrijft het niet", worker.beat(queue) is False
      and fake.values["a"][worker.FIELD] == worker.DONE and "3 regels" in fake.values["a"][worker.INFO])

# paden komen niet in het detailveld
check("scrub: bestand niet gevonden", "/Volumes" not in worker.scrub("Bestand niet gevonden: /Volumes/Voorbeeld/Iconik/Map met spatie/clip.mp4")
      and worker.scrub("Bestand niet gevonden: /Volumes/A/b c.mp4") == "Bestand niet gevonden.")
check("scrub: map niet bereikbaar", worker.scrub("De map /Volumes/Voorbeeld/Iconik is niet bereikbaar. Is de opslag (ISG) gemount?")
      == "De map is niet bereikbaar. Is de opslag (ISG) gemount?" and "Voorbeeld" not in worker.scrub("The folder /Volumes/Voorbeeld/Iconik cannot be reached. Is it mounted?"))
check("scrub: pad tussen aanhalingstekens en los pad", "/Users" not in worker.scrub("[Errno 2] No such file or directory: '/Users/x y/z.wav'")
      and "/private" not in worker.scrub("Fout in /private/var/folders/ab/cd.tmp daarna") and worker.scrub("Geen internet.") == "Geen internet.")
fake.values["p"] = {worker.FIELD: worker.BUSY, worker.INFO: "x"}
queue.finish("p", "testmac", False, "Bestand niet gevonden: /Volumes/Voorbeeld/Iconik/geheim/clip.mp4")
check("finish schrijft geen pad", "/Volumes" not in fake.values["p"][worker.INFO] and "testmac" in fake.values["p"][worker.INFO])
queue.give_back("p", "testmac", "De map /Volumes/Voorbeeld/Iconik is niet bereikbaar. Is de opslag (ISG) gemount?", 1)
check("give_back schrijft geen pad", "/Volumes" not in fake.values["p"][worker.INFO] and "Voorbeeld" not in fake.values["p"][worker.INFO])

# eerdere mislukte pogingen blijven achter het resultaat staan
fake.values["h"] = {worker.FIELD: worker.BUSY, worker.INFO: worker.busy_info("testmac", "abc123", 2, ["othermac", "derde"])}
queue.finish("h", "testmac", True, "63 regels")
check("klaar noemt de Macs waarop het eerder mislukte", fake.values["h"][worker.FIELD] == worker.DONE
      and "63 regels" in fake.values["h"][worker.INFO] and "othermac, derde" in fake.values["h"][worker.INFO]
      and fake.values["h"][worker.INFO].startswith("testmac, "))
fake.values["i"] = {worker.FIELD: worker.BUSY, worker.INFO: worker.busy_info("testmac", "abc123", 1)}
queue.finish("i", "testmac", True, "10 regels")
check("zonder eerdere fouten blijft de regel zoals hij was", "Eerder" not in fake.values["i"][worker.INFO]
      and "Earlier" not in fake.values["i"][worker.INFO])

# een Mac die zijn asset kwijt is schrijft er niet meer overheen
def claim_a():
    fake.values["a"] = {worker.FIELD: worker.REQUESTED}
    worker.poll_once(queue, "testmac", lambda: False, lambda *a, **k: None, sleep=nap)

claim_a()
fake.values["a"] = {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op othermac sinds 10:00 (ffffff)"}
puts = len(fake.puts)
job = Job(asset_id="a", status="klaar", message="3 regels")
lost_result = worker.report(queue, job, who="testmac")
check("overgenomen asset: report meldt het verlies", lost_result == "lost")
check("overgenomen asset: report schrijft niets", len(fake.puts) == puts
      and fake.values["a"][worker.FIELD] == worker.BUSY and "othermac" in fake.values["a"][worker.INFO])
check("  de job krijgt een notitie en de heartbeat staat uit", ("overgenomen" in job.message or "taken over" in job.message)
      and not worker.current)

claim_a()
fake.values["a"] = {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op othermac sinds 10:00 (ffffff)"}
worker.current["beat"] -= worker.HEARTBEAT + 1
check("heartbeat merkt het verlies op", worker.beat(queue) is False and "a" in worker.lost)
puts = len(fake.puts)
worker.report(queue, Job(**{"asset_id": "a", "status": "fout", "local": True, "remote": 1, "message": "Geen internet"}), who="testmac")
check("  daarna schrijft report ook niets, maar de Mac pauzeert wel", len(fake.puts) == puts and worker.paused()
      and "a" not in worker.lost)
worker.resume()

claim_a()
puts = len(fake.puts)
worker.report(queue, Job(**{"asset_id": "a", "status": "klaar", "message": "5 regels"}), who="testmac")
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

# pauzereden, en hervatten door de gebruiker
worker.pause(reason="De map /Volumes/X/y is niet bereikbaar. Is de opslag (ISG) gemount?")
check("pauze met reden, zonder pad", worker.paused() and worker.state["pause_reason"] == "De map is niet bereikbaar. Is de opslag (ISG) gemount?")
worker.resume()
check("hervatten wist de pauze en de reden", not worker.paused() and worker.state["pause_reason"] == "" and worker.state["paused_until"] is None)
worker.pause(seconds=-1, reason="verlopen")
check("een verlopen pauze wist de reden", worker.paused() is False and worker.state["pause_reason"] == "")

fake, queue = ready()
worker.current.clear()
worker._resumed_at[0] = 0.0
fake.values["a"] = {worker.FIELD: worker.REQUESTED}
queue.claim("a", "testmac", sleep=nap)
queue.give_back("a", "testmac", "Opslag niet gemount.", 1)
check("zonder hervatten slaat de Mac zijn eigen asset over", queue.claim("a", "testmac", sleep=nap) == 0)
worker.resume()
check("na hervatten pakt dezelfde Mac het meteen weer, als poging 2", queue.claim("a", "testmac", sleep=nap) == 2)
worker.current.clear()
worker._resumed_at[0] = 0.0

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
    worker.report(queue, Job(**{"asset_id": "d", "status": "overgeslagen", "message": "No speech"}), who="enmac")
    check("overgeslagen in het Engels", "Skipped: No speech" in fake.values["d"][worker.INFO])
finally:
    worker.set_language("nl")
check("onbekende taal valt terug op Nederlands", (worker.set_language("fr"), worker.language[0])[1] == "nl")

# --- de lus (worker.py): via de Iconik-connector, met een nep-client
import tempfile  # noqa: E402
import base, settings, connectors, iconik  # noqa: E402,E401
base.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"   # nooit de echte voorkeuren aanraken
base.print = lambda *a, **k: None
SOURCE = settings.add_integration("iconik")
connectors.install(iconik)
worker.current.clear()
log = []
stop = threading.Event()
count = [0]
asked = []


def enabled_off(instance):
    count[0] += 1
    if count[0] >= 2:
        stop.set()
    return False


iconik.client = lambda instance: asked.append(instance)   # geeft None: niet ingelogd
loop.run(enabled_off, lambda *a, **k: None, stop, log.append, interval=0)
check("lus uit: de verbinding nooit gevraagd", not asked and worker.state["enabled"] is False)

stop = threading.Event()
count = [0]


def enabled_on(instance):
    count[0] += 1
    if count[0] >= 2:
        stop.set()
    return True


loop.run(enabled_on, lambda *a, **k: None, stop, log.append, interval=0)
check("lus aan maar niet ingelogd: fout in de status", "Niet ingelogd" in worker.state["error"] and asked == [SOURCE, SOURCE])

stop = threading.Event()
count = [0]
logged = []
base.print = lambda *a, **k: logged.append(" ".join(str(x) for x in a))


def boom(instance):
    raise RuntimeError("netwerk weg")


iconik.client = boom
loop.run(enabled_on, lambda *a, **k: None, stop, log.append, interval=0)
check("fout in een rondje stopt de lus niet, het hele verhaal staat in het log", count[0] >= 2
      and any("RuntimeError: netwerk weg" in line for line in logged), logged[-1:])
check("  het venster krijgt een gewone zin, niet de ruwe fout", "netwerk weg" not in worker.state["error"]
      and "Details staan in het log" in worker.state["error"], worker.state["error"])
base.print = lambda *a, **k: None

# de lus neemt een asset aan uit de rij van het exemplaar, en zet het item met dat exemplaar in de lijst
rij = Fake()
iconik.client = lambda instance: rij
rij.values["q1"] = {worker.FIELD: worker.REQUESTED}
added = []
worker.current.clear()
worker._pause[0] = 0.0
connectors.host = None
iconik.host = connectors.Host()
iconik._views.clear()
got = loop.round_once(lambda instance: True, lambda key, **k: added.append((key, k)), log.append)
check("lus: aangenomen asset komt met zijn exemplaar in de lijst", got == f"{SOURCE}:q1"
      and added == [("q1", {"remote": 1, "auto": False, "source": SOURCE})], (got, added))
check("lus: het asset staat op bezig met de naam van deze Mac", rij.values["q1"][worker.FIELD] == worker.BUSY
      and loop.machine_name() in rij.values["q1"][worker.INFO])
check("lus: de connector weet welk exemplaar de lopende opdracht heeft", worker.current.get("instance") == SOURCE)


class NoRoom(connectors.Host):
    def may_take(self):
        return False


rij.values["q2"] = {worker.FIELD: worker.REQUESTED}
worker.current.clear()
iconik.host = NoRoom()
added.clear()
check("lus: geen ruimte op deze Mac (may_take): niets aangenomen",
      loop.round_once(lambda instance: True, lambda key, **k: added.append(key), log.append) is None and not added
      and rij.values["q2"][worker.FIELD] == worker.REQUESTED)
iconik.host = connectors.Host()

# terugmelden via de connector, met gewone gegevens
job = Job(**{"item": f"{SOURCE}:q1", "asset_id": "q1", "status": "klaar", "message": "12 regels", "remote": 1})
worker.current.update(asset="q1", code=worker.claim_code(rij.values["q1"][worker.INFO]), beat=0, instance=SOURCE)
check("terugmelden via de connector: klaar in Iconik", loop.report(job) is None and rij.values["q1"][worker.FIELD] == worker.DONE
      and "12 regels" in rij.values["q1"][worker.INFO])
worker.current.clear()

# --- fouten in gewone woorden (de details alleen in het log)
import requests  # noqa: E402
CA = "/Users/iemand/build/Local Transcribe.app/Contents/Resources/python/lib/python3.12/site-packages/certifi/cacert.pem"
cases = [
    (OSError(f"Could not find a suitable TLS CA certificate bundle, invalid path: {CA}"), "Geen veilige verbinding met Iconik"),
    (requests.exceptions.SSLError("certificate verify failed"), "Geen veilige verbinding met Iconik"),
    (requests.exceptions.ConnectionError("Max retries exceeded with url: /API/..."), "Geen verbinding met Iconik"),
    (requests.exceptions.ReadTimeout("HTTPSConnectionPool: Read timed out."), "Geen verbinding met Iconik"),
    (worker_problem := type("Problem", (Exception,), {})("GET metadata/v1/views/ gaf HTTP 401: {\"error\": \"x\"}"), "Log opnieuw in"),
    (worker_problem.__class__("PUT metadata/v1/assets/a/ gaf HTTP 403: verboden"), "mag dit niet (HTTP 403)"),
    (worker_problem.__class__("GET assets/v1/x gaf HTTP 503: <html><body>Service Unavailable</body></html>"), "Iconik gaf een fout (HTTP 503)"),
    (worker_problem.__class__("GET assets/v1/x gaf HTTP 404: niet gevonden"), "Iconik weigerde een verzoek (HTTP 404)"),
    (ValueError("Expecting value: line 1 column 1"), "Details staan in het log"),
]
worker.set_language("nl")
for error, expected in cases:
    text = worker.plain_error(error)
    check(f"gewone woorden: {type(error).__name__} {str(error)[:40]!r}", expected in text and "/Users/" not in text
          and "<html" not in text and "Max retries" not in text, text)
worker.set_language("en")
check("gewone woorden ook in het Engels", worker.plain_error(cases[2][0]).startswith("No connection to Iconik")
      and "Log in again" in worker.plain_error(cases[4][0]))
worker.set_language("nl")

# --- hulpfuncties
check("age_seconds onleesbaar is None", worker.age_seconds("niet") is None and worker.age_seconds(None) is None)
check("age_seconds leest een tijd van Iconik", abs(worker.age_seconds("2026-10-14T10:37:36.882000+00:00",
      datetime(2026, 10, 14, 10, 47, 36, 882000, tzinfo=timezone.utc)) - 600) < 0.01)
check("busy_owner leest de naam", worker.busy_owner("Bezig op Mac Studio sinds 12:59 (a1b2c3)") == "Mac Studio" and worker.busy_owner("x") == "")
check("stamp is lokale tijd, dag-maand uur:minuut", worker.stamp(datetime(2026, 10, 14, 12, 59)) == "14-10 12:59")
name = loop.machine_name()
check("machine_name is een leesbare naam", bool(name) and name.isprintable())
check("zonder eigen naam: de computernaam", name == loop.computer_name())
check("eigen naam voor deze Mac", loop.set_machine_alias("  Montage  3 ") == "Montage 3" and loop.machine_name() == "Montage 3")
check("eigen naam: tekens die de lijst breken en regeleinden eruit, niet te lang",
      loop.clean_alias("a;b[c]\nd") == "abcd" and len(loop.clean_alias("x" * 100)) == loop.MAX_NAME, loop.clean_alias("a;b[c]\nd"))
check("eigen naam leeg: weer de computernaam", loop.set_machine_alias("") == "" and loop.machine_name() == loop.computer_name())

fake.values["q"] = {worker.FIELD: worker.BUSY, worker.INFO: "Bezig op testmac sinds 13:00 (abc)"}
queue.give_back("q", "testmac", "Is de opslag (ISG) gemount?", 1)
check("geen punt na een vraagteken", "gemount? Mislukte poging 1 van 3" in fake.values["q"][worker.INFO], fake.values["q"][worker.INFO])

# --- auto transcribe: kandidaten zoeken en claimen
fake, queue = ready()
media = {
    "nieuw-video":   ("video", "opslag-1", 20, "2026-09-13T12:00:00"),
    "oud-video":     ("video", "opslag-1", 30, "2025-09-20T11:00:00"),
    "audio":         ("audio", "opslag-1", 10, "2026-09-01T10:00:00"),
    "te-lang":       ("video", "opslag-1", 61, "2026-09-10T10:00:00"),
    "andere-opslag": ("video", "opslag-2", 10, "2026-09-12T10:00:00"),
    "met-transcript": ("video", "opslag-1", 10, "2026-09-11T10:00:00"),
    "aangevraagd":   ("video", "opslag-1", 10, "2026-09-09T10:00:00"),
    "klaar":         ("video", "opslag-1", 10, "2026-09-08T10:00:00"),
}
for asset, (kind, storage, minutes, created) in media.items():
    fake.media[asset] = {"type": kind, "storage": storage, "minutes": minutes, "created": created,
                         "transcript": asset == "met-transcript"}
fake.values["aangevraagd"] = {worker.FIELD: worker.REQUESTED}
fake.values["klaar"] = {worker.FIELD: worker.DONE}

found = queue.candidates(["opslag-1"])
check("kandidaten: video zonder status en transcript, binnen een uur, nieuwste eerst",
      found == ["nieuw-video", "oud-video"], found)
check("met audio aan komt audio erbij, op datum gesorteerd",
      queue.candidates(["opslag-1"], audio=True) == ["nieuw-video", "audio", "oud-video"], queue.candidates(["opslag-1"], audio=True))
check("andere opslag alleen als deze Mac die kent",
      queue.candidates(["opslag-1", "opslag-2"]) == ["nieuw-video", "andere-opslag", "oud-video"])
check("langere maximale duur laat het lange asset toe", "te-lang" in queue.candidates(["opslag-1"], max_minutes=90))
check("limiet", queue.candidates(["opslag-1"], audio=True, limit=1) == ["nieuw-video"])
check("geen opslag bekend: geen kandidaten, geen zoekopdracht", queue.candidates([]) == [])

check("een asset zonder ingevulde metadata leest als geen status", queue.read("nieuw-video") == (None, "", None))
real_get = fake.get
fake.get = lambda path: (_ for _ in ()).throw(RuntimeError(f"GET {path} returned HTTP 500: server error"))
try:
    queue.read("nieuw-video")
    check("een andere fout dan een 404 wordt doorgegeven", False)
except RuntimeError:
    check("een andere fout dan een 404 wordt doorgegeven", True)
fake.get = real_get
check("auto claimt een asset zonder status", queue.claim("nieuw-video", "macbook", sleep=lambda s: None, auto=True) == 1)
info = fake.values["nieuw-video"][worker.INFO]
check("auto claim: bezig, met [auto] en een claimcode", fake.values["nieuw-video"][worker.FIELD] == worker.BUSY
      and worker.is_auto(info) and worker.claim_code(info) and worker.busy_owner(info) == "macbook", info)
check("auto claimt geen aangevraagd asset", queue.claim("aangevraagd", "macbook", sleep=lambda s: None, auto=True) == 0)
check("auto claimt geen asset dat al klaar is", queue.claim("klaar", "macbook", sleep=lambda s: None, auto=True) == 0)
check("de gewone route claimt geen asset zonder status", queue.claim("oud-video", "macbook", sleep=lambda s: None) == 0)
check("na de claim is het geen kandidaat meer", "nieuw-video" not in queue.candidates(["opslag-1"]))

# Mislukt door de Mac: terug naar leeg, niet naar Aangevraagd
check("teruggeven van een auto-job", queue.give_back("nieuw-video", "macbook", "Is de opslag (ISG) gemount?", 1))
info = fake.values["nieuw-video"].get(worker.INFO, "")
check("mislukte auto-job: status leeg, geen voorrang", worker.FIELD not in fake.values["nieuw-video"], fake.values["nieuw-video"])
check("mislukte auto-job: poging, Mac en [auto] in de details", worker.failed_attempts(info) == 1
      and worker.failed_on(info) == ["macbook"] and worker.is_auto(info), info)
check("weer een kandidaat", "nieuw-video" in queue.candidates(["opslag-1"]))
check("de Mac waarop hij mislukte pakt hem niet zelf opnieuw",
      queue.claim("nieuw-video", "macbook", sleep=lambda s: None, auto=True) == 0)
check("een andere Mac wel, als tweede poging", queue.claim("nieuw-video", "Laptop", sleep=lambda s: None, auto=True) == 2
      and worker.failed_on(fake.values["nieuw-video"][worker.INFO]) == ["macbook"])
check("na drie pogingen definitief fout", not queue.give_back("nieuw-video", "Laptop", "opslag weg", 3)
      and fake.values["nieuw-video"][worker.FIELD] == worker.FAILED)

# Klaar: [auto] blijft zichtbaar in de details
queue.claim("oud-video", "macbook", sleep=lambda s: None, auto=True)
queue.finish("oud-video", "macbook", True, "31 regels, 2 sprekers")
info = fake.values["oud-video"][worker.INFO]
check("klaar na auto: Klaar met [auto] in de details", fake.values["oud-video"][worker.FIELD] == worker.DONE
      and worker.is_auto(info) and "31 regels" in info, info)

# Vastgelopen auto-job: een andere Mac zet hem terug op leeg, niet op Aangevraagd
queue.claim("audio", "macbook", sleep=lambda s: None, auto=True)
fake.values["audio"][worker.INFO] = fake.values["audio"][worker.INFO].replace(
    worker.utc_stamp(), "2026-10-01 10:00Z")
released = queue.release_stale("Laptop")
check("vastgelopen auto-job gaat terug naar leeg", released == ["audio"] and worker.FIELD not in fake.values["audio"]
      and worker.failed_on(fake.values["audio"][worker.INFO]) == ["macbook"], fake.values["audio"])

# --- de lus: eerst aanvragen, dan pas automatisch
fake, queue = ready()
for asset, created in (("auto-1", "2026-09-02"), ("auto-2", "2026-09-01")):
    fake.media[asset] = {"type": "video", "storage": "opslag-1", "minutes": 5, "created": created, "transcript": False}
taken = []
add = lambda asset_id, remote=0, auto=False: taken.append((asset_id, remote, auto) if auto else (asset_id, remote))
settings = {"storages": ["opslag-1"], "audio": False, "max_minutes": 60}
worker.current.clear()
worker._pause[0] = 0.0
check("auto uit: niets automatisch", worker.poll_once(queue, "macbook", lambda: False, add, sleep=lambda s: None) is None
      and not taken)
check("geen ruimte (auto geeft None): niets automatisch",
      worker.poll_once(queue, "macbook", lambda: False, add, sleep=lambda s: None, auto=lambda: None) is None and not taken)
fake.values["aanvraag"] = {worker.FIELD: worker.REQUESTED}
got = worker.poll_once(queue, "macbook", lambda: False, add, sleep=lambda s: None, auto=lambda: settings)
check("een aanvraag gaat voor, ook met auto aan", got == "aanvraag" and taken[-1] == ("aanvraag", 1)
      and not worker.is_auto(fake.values["aanvraag"][worker.INFO]), taken)
got = worker.poll_once(queue, "macbook", lambda: False, add, sleep=lambda s: None, auto=lambda: settings)
check("geen aanvraag meer: het nieuwste asset zonder status, met auto in de lijst", got == "auto-1" and taken[-1] == ("auto-1", 1, True)
      and worker.is_auto(fake.values["auto-1"][worker.INFO]) and worker.state["last_auto"] == "auto-1", taken)
check("de heartbeat volgt het auto-asset", worker.current.get("asset") == "auto-1"
      and worker.current.get("code") == worker.claim_code(fake.values["auto-1"][worker.INFO]))
check("tijdens een opdracht wordt niets gezocht",
      worker.poll_once(queue, "macbook", lambda: True, add, sleep=lambda s: None, auto=lambda: settings) is None)
calls = []
worker.poll_once(queue, "macbook", lambda: False, add, sleep=lambda s: None,
                 auto=lambda: calls.append(1) or None)
fake.values["aanvraag-2"] = {worker.FIELD: worker.REQUESTED}
worker.poll_once(queue, "macbook", lambda: False, add, sleep=lambda s: None,
                 auto=lambda: calls.append(2) or settings)
check("de ruimte wordt pas gemeten als er geen aanvraag ligt", calls == [1], calls)
worker.current.clear()
fake.values["aanvraag-3"] = {worker.FIELD: worker.REQUESTED}
fake.media["auto-3"] = {"type": "video", "storage": "opslag-1", "minutes": 5, "created": "2026-09-03", "transcript": False}
answers = iter([True, False])   # mag aan het begin van de ronde, niet meer vlak voor de claim
got = worker.poll_once(queue, "macbook", lambda: False, add, sleep=lambda s: None, auto=lambda: settings,
                       allowed=lambda: next(answers, False))
check("mag het niet meer vlak voor de claim: niets geclaimd", got is None
      and fake.values["aanvraag-3"][worker.FIELD] == worker.REQUESTED, fake.values["aanvraag-3"])
check("en geen automatische claim", worker.FIELD not in fake.values.get("auto-3", {}))
worker.current.clear()

# Een gewone aanvraag blijft zich gedragen zoals voorheen
fake.values["gewoon"] = {worker.FIELD: worker.REQUESTED}
queue.claim("gewoon", "macbook", sleep=lambda s: None)
check("gewone claim heeft geen [auto]", not worker.is_auto(fake.values["gewoon"][worker.INFO]))
queue.give_back("gewoon", "macbook", "opslag weg", 1)
check("gewone job gaat terug op Aangevraagd", fake.values["gewoon"][worker.FIELD] == worker.REQUESTED
      and not worker.is_auto(fake.values["gewoon"][worker.INFO]))

print()
print(f"{sum(results)} van {len(results)} goed")
sys.exit(0 if all(results) else 1)
