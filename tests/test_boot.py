"""main.py (boot): welke code wordt gekozen. Draaien vanuit de projectmap:

    python3 tests/test_boot.py
"""
import importlib.util, json, os, tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("boot", "app/main.py")
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


# Met de controle van shell 5: wat de lader teruggeeft telt, niet wat er los in de map staat.
def with_loader(name, expect, loaded, shell=1):
    global fails
    root = Path(tempfile.mkdtemp()); here = root / "app"; code = root / "code"; here.mkdir(); (code / "0.2.1").mkdir(parents=True)
    (here / "manifest.json").write_text(json.dumps({"version": "0.2.0"}))
    (code / "current.json").write_text(json.dumps({"version": "0.2.1"}))
    (code / "0.2.1" / "manifest.json").write_text(json.dumps({"version": "0.2.1", "requires_shell": 1}))
    (code / "0.2.1" / "app_main.py").write_text("")
    got = boot.choose_code(here, code, shell, vt, lambda folder, version: loaded)
    ok = (got[1] if got else None) == expect and (got is None or got[2] is loaded[0])
    fails += not ok
    print(("OK   " if ok else "FOUT ") + name + f" -> {got[1] if got else None}")


good = {"manifest.json": json.dumps({"version": "0.2.1", "requires_shell": 1}).encode(), "app_main.py": b""}
with_loader("lader: gecontroleerde update", "0.2.1", (good, None))
with_loader("lader: afgekeurd", None, (None, "de handtekening klopt niet"))
with_loader("lader: manifest in de zip zegt andere versie", None, (dict(good, **{"manifest.json": b'{"version": "9.9.9"}'}), None))
with_loader("lader: manifest in de zip vraagt nieuwere shell", None, (dict(good, **{"manifest.json": b'{"version": "0.2.1", "requires_shell": 2}'}), None))
with_loader("lader: app_main.py niet in de zip", None, ({"manifest.json": good["manifest.json"]}, None))
with_loader("lader: manifest in de zip is onzin", None, (dict(good, **{"manifest.json": b"{"}), None))
print("ALLES OK" if not fails else f"{fails} FOUT")
raise SystemExit(1 if fails else 0)
