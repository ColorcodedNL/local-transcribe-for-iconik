"""De instellingenlaag (app/settings.py): het schema, lezen met standaardwaarden, bewaren met controle.

Draaien: uv run --no-project --with pynacl --with requests --with numpy python -B tests/test_settings.py
"""
import json
import sys
import tempfile
import types
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
import transcribe, settings, serve, resources, filters, iconik_api, worker, whisper_mlx  # noqa: E402,E401

transcribe.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"   # nooit de echte voorkeuren aanraken
transcribe.save_config({"app_language": "nl"})
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


def refused(key, value):
    try:
        settings.clean(key, value)
    except settings.SettingError:
        return True
    return False


def raw():
    return json.loads(transcribe.CONFIG_PATH.read_text())


S = settings.SCHEMA

# --- het schema zelf ---
check("schema: elke sleutel is groep.naam", all(k.count(".") == 1 for k in S))
check("schema: groepen zijn bekend", {s.group for s in S.values()} <= {"general", "transcription", "refine", "iconik"},
      {s.group for s in S.values()})
check("schema: elke oude naam één keer", len({s.flat for s in S.values()}) == len(S) and all(s.flat for s in S.values()))
check("schema: label en uitleg in twee talen", all(len(s.label) == 2 and all(s.label) and len(s.help) == 2 for s in S.values()))
check("schema: elke standaardwaarde is zelf geldig",
      all(settings._usable(s, s.default) for s in S.values()), [k for k, s in S.items() if not settings._usable(s, s.default)])
check("schema: 18 instellingen", len(S) == 18, len(S))

# --- standaardwaarden gelijk aan wat de code nu zelf gebruikt ---
check("standaard: ruimtegrenzen gelijk aan resources.DEFAULTS",
      all(S[f"transcription.{k}"].default == v for k, v in resources.DEFAULTS.items()))
check("standaard: langste asset gelijk aan worker.AUTO_MAX_MINUTES", S["iconik.auto_max_minutes"].default == worker.AUTO_MAX_MINUTES)
check("standaard: vooruit lezen gelijk aan serve.PREFETCH_MIN_FREE_GB",
      S["transcription.prefetch_min_free_gb"].default == serve.PREFETCH_MIN_FREE_GB)
check("standaard: te verwachten talen gelijk aan filters.EXPECTED_LANGUAGES",
      tuple(S["transcription.expected_languages"].default) == filters.EXPECTED_LANGUAGES)
check("standaard: Iconik-adres gelijk aan iconik_api.DEFAULT_HOST", S["iconik.host"].default == iconik_api.DEFAULT_HOST)
check("standaard: modelbuffer 2 GB", S["transcription.mlx_cache_gb"].default == 2)
check("standaard: naam van de Mac past in worker.MAX_NAME", S["iconik.machine_name"].max_length >= worker.MAX_NAME)

# --- lezen: niets in het bestand geeft de standaard ---
check("lezen: leeg geeft standaard", settings.get("iconik.auto_transcribe") is False and settings.get("transcription.language") == "auto")
check("lezen: getal komt als float", settings.get("iconik.auto_max_minutes") == 60.0 and isinstance(settings.get("iconik.auto_max_minutes"), float))
try:
    settings.get("bestaat.niet")
    check("lezen: onbekende sleutel geeft KeyError", False)
except KeyError:
    check("lezen: onbekende sleutel geeft KeyError", True)

# --- lezen is ruim, zoals de verspreide .get() van 0.6 ---
transcribe.save_config({**raw(), "auto_max_minutes": 90, "auto_max_gpu": "onzin", "auto_max_cpu": 0, "prefetch_min_free_gb": True,
                        "mounts": {"opslag-1": "/Volumes/ISG"}, "client_id": "c", "expected_languages": []})
check("lezen: geldig getal uit het bestand", settings.get("iconik.auto_max_minutes") == 90.0)
check("lezen: tekst waar een getal hoort geeft standaard", settings.get("transcription.auto_max_gpu") == 40.0)
check("lezen: nul geeft standaard (groter dan nul)", settings.get("transcription.auto_max_cpu") == 40.0)
check("lezen: true is geen getal", settings.get("transcription.prefetch_min_free_gb") == 6.0)
check("lezen: mounts", settings.get("iconik.mounts") == {"opslag-1": "/Volumes/ISG"})
check("lezen: een kopie, het bestand verandert niet mee",
      (lambda m: (m.clear(), settings.get("iconik.mounts"))[1])(settings.get("iconik.mounts")) == {"opslag-1": "/Volumes/ISG"})
check("lezen: client-ID niet streng gecontroleerd bij lezen", settings.get("iconik.client_id") == "c")
check("lezen: lege talenlijst geeft standaard", settings.get("transcription.expected_languages") == ["nl", "en"])

# --- bewaren: streng ---
check("bewaren: aan/uit alleen true/false", refused("iconik.auto_transcribe", "on") and refused("iconik.auto_transcribe", 1))
check("bewaren: keuze buiten de lijst", refused("general.app_language", "de"))
check("bewaren: gesproken taal", not refused("transcription.language", "uk") and refused("transcription.language", "Engels"))
check("bewaren: client-ID moet er een zijn", refused("iconik.client_id", "c") and refused("iconik.client_id", "x" * 36))
check("bewaren: client-ID naar kleine letters",
      settings.clean("iconik.client_id", " ABCDEF12-1234-1234-1234-123456789ABC ") == "abcdef12-1234-1234-1234-123456789abc")
check("bewaren: getal met komma", settings.clean("iconik.auto_max_minutes", "45,5") == 45.5)
check("bewaren: getal niet nul of minder", refused("iconik.auto_max_minutes", 0) and refused("iconik.auto_max_minutes", -3))
check("bewaren: Iconik-adres alleen https", refused("iconik.host", "http://app.iconik.io") and refused("iconik.host", "https://x/pad?a=1")
      and settings.clean("iconik.host", "https://eu.iconik.io") == "https://eu.iconik.io")
check("bewaren: leeg Iconik-adres wordt de standaard", settings.clean("iconik.host", "") == "https://app.iconik.io")
check("bewaren: te lange naam", refused("iconik.machine_name", "x" * 65))
check("bewaren: talen als tekst", settings.clean("transcription.expected_languages", "nl, EN ru") == ["nl", "en", "ru"])
check("bewaren: talen niet leeg", refused("transcription.expected_languages", []))
try:
    settings.put("iconik.auto_max_minutes", "nooit")
    check("bewaren: fout geeft SettingError met melding", False)
except settings.SettingError as error:
    check("bewaren: fout geeft SettingError met melding", "Langste asset" in error.message(), error.message())
before = raw()
check("bewaren: geweigerd verandert het bestand niet", raw() == before)

# --- put schrijft onder de oude naam, de rest blijft staan ---
settings.put("iconik.auto_transcribe", True)
settings.put("iconik.client_id", "ABCDEF12-1234-1234-1234-123456789ABC")
iconik_block = next(v for v in raw()["integrations"].values() if v.get("type") == "iconik")
check("put: in de nieuwe indeling, bij de koppeling", raw().get("version") == 2 and iconik_block.get("auto_transcribe") is True
      and iconik_block.get("client_id", "").startswith("abcdef12") and "auto_transcribe" not in raw(), raw())
check("put: andere instellingen blijven", iconik_block.get("mounts") == {"opslag-1": "/Volumes/ISG"}
      and raw()["general"].get("app_language") == "nl", raw())
check("put: en get leest het terug", settings.get("iconik.auto_transcribe") is True)

# --- beschrijving voor het venster ---
plain = settings.describe()
check("describe: gewone instellingen zonder de geavanceerde", plain and all(not S[i["key"]].advanced for i in plain))
check("describe: per groep", all(i["key"].startswith("iconik.") for i in settings.describe("iconik")))
check("describe: tekst in de taal van de app", next(i for i in plain if i["key"] == "iconik.worker_enabled")["label"] == "Opdrachten op afstand")
check("describe: alles met advanced", len(settings.describe(advanced=True)) == len(S))
check("describe: gewone gegevens (JSON)", bool(json.dumps(settings.describe(advanced=True))))

# --- modelbuffer: 0 betekent geen buffer en blijft 0 ---
transcribe.save_config({**raw(), "mlx_cache_gb": 0})
check("lezen: modelbuffer 0 blijft 0", settings.get("transcription.mlx_cache_gb") == 0)
transcribe.save_config({**raw(), "mlx_cache_gb": -1})
check("lezen: modelbuffer onder 0 geeft standaard", settings.get("transcription.mlx_cache_gb") == 2.0)

# --- niemand leest nog zelf uit config.json, behalve de taal van de app in base (komt eerder dan settings) ---
loose = [f"{f.name}:{n}" for f in sorted(Path("app").glob("*.py")) if f.name not in ("settings.py",)
         for n, line in enumerate(f.read_text().splitlines(), 1)
         if "load_config()" in line and not line.lstrip().startswith(("def ", "#"))
         and not (f.name == "base.py" and '"app_language"' in line) and "config = load_config()" not in line]
check("geen losse lezers van config.json meer in app/", not loose, loose)

# --- een later deel meldt zijn eigen instelling aan ---
check("aanmelden: de engine brengt zijn modelbuffer mee", S.get("transcription.mlx_cache_gb") is whisper_mlx.CACHE_SETTING)
check("aanmelden: settings.py zelf kent geen MLX", "mlx" not in Path("app/settings.py").read_text().lower())
check("aanmelden: dezelfde nog eens is goed", settings.register(whisper_mlx.CACHE_SETTING) is whisper_mlx.CACHE_SETTING
      and len(S) == 18)
try:
    settings.register(settings.Setting("transcription.mlx_cache_gb", "number", 5.0, ("x", "x")))
    check("aanmelden: een andere met dezelfde sleutel wordt geweigerd", False)
except ValueError:
    check("aanmelden: een andere met dezelfde sleutel wordt geweigerd", True)

# --- de omzetting van een plat bestand (0.6) ---
import shutil  # noqa: E402
old_path = transcribe.CONFIG_PATH
folder = Path(tempfile.mkdtemp())
transcribe.CONFIG_PATH = folder / "config.json"
flat = {"app_language": "nl", "language": "uk", "client_id": "abcdef12-1234-1234-1234-123456789abc",
        "host": "https://eu.iconik.io", "mounts": {"opslag-1": "/Volumes/ISG"}, "worker_enabled": True,
        "auto_transcribe": True, "auto_audio": False, "machine_name": "Montage 3", "iconik_language": "en",
        "auto_max_minutes": 90, "auto_min_free_gb": 10, "mlx_cache_gb": 0, "expected_languages": ["nl", "en", "ru"],
        "subtitles": {"rules": {"max_chars": 37}, "formats": "both", "place": "other"}, "iets_onbekends": [1, 2]}
transcribe.CONFIG_PATH.write_text(json.dumps(flat))
before_text = transcribe.CONFIG_PATH.read_text()
check("omzetting: elke instelling leest na de omzetting hetzelfde als ervoor",
      all(settings.get(s.key) == (float(flat[s.flat]) if s.kind == "number" else flat[s.flat])
          for s in S.values() if s.flat in flat),
      {s.key: settings.get(s.key) for s in S.values() if s.flat in flat})
new_file = raw()
check("omzetting: versie 2 met de groepen", new_file.get("version") == 2
      and all(isinstance(new_file.get(g), dict) for g in ("general", "transcription", "refine", "integrations")), new_file)
items = list(new_file["integrations"].items())
check("omzetting: één Iconik-koppeling met een eigen kenmerk", len(items) == 1 and items[0][1]["type"] == "iconik"
      and __import__("re").fullmatch(r"iconik-[0-9a-f]{6}", items[0][0]), items)
check("omzetting: geen platte instellingen meer bovenaan", not any(s.flat in new_file for s in S.values()), list(new_file))
check("omzetting: onbekende sleutel blijft ongemoeid", new_file.get("iets_onbekends") == [1, 2])
check("omzetting: de grenzen voor automatisch werk bij transcriptie", new_file["transcription"].get("auto_min_free_gb") == 10
      and iconik_block is not None and items[0][1].get("auto_max_minutes") == 90)
backup = folder / "config-0.6.json"
check("omzetting: reservekopie config-0.6.json, gelijk aan het oude bestand",
      backup.is_file() and backup.read_text() == before_text)
same_id = items[0][0]
settings.put("iconik.machine_name", "Montage 4")
check("omzetting: maar één keer; het kenmerk blijft", list(raw()["integrations"]) == [same_id] and backup.read_text() == before_text)
transcribe.save_config({**raw(), "auto_audio": True})
check("een losse platte sleutel later (een deel dat later aanmeldt) gaat ook op zijn plek",
      settings.get("iconik.auto_audio") is True and "auto_audio" not in raw() and list(raw()["integrations"]) == [same_id])
check("taal van de app in base leest de nieuwe plek", transcribe.app_language() == "nl"
      and S["general.app_language"].key == "general.app_language")
settings.put("general.app_language", "en")
check("taal van de app in base volgt een wijziging", transcribe.app_language() == "en")
settings.put("general.app_language", "nl")
check("koppelingen voor het venster", settings.integrations() == [{"id": same_id, "type": "iconik"}])
check("waarden per groep voor het venster", settings.values("general") == {"general.app_language": "nl"}
      and settings.values("iconik")["iconik.machine_name"] == "Montage 4")
check("stored: niets bewaard is None, geen standaard", settings.stored("transcription.prefetch_min_free_gb") is None)
settings.update("iconik.mounts", lambda m: m.update({"opslag-2": "/Volumes/B"}))
check("update: mounts aangevuld, de rest bleef", settings.get("iconik.mounts") == {"opslag-1": "/Volumes/ISG", "opslag-2": "/Volumes/B"})

# leeg of ontbrekend bestand: niets schrijven bij alleen lezen
transcribe.CONFIG_PATH = folder / "leeg" / "config.json"
check("geen bestand: standaardwaarden, en er wordt niets geschreven",
      settings.get("iconik.auto_transcribe") is False and not transcribe.CONFIG_PATH.exists())
settings.put("iconik.auto_transcribe", True)
check("geen bestand: het eerste bewaren maakt versie 2, zonder reservekopie",
      raw().get("version") == 2 and settings.get("iconik.auto_transcribe") is True
      and not (folder / "leeg" / "config-0.6.json").exists(), raw())
transcribe.CONFIG_PATH = old_path
shutil.rmtree(folder, ignore_errors=True)

# --- kopjes ---
check("kopjes: elk kopje bestaat", all(not s.section or s.section in settings.SECTIONS for s in S.values()))
auto = [i for i in settings.describe("transcription", advanced=True) if i.get("section", {}).get("id") == "automatic"]
check("kopjes: de drie ruimtegrenzen onder 'Werk dat de app vanzelf oppakt'",
      sorted(i["key"] for i in auto) == ["transcription.auto_max_cpu", "transcription.auto_max_gpu", "transcription.auto_min_free_gb"]
      and auto[0]["section"]["title"] == "Werk dat de app vanzelf oppakt", auto)
check("kopjes: auto transcribe bij Iconik verwijst ernaar", "Transcriptie" in base_help if (base_help := next(
      i["help"] for i in settings.describe("iconik") if i["key"] == "iconik.auto_transcribe")) else False)

# --- terug naar de standaard en wat iemand zelf instelde ---
settings.put("transcription.auto_max_cpu", 55)
check("aangepast: een bewaarde waarde telt als eigen keuze", "transcription.auto_max_cpu" in settings.custom("transcription"))
settings.put("transcription.auto_max_gpu", 40)
check("aangepast: de standaardwaarde bewaren telt niet als aangepast", "transcription.auto_max_gpu" not in settings.custom("transcription"))
check("aangepast: een andere groep telt niet mee", "transcription.auto_max_cpu" not in settings.custom("general"))
check("terugzetten: geeft wat er stond", settings.reset(["transcription.auto_max_cpu"]) == ["transcription.auto_max_cpu"])
check("terugzetten: weg uit het bestand, standaard geldt weer",
      settings.stored("transcription.auto_max_cpu") is None and settings.get("transcription.auto_max_cpu") == 40.0)
check("terugzetten: wat er niet stond is geen fout", settings.reset(["transcription.auto_max_cpu"]) == [])
try:
    settings.reset(["bestaat.niet"])
    check("terugzetten: onbekende sleutel geweigerd", False)
except KeyError:
    check("terugzetten: onbekende sleutel geweigerd", True)
spoken = next(i for i in settings.describe("transcription") if i["key"] == "transcription.language")
check("venster: de gesproken taal heeft een keuzelijst met de geteste talen",
      spoken.get("options", [])[:3] == ["auto", "nl", "en"] and spoken.get("tested") == ["auto", "nl", "en"], spoken)

check("venster: describe zegt welke instellingen geavanceerd zijn",
      {i["key"]: i["advanced"] for i in settings.describe("transcription", advanced=True)}.get("transcription.prefetch_min_free_gb") is True
      and all(i["advanced"] is False for i in settings.describe("transcription")))

# --- een koppeling verwijderen ---
settings.put("iconik.client_id", "11111111-1111-1111-1111-111111111111")
iid = next(i["id"] for i in settings.integrations() if i["type"] == "iconik")
settings.remove_integration(iid)
check("koppeling verwijderen: weg uit config.json met haar instellingen",
      not settings.integrations() and settings.stored("iconik.client_id") is None)
settings.remove_integration(iid)   # twee keer is geen fout

# --- meerdere exemplaren van een koppeling (stap 5a) ---
transcribe.CONFIG_PATH.write_text(json.dumps({"version": 2, "general": {}, "transcription": {}, "refine": {},
                                              "integrations": {}}))
first = settings.add_integration("iconik")
second = settings.add_integration("iconik")
check("meerdere: add_integration maakt twee exemplaren", first != second and settings.instances("iconik") == sorted([first, second])
      and all(raw()["integrations"][i] == {"type": "iconik"} for i in (first, second)), raw())
settings.put("iconik.host", "https://een.example", instance=first)
settings.put_many({"iconik.client_id": "22222222-2222-2222-2222-222222222222", "iconik.auto_transcribe": True,
                   "transcription.language": "nl"}, instance=second)
check("meerdere: bewaren raakt alleen het gekozen exemplaar",
      raw()["integrations"][first] == {"type": "iconik", "host": "https://een.example"}
      and raw()["integrations"][second] == {"type": "iconik", "client_id": "22222222-2222-2222-2222-222222222222",
                                            "auto_transcribe": True}, raw())
check("meerdere: een instelling van de app gaat niet in het exemplaar", raw()["transcription"].get("language") == "nl")
check("meerdere: lezen per exemplaar", settings.get("iconik.host", first) == "https://een.example"
      and settings.get("iconik.host", second) == "https://app.iconik.io"
      and settings.get("iconik.auto_transcribe", second) is True and settings.get("iconik.auto_transcribe", first) is False)
check("meerdere: stored en custom per exemplaar", settings.stored("iconik.host", second) is None
      and settings.custom("iconik", first) == ["iconik.host"] and "iconik.auto_transcribe" in settings.custom("iconik", second))
check("meerdere: values per exemplaar", settings.values("iconik", first)["iconik.host"] == "https://een.example")
settings.update("iconik.mounts", lambda mounts: mounts.update({"s1": "/opslag"}), instance=second)
check("meerdere: update per exemplaar", settings.get("iconik.mounts", second) == {"s1": "/opslag"}
      and settings.get("iconik.mounts", first) == {})
check("meerdere: reset per exemplaar", settings.reset(["iconik.host"], instance=first) == ["iconik.host"]
      and settings.stored("iconik.host", first) is None)
check("meerdere: een instelling van de app leest gewoon zonder exemplaar", settings.get("transcription.language") == "nl")


def raises(what, error=settings.InstanceError):
    try:
        what()
    except error:
        return True
    return False


check("meerdere: zonder exemplaar lezen is een fout, nooit stil het eerste",
      raises(lambda: settings.get("iconik.host")) and raises(lambda: settings.values("iconik")))
check("meerdere: zonder exemplaar bewaren is een fout en bewaart niets",
      raises(lambda: settings.put("iconik.auto_audio", True)) and "auto_audio" not in json.dumps(raw()))
check("meerdere: instance_id zonder keuze is een fout", raises(lambda: settings.instance_id("iconik")))
check("meerdere: een exemplaar dat er niet is: lezen geeft standaard, bewaren is een fout",
      settings.get("iconik.host", "iconik-ffffff") == "https://app.iconik.io"
      and raises(lambda: settings.put("iconik.host", "https://x.example", instance="iconik-ffffff"))
      and "iconik-ffffff" not in raw()["integrations"])
check("meerdere: onbekende soort", raises(lambda: settings.add_integration("nextcloud")))
before = json.dumps(raw())
cfg = raw()
cfg["auto_audio"] = True                       # een platte sleutel die later opduikt, bij twee Iconiks
transcribe.CONFIG_PATH.write_text(json.dumps(cfg))
check("meerdere: een losse platte sleutel van een koppeling wordt niet geraden", settings.get("transcription.language") == "nl"
      and raw().get("auto_audio") is True and all("auto_audio" not in raw()["integrations"][i] for i in (first, second)), raw())
cfg.pop("auto_audio")
transcribe.CONFIG_PATH.write_text(before)
settings.remove_integration(second)
check("meerdere: na weghalen van één weer zonder exemplaar te lezen",
      settings.instance_id("iconik") == first and settings.get("iconik.host") == "https://app.iconik.io")
settings.remove_integration(first)

# --- via de vaste ingang ---
check("transcribe kent settings als deel", settings in transcribe.PARTS
      and transcribe.PARTS.index(settings) == transcribe.PARTS.index(transcribe.base) + 1)

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
