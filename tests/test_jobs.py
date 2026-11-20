"""De lijst met opdrachten blijft bewaard tot de gebruiker hem opruimt.

Draaien: uv run --no-project --with pynacl --with requests python -B tests/test_jobs.py
"""
import json
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "app")
# Nooit de echte sleutelhanger: een nep-keyring zonder inlog (anders vraagt macOS om het wachtwoord).
_kr = types.ModuleType("keyring")
_kr.get_password = lambda *a: None
_kr.set_password = lambda *a: None
_kr.delete_password = lambda *a: None
sys.modules["keyring"] = _kr
for name in ("keyring", "numpy", "mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)

import transcribe, serve  # noqa: E402,E401

transcribe.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"   # nooit de echte voorkeuren aanraken
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


def reset():
    serve.jobs.clear()
    serve.jobs.cleared = 0
    while not serve.work.empty():
        serve.work.get_nowait()


# --- opslaan en terughalen
reset()
for asset in ("a1", "a2", "a3", "a4"):
    serve.add_job(asset)
a1, a2, a3, a4 = serve.jobs
a1.set(status="klaar", message="63 regels", title="Clip een", duration=120.0, started=datetime.now(), finished=datetime.now(),
          result={"segments": 63, "speakers": 2}, language="nl", tracks=[{"n": 1, "db": -12.5, "note": "geluid", "used": True, "env": [0.1, 0.5]}])
a2.set(status="fout", message="Geen internet", finished=datetime.now(), auto=True)
a3.set(status="bezig", started=datetime.now(), stage="transcriberen", fraction=0.4)
serve.save_jobs()
check("het bestand staat naast de voorkeuren", serve.jobs_path().is_file() and serve.jobs_path().parent == transcribe.CONFIG_PATH.parent)
check("geen tijdelijk bestand achtergebleven", not list(serve.jobs_path().parent.glob("*.tmp")))

reset()
serve.load_jobs()
got = {job.asset_id: job for job in serve.jobs}
check("alle vier terug, in dezelfde volgorde", [j.asset_id for j in serve.jobs] == ["a1", "a2", "a3", "a4"])
check("klaar blijft klaar met resultaat, titel, taal en sporen", got["a1"].status == "klaar" and got["a1"].title == "Clip een"
      and got["a1"].result == {"segments": 63, "speakers": 2} and got["a1"].language == "nl"
      and got["a1"].tracks[0]["env"] == [0.1, 0.5] and isinstance(got["a1"].finished, datetime))
check("fout blijft fout met de melding", got["a2"].status == "fout" and got["a2"].message == "Geen internet")
check("het label Auto blijft bewaard, en ontbreekt waar het niet hoort", got["a2"].auto is True and got["a1"].auto is False)
check("wat bezig was is afgebroken, met een melding en een eindtijd", got["a3"].status == "afgebroken"
      and got["a3"].message and got["a3"].finished is not None and got["a3"].stage == "")
check("wat wachtte is ook afgebroken", got["a4"].status == "afgebroken")
check("afgebroken opdrachten staan niet in de werkrij", serve.work.empty())
check("de nummers lopen door", serve.add_job("a5") and serve.jobs[-1].n == 5, str(serve.jobs[-1].n))

# --- opruimen
serve.jobs[-1].set(status="bezig")
serve.clear_finished()
check("opruimen laat alleen wat loopt of wacht staan", [j.asset_id for j in serve.jobs] == ["a5"])
reset()
serve.load_jobs()
check("het opruimen is ook bewaard, met doorlopende nummers", [j.asset_id for j in serve.jobs] == ["a5"] and serve.jobs.cleared == 4)
serve.add_job("a6")
check("nieuw nummer na opruimen en herstart", serve.jobs[-1].n == 1 + 1 + 4 or serve.jobs[-1].n == 6, str(serve.jobs[-1].n))

# --- bovengrens
reset()
for i in range(serve.MAX_KEPT_JOBS + 20):
    serve.add_job(f"x{i}")
    serve.jobs[-1].set(status="klaar")
serve.save_jobs()
reset()
serve.load_jobs()
check("niet meer dan de bovengrens bewaard, de nieuwste", len(serve.jobs) == serve.MAX_KEPT_JOBS and serve.jobs[-1].asset_id == f"x{serve.MAX_KEPT_JOBS + 19}")

# --- kapotte of vreemde bestanden
reset()
serve.jobs_path().write_text("{ dit is geen json", encoding="utf-8")
serve.load_jobs()
check("kapot bestand: lege lijst, geen crash", len(serve.jobs) == 0)
serve.jobs_path().write_text(json.dumps({"saved": "nooit", "cleared": "x?", "jobs": [{"n": 1}, "rommel", {"n": 2, "asset_id": "ok", "status": "klaar"}]}), encoding="utf-8")
try:
    serve.load_jobs()
    ok = True
except Exception:
    ok = False
check("rare inhoud: geen crash", ok)
serve.jobs_path().unlink()
reset()
serve.load_jobs()
check("geen bestand: lege lijst", len(serve.jobs) == 0)

# --- terug in de rij is geen eindfout en opdrachten uit de rij krijgen hun titel
class FakeQueue:
    def __init__(self, connection): pass
    def setup(self): pass


outcome = ["requeued"]
serve.client = lambda: types.SimpleNamespace(get=lambda path: {"title": "Titel uit Iconik"})
serve.remote.Queue = FakeQueue
serve.remote.report = lambda queue, job: outcome[0]
reset()
serve.add_job("r1", remote=2)
import time
for _ in range(50):
    if serve.jobs[0].title:
        break
    time.sleep(0.05)
check("opdracht uit de rij krijgt zijn titel", serve.jobs[0].title == "Titel uit Iconik")
job = serve.jobs[0]
job.set(status="fout", local=True, message="Geen internet.")
serve.report_remote(job)
check("teruggezet asset is geen eindfout", job.status == "teruggezet" and job.message.startswith("Geen internet.")
      and ("andere Mac" in job.message or "another Mac" in job.message), job.message)
outcome[0] = None
job.set(status="fout", local=True, message="Geen internet.")
serve.report_remote(job)
check("zonder terugzetten blijft het een fout", job.status == "fout" and job.message == "Geen internet.")
serve.save_jobs()
reset()
serve.load_jobs()
check("teruggezet blijft zo bewaard", serve.jobs and serve.jobs[0].status in ("fout", "teruggezet"))

# --- collecties: uitpakken buiten het verzoek, een fout wordt een rode regel in de lijst
import threading  # noqa: E402


def wait_for(cond, seconds=3):
    end = time.time() + seconds
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


reset()
gate = threading.Event()


class FakeIconik:
    def get(self, path):
        if path.startswith("assets/v1/collections/col1/contents/"):
            gate.wait(3)   # een trage collectie
            return {"objects": [{"object_type": "assets", "id": "c1"}, {"object_type": "assets", "id": "c2"}]}
        if path.startswith("assets/v1/collections/kapot/contents/"):
            raise transcribe.Problem("GET assets/v1/collections/kapot/contents/ returned HTTP 500")
        if path == "assets/v1/collections/kapot/":
            return {"title": "Draaidag 3"}
        return {"title": "Titel uit Iconik"}


serve.client = lambda: FakeIconik()
started = time.time()
serve.queue_request([(["los1"], ["col1"])])
check("losse assets staan meteen in de rij, ook als de collectie nog uitgepakt wordt",
      [j.asset_id for j in serve.jobs] == ["los1"] and time.time() - started < 1, [j.asset_id for j in serve.jobs])
gate.set()
check("daarna volgen de assets uit de collectie", wait_for(lambda: [j.asset_id for j in serve.jobs] == ["los1", "c1", "c2"]),
      [j.asset_id for j in serve.jobs])

reset()
serve.queue_request([([], ["kapot"])])
check("een collectie die niet lukt wordt een rode regel met naam en melding",
      wait_for(lambda: len(serve.jobs) == 1) and serve.jobs[0].status == "fout"
      and "Draaidag 3" in serve.jobs[0].title and "HTTP 500" in serve.jobs[0].message, serve.jobs)
check("die regel komt niet in de werkrij", serve.work.empty())
serve.add_job("na")
check("het volgnummer loopt door", serve.jobs[-1].n == serve.jobs[0].n + 1)
serve.save_jobs()
reset()
serve.load_jobs()
check("de rode regel blijft bewaard", serve.jobs and serve.jobs[0].status == "fout" and "Draaidag 3" in serve.jobs[0].title)

# --- jobs.json van 0.6 (en van foundation tot stap 4b) wordt precies zo gelezen en weer zo geschreven
old_rows = [
    {"n": 7, "asset_id": "oud1", "title": "Clip", "status": "klaar", "message": "12 regels", "duration": 61.5,
     "result": {"segments": 12, "speakers": 1}, "language": "en", "tracks": [{"n": 1, "used": True}], "auto": False,
     "added": "2026-11-16T10:00:00", "started": "2026-11-16T10:00:05", "finished": "2026-11-16T10:02:00"},
    {"n": 8, "asset_id": "oud2", "title": "", "status": "fout", "message": "Geen internet", "duration": None,
     "result": None, "language": None, "tracks": [], "auto": True,
     "added": "2026-11-16T11:00:00", "started": None, "finished": "2026-11-16T11:00:01"},
]
serve.jobs_path().write_text(json.dumps({"saved": "2026-11-16T12:00:00", "cleared": 6, "jobs": old_rows}), encoding="utf-8")
reset()
serve.load_jobs()
again = serve.jobs.payload()
source = serve.items.iconik_source()
expected = [dict(row, item=f"{source}:{row['asset_id']}") for row in old_rows]
check("oud bestand: asset_id wordt het item in de enige Iconik", source and [j.item for j in serve.jobs] == [f"{source}:oud1", f"{source}:oud2"],
      [j.item for j in serve.jobs])
check("oud bestand: verder komt elke regel ongewijzigd terug", again["jobs"] == expected, again["jobs"])
check("oud bestand: het asset blijft te vinden voor Iconik", [j.asset_id for j in serve.jobs] == ["oud1", "oud2"])
check("oud bestand: de teller van opgeruimde regels blijft", again["cleared"] == 6 and serve.jobs.cleared == 6)
check("bewaren schrijft versie 2 met item, en asset_id voor een oudere versie van de app", again["version"] == 2
      and all(set(row) == set(expected[0]) for row in again["jobs"]))
serve.save_jobs()
reset()
serve.load_jobs()
check("versie 2 komt precies zo terug", serve.jobs.payload()["jobs"] == expected)
# Zo leest 0.6.2 een regel (serve.load_jobs van toen): n en asset_id moeten er zijn, anders valt de regel weg.
check("een oudere versie vindt in elke regel n en asset_id", all(isinstance(r["n"], int) and r["asset_id"]
      for r in json.loads(serve.jobs_path().read_text())["jobs"]))
red = {"n": 9, "asset_id": "", "title": "Collectie X", "status": "fout", "message": "HTTP 500", "added": "2026-11-16T12:00:00"}
serve.jobs_path().write_text(json.dumps({"cleared": 0, "jobs": [red]}), encoding="utf-8")
reset()
serve.load_jobs()
check("oud bestand: een rode regel zonder asset blijft, zonder item", len(serve.jobs) == 1 and serve.jobs[0].item == ""
      and serve.jobs[0].title == "Collectie X")
serve.jobs_path().write_text(json.dumps({"version": 2, "cleared": 0, "jobs": [
    dict(expected[0], item="geen-item"), dict(expected[0], n=12, item="local:clip-1")]}), encoding="utf-8")
reset()
serve.load_jobs()
check("versie 2: een regel met een onzin-item valt weg, een lokaal item blijft", [j.item for j in serve.jobs] == ["local:clip-1"]
      and serve.jobs[0].asset_id == "")

# --- een opdracht kent alleen zijn eigen velden (een tikfout wordt een fout, geen stil nieuw veld)
try:
    serve.jobs[0].set(statsu="klaar")
    typo = False
except AttributeError:
    typo = True
check("een onbekend veld wordt geweigerd", typo)

# --- veel draden tegelijk: de lijst blijft heel en bewaren ziet een opdracht nooit half veranderd
reset()
for i in range(20):
    serve.add_job(f"t{i}")
stop = threading.Event()
problems = []


def flip(job):
    # zoals de werker: status en eindtijd in één keer, steeds heen en weer
    while not stop.is_set():
        job.set(status="klaar", finished=datetime.now(), message="klaar")
        job.set(status="bezig", finished=None, message="", stage="transcriberen", fraction=0.5)


def churn():
    # toevoegen, weghalen en opruimen door elkaar
    k = 0
    while not stop.is_set():
        k += 1
        serve.add_job(f"extra{k}")
        last = serve.jobs[-1]
        last.set(status="klaar", finished=datetime.now())
        serve.jobs.remove(last.n)
        if k % 25 == 0:
            serve.jobs.clear_finished()


def look():
    # zoals bewaren en /state: elke regel moet kloppen
    while not stop.is_set():
        try:
            for row in serve.jobs.payload()["jobs"]:
                if row["status"] == "klaar" and not row["finished"]:
                    problems.append(("klaar zonder eindtijd", row["asset_id"]))
                if row["status"] == "bezig" and row["finished"]:
                    problems.append(("bezig met eindtijd", row["asset_id"]))
            for view in serve.jobs.views():
                if view["status"] == "klaar" and view["message"] != "klaar" and view["asset_id"].startswith("t"):
                    problems.append(("half veranderd in /state", view["asset_id"]))
            [j.n for j in serve.jobs]
        except Exception as error:   # bv. "list changed size during iteration"
            problems.append((type(error).__name__, str(error)))


threads = [threading.Thread(target=flip, args=(job,)) for job in list(serve.jobs)[:5]]
threads += [threading.Thread(target=churn), threading.Thread(target=look), threading.Thread(target=look)]
for t in threads:
    t.start()
time.sleep(1.5)
stop.set()
for t in threads:
    t.join(5)
check("veel draden tegelijk: geen half veranderde opdracht en geen fout", not problems, problems[:3])
numbers = [j.n for j in serve.jobs]
check("veel draden tegelijk: geen dubbele nummers", len(numbers) == len(set(numbers)))
serve.save_jobs()
reset()
serve.load_jobs()
check("veel draden tegelijk: het bestand is daarna gewoon te lezen", len(serve.jobs) == len(numbers))

# --- de app gebruikt de oude vorm (een dict per opdracht) nergens meer
import re  # noqa: E402
# (refine.py heeft een eigen `job` voor het uitrekenen van golfvormen; dat is een andere zaak)
old_style = [f"{f.name}:{i}" for f in map(Path, ("app/serve.py", "app/worker.py", "app/app_main.py")) for i, line in enumerate(f.read_text().splitlines(), 1)
             if re.search(r"\b(job|upcoming)(\[|\.get\(|\.pop\(|\.update\()", line)]
check("geen job[...] of job.get(...) meer in de app", not old_style, old_style)

print("\nALLES OK" if not fails else f"\n{fails} FOUT")
sys.exit(1 if fails else 0)
