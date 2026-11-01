"""Vooruit lezen van de volgende opdracht alleen met genoeg geheugen vrij (de pieken tellen op).

Draaien: uv run --no-project --with pynacl --with requests python -B tests/test_prefetch.py
"""
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, "app")
for name in ("keyring", "numpy", "mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
import transcribe, serve, resources  # noqa: E402,E401

transcribe.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"   # nooit de echte voorkeuren aanraken
serve.client = lambda: None   # vooruit lezen praat dan met niemand
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


def attempt(available_gb, level=1):
    resources.memory = lambda: (None if available_gb is None else available_gb * 1e9, level)
    job = {"asset_id": "volgende"}
    with serve.work.mutex:
        serve.work.queue.clear()
        serve.work.queue.append(job)
    serve.start_prefetch()
    box = job.get("prefetch")
    if box:
        box["thread"].join(5)
    return box is not None


check("ruim genoeg vrij: vooruit lezen", attempt(20))
check("te weinig vrij: niet vooruit lezen", not attempt(4))
check("precies de grens: vooruit lezen", attempt(6))
check("geheugendruk hoog: niet vooruit lezen, ook met genoeg vrij", not attempt(20, level=2))
check("geheugen niet te lezen: gewoon vooruit lezen", attempt(None, level=None))
transcribe.set_config("prefetch_min_free_gb", 2)
check("grens in config.json lager: 4 GB is genoeg", attempt(4))
transcribe.set_config("prefetch_min_free_gb", "onzin")
check("onzin in config.json: standaardgrens", not attempt(4))

with serve.work.mutex:
    serve.work.queue.clear()
print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
