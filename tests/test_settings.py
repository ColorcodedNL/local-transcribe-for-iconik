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
check("put: onder de oude naam", raw().get("auto_transcribe") is True and raw().get("client_id").startswith("abcdef12"))
check("put: andere instellingen blijven", raw().get("mounts") == {"opslag-1": "/Volumes/ISG"} and raw().get("app_language") == "nl")
check("put: en get leest het terug", settings.get("iconik.auto_transcribe") is True)

# --- beschrijving voor het venster ---
plain = settings.describe()
check("describe: gewone instellingen zonder de geavanceerde", plain and all(not S[i["key"]].advanced for i in plain))
check("describe: per groep", all(i["key"].startswith("iconik.") for i in settings.describe("iconik")))
check("describe: tekst in de taal van de app", next(i for i in plain if i["key"] == "iconik.worker_enabled")["label"] == "Opdrachten op afstand")
check("describe: alles met advanced", len(settings.describe(advanced=True)) == len(S))
check("describe: gewone gegevens (JSON)", bool(json.dumps(settings.describe(advanced=True))))

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

# --- via de vaste ingang ---
check("transcribe kent settings als deel", settings in transcribe.PARTS
      and transcribe.PARTS.index(settings) == transcribe.PARTS.index(transcribe.base) + 1)

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
