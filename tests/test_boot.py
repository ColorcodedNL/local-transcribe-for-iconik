"""main.py (boot): welke code wordt gekozen. Draaien vanuit de projectmap:

    python3 tests/test_boot.py
"""
import importlib.util, json, os, tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("boot", "main.py")
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)
boot.log = lambda text: print("    log:", text.splitlines()[0])
vt = lambda t: tuple((list(map(int, t.split(".")[:3])) + [0, 0, 0])[:3])
fails = 0


def scenario(name, expect, bundled="0.2.0", current="0.2.1", manifest=None, app_main=True, marker=None, bad=None, shell=1):
    global fails
    root = Path(tempfile.mkdtemp()); here = root / "app"; code = root / "code"; here.mkdir(); code.mkdir()
    if bundled: (here / "manifest.json").write_text(json.dumps({"version": bundled}))
    if current is not None: (code / "current.json").write_text(json.dumps({"version": current}))
    if current and "/" not in current:
        d = code / current; d.mkdir()
        (d / "manifest.json").write_text(json.dumps(manifest or {"version": current, "requires_shell": 1}))
        if app_main: (d / "app_main.py").write_text("")
    if marker: (code / "starting").write_text(marker)
    if bad: (code / "bad.json").write_text(json.dumps(bad))
    got = boot.choose_code(here, code, shell, vt)
    got = got[1] if got else None
    fails += got != expect
    extra = ""
    if marker:
        extra = f" marker weg={not (code / 'starting').exists()} bad={json.loads((code / 'bad.json').read_text()) if (code / 'bad.json').exists() else []}"
    print(("OK   " if got == expect else "FOUT ") + name + f" -> {got}" + extra)


scenario("geen bundel (ontwikkelmap)", None, bundled=None)
scenario("nieuwere update", "0.2.1")
scenario("update even oud als bundel", None, current="0.2.0")
scenario("update ouder dan bundel (na nieuwe dmg)", None, bundled="0.3.0")
scenario("vraagt nieuwere shell", None, manifest={"version": "0.2.1", "requires_shell": 2})
scenario("manifest met andere versie", None, manifest={"version": "9.9.9"})
scenario("app_main.py ontbreekt", None, app_main=False)
scenario("op de lijst met mislukte versies", None, bad=["0.2.1"])
scenario("padmanipulatie in versie", None, current="../x")
scenario("geen current.json", None, current=None)
scenario("vorige start mislukt (proces weg)", None, marker="0.2.1 999999")
scenario("start loopt nog (eigen pid)", "0.2.1", marker=f"0.2.1 {os.getpid()}")
scenario("marker van andere versie", "0.2.1", marker="0.2.0 999999")
print("ALLES OK" if not fails else f"{fails} FOUT")
raise SystemExit(1 if fails else 0)
