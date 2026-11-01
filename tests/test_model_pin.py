"""Het Whisper-model staat vast op een revisie, en de vaste revisie wordt ook zonder internet in de cache gevonden.

Draaien: uv run --no-project --with pynacl --with requests --with huggingface_hub python -B tests/test_model_pin.py
"""
import os
import sys
import tempfile
import types
from pathlib import Path

cache = Path(tempfile.mkdtemp())
os.environ["HF_HUB_CACHE"] = str(cache)      # voor het importeren van huggingface_hub: een lege, eigen cache
os.environ["HF_HUB_OFFLINE"] = "1"           # nooit het netwerk op

sys.path.insert(0, "app")
for name in ("keyring", "numpy", "mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
import transcribe  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


REPO = "mlx-community/whisper-large-v3-mlx"
pinned = transcribe.MODEL_REVISIONS.get(REPO, "")
check("model staat vast op een commit-hash", len(pinned) == 40 and all(c in "0123456789abcdef" for c in pinned), pinned)

# Een cache zoals hij op een Mac staat: snapshots/<hash>/ met de bestanden, refs/main wijst naar die hash.
folder = cache / ("models--" + REPO.replace("/", "--"))
snapshot = folder / "snapshots" / pinned
snapshot.mkdir(parents=True)
(snapshot / "config.json").write_text("{}")
(folder / "refs").mkdir()
(folder / "refs" / "main").write_text(pinned)
check("vaste revisie in de cache: dat pad, zonder netwerk", transcribe.ensure_model(REPO) == str(snapshot),
      transcribe.ensure_model(REPO))

# Een nieuwere revisie op main in de cache verandert niets: de app blijft op de vaste.
newer = "f" * 40
(folder / "snapshots" / newer).mkdir()
(folder / "snapshots" / newer / "config.json").write_text("{}")
(folder / "refs" / "main").write_text(newer)
check("nieuwere main in de cache: toch de vaste revisie", transcribe.ensure_model(REPO) == str(snapshot))

# Zonder de vaste revisie in de cache en zonder netwerk: een nette fout, geen andere revisie.
os.rename(snapshot, snapshot.with_name("weg"))
transcribe.tell = lambda *a, **k: None
try:
    result = transcribe.ensure_model(REPO)
    check("vaste revisie ontbreekt offline: nette fout", False, result)
except transcribe.Problem as problem:
    check("vaste revisie ontbreekt offline: nette fout", "Whisper" in str(problem), problem)

# Een lokaal pad gaat ongewijzigd terug.
check("lokaal pad: ongewijzigd", transcribe.ensure_model(str(cache)) == str(cache))

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
