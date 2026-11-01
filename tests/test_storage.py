"""Bewaking van de opslag (ISG) bij auto transcribe: is het mount point er, en wanneer waarschuwt de app.

Draaien: uv run --no-project --with pynacl --with requests python -B tests/test_storage.py
"""
import os
import sys
import tempfile
import time
import types
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


# --- mount_reachable, met een nagemaakte /Volumes ---
volumes = Path(tempfile.mkdtemp())
(volumes / "Opslag" / "Archief").mkdir(parents=True)        # lege achtergebleven map, niet gekoppeld
(volumes / "Opslag-1" / "Archief").mkdir(parents=True)
(volumes / "Ander").mkdir()
mounted = set()
real_ismount = os.path.ismount
os.path.ismount = lambda p: str(p) in mounted
check("achtergebleven map telt niet als gekoppeld",
      not transcribe.mount_reachable(volumes / "Opslag" / "Archief", volumes))
mounted.add(str(volumes / "Opslag"))
check("gekoppeld volume met de map: bereikbaar", transcribe.mount_reachable(volumes / "Opslag" / "Archief", volumes))
check("gekoppeld maar de map ontbreekt: niet bereikbaar",
      not transcribe.mount_reachable(volumes / "Opslag" / "Weg", volumes))
mounted = {str(volumes / "Opslag-1")}
check("opnieuw gekoppeld als Opslag-1: bereikbaar", transcribe.mount_reachable(volumes / "Opslag" / "Archief", volumes))
mounted = {str(volumes / "Ander")}
check("ander volume telt niet mee", not transcribe.mount_reachable(volumes / "Opslag" / "Archief", volumes))
check("het volume zelf als mount point", transcribe.mount_reachable(volumes / "Ander", volumes))
os.path.ismount = real_ismount
local = Path(tempfile.mkdtemp())
check("map op een eigen schijf: bestaat hij, dan is hij er", transcribe.mount_reachable(local, volumes)
      and not transcribe.mount_reachable(local / "weg", volumes))

# --- tijdslimiet: een koppeling die hangt ---
real_reach = transcribe.mount_reachable
transcribe.mount_reachable = lambda p: time.sleep(1.5) or True
t = time.monotonic()
check("hangt: telt als weg na de tijdslimiet", serve.path_reachable("/hangt", timeout=0.2) is False)
check("hangt nog: geen tweede draad, meteen weg", serve.path_reachable("/hangt", timeout=0.2) is False
      and time.monotonic() - t < 1.0)
time.sleep(1.5)
transcribe.mount_reachable = lambda p: True
check("klaar met hangen: weer gewoon kijken", serve.path_reachable("/hangt", timeout=0.2) is True)

# --- check_storage: wanneer de waarschuwing komt ---
online = {"/Volumes/A": True, "/Volumes/B": True}
transcribe.mount_reachable = lambda p: online[p]
transcribe.save_config({"mounts": {"a": "/Volumes/A", "b": "/Volumes/B"}})
warnings = []
gone_seen = []   # seintjes voor het venster (de gele balk), los van de meldingen
serve.storage_hook = lambda kind, names: (gone_seen if kind == "gone" else warnings).append((kind, names))
serve.storage.update(offline=[], checked=0.0, warned=[])
state = {"round": 0, "suspect": []}

online["/Volumes/B"] = False
wait = serve.check_storage(state)
check("opstarten, B weg: werker pakt B meteen niet, nog geen melding (macOS koppelt misschien nog)",
      serve.storage["offline"] == ["b"] and not warnings and wait == serve.STORAGE_RECHECK, (serve.storage, warnings, wait))
serve.check_storage(state)
check("eerste keer weg: een seintje voor het venster", gone_seen == [("gone", ["/Volumes/B"])], gone_seen)
serve.check_storage(state)
check("tweede keer nog weg: melding bij opstarten, geen tweede seintje",
      warnings == [("startup", ["/Volumes/B"])] and len(gone_seen) == 1, (warnings, gone_seen))
serve.check_storage(state)
wait = serve.check_storage(state)
check("blijft weg: geen tweede melding, gewoon tempo", len(warnings) == 1 and wait == serve.STORAGE_CHECK, (warnings, wait))
online["/Volumes/B"] = True
serve.check_storage(state)
check("terug: melding weer bereikbaar, werker mag weer", warnings[-1] == ("back", ["/Volumes/B"])
      and serve.storage["offline"] == [] and serve.storage["warned"] == [], (warnings, serve.storage))
warnings.clear()
online["/Volumes/A"] = False
wait = serve.check_storage(state)
check("tijdens gebruik even weg: nog geen melding, wel snel nog eens kijken", not warnings
      and serve.storage["offline"] == ["a"] and wait == serve.STORAGE_RECHECK, (warnings, wait))
online["/Volumes/A"] = True
serve.check_storage(state)
check("hapering voorbij: nooit een melding", not warnings, warnings)
online["/Volumes/A"] = False
serve.check_storage(state)
serve.check_storage(state)
check("echt weg: melding verbinding kwijt", warnings == [("lost", ["/Volumes/A"])], warnings)

serve.storage_hook = None
serve.storage.update(offline=[], checked=0.0, warned=[])
state = {"round": 0, "suspect": []}
serve.check_storage(state)
serve.check_storage(state)
check("zonder venster: melding bewaard tot het venster er is", serve.storage_pending == [("startup", ["/Volumes/A"])],
      serve.storage_pending)

# --- het venster ziet het meteen, niet pas bij de volgende ronde van de werker ---
serve.storage_pending.clear()
serve.storage_hook = lambda kind, names: None
serve.meter = types.SimpleNamespace(start=lambda: None, check=lambda limits: (True, "", {}))
transcribe.save_config({"mounts": {"a": "/Volumes/A", "b": "/Volumes/B"}, "auto_transcribe": True})
online.update({"/Volumes/A": True, "/Volumes/B": True})
serve.storage.update(offline=[], checked=0.0, warned=[])
serve.remote.state["auto"] = None
state = {"round": 0, "suspect": []}
serve.check_storage(state)
online["/Volumes/B"] = False
serve.check_storage(state)
check("opslag weg: venster weet het na deze ronde", (serve.remote.state.get("auto") or {}).get("offline") == ["/Volumes/B"],
      serve.remote.state.get("auto"))
online["/Volumes/B"] = True
serve.check_storage(state)
check("opslag terug: balk weg na deze ronde", not (serve.remote.state.get("auto") or {}).get("offline"),
      serve.remote.state.get("auto"))
transcribe.mount_reachable = real_reach

print("\nAlles goed." if not fails else f"\n{fails} fout(en).")
sys.exit(1 if fails else 0)
