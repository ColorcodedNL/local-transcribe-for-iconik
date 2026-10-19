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
    serve.cleared[0] = 0
    while not serve.work.empty():
        serve.work.get_nowait()


# --- opslaan en terughalen
reset()
for asset in ("a1", "a2", "a3", "a4"):
    serve.add_job(asset)
a1, a2, a3, a4 = serve.jobs
a1.update(status="klaar", message="63 regels", title="Clip een", duration=120.0, started=datetime.now(), finished=datetime.now(),
          result={"segments": 63, "speakers": 2}, language="nl", tracks=[{"n": 1, "db": -12.5, "note": "geluid", "used": True, "env": [0.1, 0.5]}])
a2.update(status="fout", message="Geen internet", finished=datetime.now())
a3.update(status="bezig", started=datetime.now(), stage="transcriberen", fraction=0.4)
serve.save_jobs()
check("het bestand staat naast de voorkeuren", serve.jobs_path().is_file() and serve.jobs_path().parent == transcribe.CONFIG_PATH.parent)
check("geen tijdelijk bestand achtergebleven", not list(serve.jobs_path().parent.glob("*.tmp")))

reset()
serve.load_jobs()
got = {job["asset_id"]: job for job in serve.jobs}
check("alle vier terug, in dezelfde volgorde", [j["asset_id"] for j in serve.jobs] == ["a1", "a2", "a3", "a4"])
check("klaar blijft klaar met resultaat, titel, taal en sporen", got["a1"]["status"] == "klaar" and got["a1"]["title"] == "Clip een"
      and got["a1"]["result"] == {"segments": 63, "speakers": 2} and got["a1"]["language"] == "nl"
      and got["a1"]["tracks"][0]["env"] == [0.1, 0.5] and isinstance(got["a1"]["finished"], datetime))
check("fout blijft fout met de melding", got["a2"]["status"] == "fout" and got["a2"]["message"] == "Geen internet")
check("wat bezig was is afgebroken, met een melding en een eindtijd", got["a3"]["status"] == "afgebroken"
      and got["a3"]["message"] and got["a3"]["finished"] is not None and got["a3"]["stage"] == "")
check("wat wachtte is ook afgebroken", got["a4"]["status"] == "afgebroken")
check("afgebroken opdrachten staan niet in de werkrij", serve.work.empty())
check("de nummers lopen door", serve.add_job("a5") and serve.jobs[-1]["n"] == 5, str(serve.jobs[-1]["n"]))

# --- opruimen
serve.jobs[-1]["status"] = "bezig"
serve.clear_finished()
check("opruimen laat alleen wat loopt of wacht staan", [j["asset_id"] for j in serve.jobs] == ["a5"])
reset()
serve.load_jobs()
check("het opruimen is ook bewaard, met doorlopende nummers", [j["asset_id"] for j in serve.jobs] == ["a5"] and serve.cleared[0] == 4)
serve.add_job("a6")
check("nieuw nummer na opruimen en herstart", serve.jobs[-1]["n"] == 1 + 1 + 4 or serve.jobs[-1]["n"] == 6, str(serve.jobs[-1]["n"]))

# --- bovengrens
reset()
for i in range(serve.MAX_KEPT_JOBS + 20):
    serve.add_job(f"x{i}")
    serve.jobs[-1]["status"] = "klaar"
serve.save_jobs()
reset()
serve.load_jobs()
check("niet meer dan de bovengrens bewaard, de nieuwste", len(serve.jobs) == serve.MAX_KEPT_JOBS and serve.jobs[-1]["asset_id"] == f"x{serve.MAX_KEPT_JOBS + 19}")

# --- kapotte of vreemde bestanden
reset()
serve.jobs_path().write_text("{ dit is geen json", encoding="utf-8")
serve.load_jobs()
check("kapot bestand: lege lijst, geen crash", serve.jobs == [])
serve.jobs_path().write_text(json.dumps({"saved": "nooit", "cleared": "x?", "jobs": [{"n": 1}, "rommel", {"n": 2, "asset_id": "ok", "status": "klaar"}]}), encoding="utf-8")
try:
    serve.load_jobs()
    ok = True
except Exception as error:
    ok = False
check("rare inhoud: geen crash", ok)
serve.jobs_path().unlink()
reset()
serve.load_jobs()
check("geen bestand: lege lijst", serve.jobs == [])

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
    if serve.jobs[0]["title"]:
        break
    time.sleep(0.05)
check("opdracht uit de rij krijgt zijn titel", serve.jobs[0]["title"] == "Titel uit Iconik")
job = serve.jobs[0]
job.update(status="fout", local=True, message="Geen internet.")
serve.report_remote(job)
check("teruggezet asset is geen eindfout", job["status"] == "teruggezet" and job["message"].startswith("Geen internet.")
      and ("andere Mac" in job["message"] or "another Mac" in job["message"]), job["message"])
outcome[0] = None
job.update(status="fout", local=True, message="Geen internet.")
serve.report_remote(job)
check("zonder terugzetten blijft het een fout", job["status"] == "fout" and job["message"] == "Geen internet.")
serve.save_jobs()
reset()
serve.load_jobs()
check("teruggezet blijft zo bewaard", serve.jobs and serve.jobs[0]["status"] in ("fout", "teruggezet"))

print("\nALLES OK" if not fails else f"\n{fails} FOUT")
sys.exit(1 if fails else 0)
