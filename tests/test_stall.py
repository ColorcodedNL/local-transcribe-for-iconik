"""Een vastgelopen opdracht: voortgang, watchdog en het lokale modelpad.

Draaien: uv run --no-project --with pynacl --with requests python -B tests/test_stall.py
"""
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, ".")
for name in ("keyring", "numpy", "mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)

# Een nepversie van huggingface_hub: de test mag niets downloaden.
hub = types.ModuleType("huggingface_hub")
cache = {"cached": True, "calls": []}


def snapshot_download(repo, local_files_only=False):
    cache["calls"].append((repo, local_files_only))
    if local_files_only and not cache["cached"]:
        raise OSError("niet in de cache")
    return "/cache/models--x/snapshots/" + ("lokaal" if local_files_only else "gedownload")


class HfApi:
    def model_info(self, *args, **kwargs):
        raise OSError("geen verbinding")


hub.snapshot_download, hub.HfApi = snapshot_download, HfApi
hub.constants = types.SimpleNamespace(HF_HUB_CACHE=tempfile.mkdtemp())
sys.modules["huggingface_hub"] = hub

import transcribe, serve  # noqa: E402,E401

transcribe.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"   # nooit de echte voorkeuren aanraken
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


# --- het lokale modelpad
folder = tempfile.mkdtemp()
check("een bestaande map is zelf het pad", transcribe.ensure_model(folder) == folder)
check("model in de cache: het lokale pad, niet de repo-naam",
      transcribe.ensure_model("org/model") == "/cache/models--x/snapshots/lokaal" and cache["calls"][-1] == ("org/model", True))
cache["cached"] = False
check("model niet in de cache: na het downloaden ook een pad",
      transcribe.ensure_model("org/model") == "/cache/models--x/snapshots/gedownload")

# --- voortgang en watchdog
def fresh_job(**extra):
    job = {"asset_id": "a", "stage": "transcriberen", "status": "bezig", "remote": 2, "duration": 0}
    job.update(extra)
    return job


serve.current[0] = None
check("geen opdracht: niets om te beoordelen", serve.progress_ok() is True)

serve.current[0] = fresh_job()
serve.last_progress[0] = time.monotonic()
check("net voortgang: in orde", serve.progress_ok() is True)
serve.last_progress[0] = time.monotonic() - serve.STALL_SECONDS - 5
check("langer dan de grens niets gemeld: vastgelopen", serve.progress_ok() is False)
serve.listen("progress", fraction=0.5)
check("een voortgangsmelding zet de klok terug", serve.progress_ok() is True)

serve.current[0] = fresh_job(duration=4 * serve.STALL_SECONDS)
serve.last_progress[0] = time.monotonic() - serve.STALL_SECONDS - 5
check("lange audio krijgt een ruimere grens (sprekerherkenning meldt niets)", serve.progress_ok() is True)
serve.last_progress[0] = time.monotonic() - 3 * serve.STALL_SECONDS
check("ook die grens bestaat", serve.progress_ok() is False)

serve.current[0] = fresh_job(stage="model")
serve.last_progress[0] = time.monotonic() - 10 * serve.STALL_SECONDS
check("model downloaden wordt niet beoordeeld", serve.progress_ok() is True)

# --- opgeven
reported, restarted, exits = [], [], []
serve.report_remote = lambda job: reported.append(dict(job))
serve.leave = lambda code: exits.append(code)
serve.STALL_GRACE = 0

job = fresh_job()
serve.current[0] = job
serve.work.put({"asset_id": "wachtend"})
serve.restart_hook = lambda: restarted.append(1)
serve.last_progress[0] = time.monotonic() - 2 * serve.STALL_SECONDS
serve.give_up_stalled(job)
check("opgegeven opdracht: afgebroken, lokaal probleem, gemarkeerd",
      job["status"] == "fout" and job["local"] is True and job["abandoned"] is True and "30" in job["message"])
check("de opdracht uit de rij wordt teruggemeld met zijn pogingnummer", len(reported) == 1 and reported[0]["remote"] == 2)
check("busy() is weer onwaar, anders vraagt afsluiten om bevestiging",
      serve.current[0] is None and serve.work.empty() and serve.busy() is False)
check("de app herstart en sluit af", restarted == [1] and exits == [0])

job = fresh_job(remote=0)
serve.current[0] = job
reported.clear(), exits.clear()
serve.restart_hook = None
serve.give_up_stalled(job)
check("gewone opdracht: niets terug te melden", reported == [])
check("zonder herstartfunctie (terminal): afsluiten met een foutcode", exits == [3])

print("\n" + ("alles OK" if not fails else f"{fails} FOUT"))
sys.exit(1 if fails else 0)
