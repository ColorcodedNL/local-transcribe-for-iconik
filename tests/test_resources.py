"""Tests voor resources.py: is er ruimte voor een automatische opdracht? Geen echte metingen.

Draaien: python3 -B tests/test_resources.py
"""
import sys

sys.path.insert(0, "app")
import resources  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(("OK    " if ok else "FOUT  ") + label + (f"  ({detail})" if detail and not ok else ""))


# --- uitlezen van macOS-uitvoer
outputs = {}
resources._run = lambda args: outputs.get(args[0], "")

outputs["pmset"] = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining present: true\n"
check("netstroom herkend", resources.on_mains_power() is True)
outputs["pmset"] = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t80%; discharging\n"
check("batterij herkend", resources.on_mains_power() is False)
outputs["pmset"] = ""
check("onbekend is None", resources.on_mains_power() is None)

outputs["vm_stat"] = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              100000.
Pages active:                            900000.
Pages inactive:                          500000.
Pages speculative:                        50000.
Pages throttled:                              0.
Pages wired down:                        300000.
Pages purgeable:                          10000.
"""
outputs["sysctl"] = "1\n"
available, level = resources.memory()
check("beschikbaar = vrij + inactief + speculatief + opschoonbaar", available == (100000 + 500000 + 50000 + 10000) * 16384, available)
check("geheugendruk normaal", level == 1)
outputs["vm_stat"] = ""
outputs["sysctl"] = ""
check("niet te lezen is None", resources.memory() == (None, None))

outputs["ioreg"] = '''+-o AGXAcceleratorG15X  <class AGXAcceleratorG15X>
    "PerformanceStatistics" = {"In use system memory"=123,"Device Utilization %"=37,"Renderer Utilization %"=30}
'''
check("GPU-bezetting uit ioreg", resources.gpu_percent() == 37)
outputs["ioreg"] = ""
check("geen GPU-waarde is None", resources.gpu_percent() is None)

# --- de meter
clock = [1000.0]
state = {"cpu": 10.0, "gpu": 5.0, "power": True, "free": 30e9, "pressure": 1, "busy": False}
meter = resources.Meter(lambda: state["busy"], cpu=lambda: state["cpu"], gpu=lambda: state["gpu"],
                        power=lambda: state["power"], mem=lambda: (state["free"], state["pressure"]), clock=lambda: clock[0])


def run_for(seconds):
    for _ in range(int(seconds // resources.SAMPLE_SECONDS)):
        meter.sample()
        clock[0] += resources.SAMPLE_SECONDS


ok, reason, _ = meter.check()
check("zonder metingen: nog aan het meten", not ok and reason == "measuring", reason)
run_for(40)
check("na 40 s: nog aan het meten", meter.check()[1] == "measuring")
run_for(30)
ok, reason, values = meter.check()
check("na ruim een minuut rustig: klaar", ok and reason is None, (reason, values))
check("waarden worden meegegeven", round(values["cpu"]) == 10 and round(values["gpu"]) == 5 and round(values["free_gb"]) == 30)

state["power"] = False
check("op batterij: niet", meter.check()[1] == "power")
state["power"] = None
check("stroom onbekend: niet", meter.check()[1] == "power")
state["power"] = True
state["free"] = 8e9
check("te weinig geheugen vrij: niet", meter.check()[1] == "memory")
check("grens is instelbaar", meter.check({"auto_min_free_gb": 6})[0])
state["free"] = 30e9
state["pressure"] = 2
check("geheugendruk boven normaal: niet", meter.check()[1] == "pressure")
state["pressure"] = 1

# Een piek van 10 s telt niet, aanhoudend gebruik wel.
state["cpu"] = 95.0
run_for(10)
state["cpu"] = 10.0
check("één piek van 10 s telt niet", meter.check()[0], meter.check())
state["gpu"] = 70.0
run_for(180)
check("aanhoudend GPU-gebruik (bijvoorbeeld montage): niet", meter.check()[1] == "gpu")
check("GPU-grens is instelbaar", meter.check({"auto_max_gpu": 80})[0])
state["gpu"] = 5.0
state["cpu"] = 60.0
run_for(180)
check("aanhoudend CPU-gebruik: niet", meter.check()[1] == "cpu")
state["cpu"] = 10.0
run_for(180)
check("na drie rustige minuten weer klaar", meter.check()[0])

# Metingen tijdens eigen werk tellen niet: na een eigen job eerst een minuut meten.
state["busy"] = True
state["cpu"] = state["gpu"] = 90.0
run_for(600)
check("tijdens eigen werk: geen oordeel op eigen metingen", meter.check()[1] == "measuring")
state["busy"] = False
state["cpu"], state["gpu"] = 10.0, 5.0
run_for(30)
check("30 s na eigen werk: nog aan het meten", meter.check()[1] == "measuring")
run_for(40)
check("ruim een minuut na eigen werk: klaar, eigen piek telt niet", meter.check()[0], meter.check())

state["gpu"] = None
check("zonder GPU-meting beslist de rest", meter.check()[0])

# --- elk bestand in app/ gaat mee in de bundel en in een code-update
import re  # noqa: E402
from pathlib import Path  # noqa: E402
listed = re.search(r"^CODE_FILES = \[(.*?)\]", Path("tools/make_manifest.py").read_text(), re.M).group(1)
shell = re.search(r"^SHELL_FILES = \[(.*?)\]", Path("tools/make_manifest.py").read_text(), re.M).group(1)
names = set(re.findall(r'"([^"]+)"', listed)) | set(re.findall(r'"([^"]+)"', shell))
in_app = {p.name for p in Path("app").iterdir() if p.suffix in (".py", ".html")}
check("elk bestand in app/ staat in make_manifest.py (anders ontbreekt het in bundel en update)",
      in_app <= names, sorted(in_app - names))

print()
print(f"{sum(results)} van {len(results)} goed")
sys.exit(0 if all(results) else 1)
