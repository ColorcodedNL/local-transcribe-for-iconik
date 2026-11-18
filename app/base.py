"""Basis van de app: namen, paden, logregels, voortgang, taal, fouten, voorkeuren en het bewaren van transcripten.

Geen kennis van Iconik, audio of modellen; alle andere delen bouwen hierop.
"""
import builtins
import json
import os
import re
import subprocess
import sys
import threading
import time
import transcript as tmodel
import system



# Niets naar buiten behalve Iconik zelf (en eenmalig de modeldownload). pyannote 4 stuurt
# standaard gebruiksgegevens naar otel.pyannote.ai; dat zetten we uit voordat het geladen wordt.
os.environ["PYANNOTE_METRICS_ENABLED"] = "false"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
# Twee namen: de interne naam verandert nooit (support-map, logs, keychain), de weergavenaam mag later anders
# worden zonder dat er gegevens of een inlog hoeven te verhuizen.
INTERNAL_NAME = "Local Transcribe for Iconik"
DISPLAY_NAME = "Local Transcribe for Iconik"   # venster, meldingen, User-Agent
OLD_APP_NAME = "Iconik Transcribe"  # de naam tot oktober 2026; voorkeuren en inlog verhuizen mee
SUPPORT_DIR = system.support_dir(INTERNAL_NAME)
try:
    _old_dir = SUPPORT_DIR.with_name(OLD_APP_NAME)
    if _old_dir.is_dir() and not SUPPORT_DIR.exists():
        _old_dir.rename(SUPPORT_DIR)
except OSError:
    pass
KEYCHAIN_SERVICE = INTERNAL_NAME
CONFIG_PATH = SUPPORT_DIR / "config.json"
SIDECARS_FILE = SUPPORT_DIR / "sidecars.json"   # welke ondertitelbestanden de app zelf schreef (pad en hash)
ENGINE_NAME = "mlx-whisper"  # hieraan herkent --replace de transcripten van dit script
REFINED_ENGINE = "local-transcribe-refined"   # een transcript dat iemand in Refine heeft gecorrigeerd
SAMPLE_RATE = 16000


LISTENER = None  # de ontvanger zet hier een functie neer om de voortgang te volgen

# Vooruit lezen draait naast een andere opdracht. Die draad zet hier lijsten neer, zodat zijn
# meldingen en logregels bewaard worden tot zijn eigen opdracht begint.
_local = threading.local()


def print(*args, **kwargs):  # noqa: A001 - bewust: logregels van het vooruit lezen bewaren
    lines = getattr(_local, "lines", None)
    if lines is not None and "file" not in kwargs:
        lines.append(kwargs.get("sep", " ").join(str(a) for a in args))
        return
    builtins.print(*args, **kwargs)


MEMORY_PROBES = []   # functies die een korte geheugentekst geven; een engine zet er een bij
_meter = {"peak": 0, "since": None, "thread": None}



def memory_report(stage):
    """Logregel met geheugen en tijd sinds de vorige meting, ook de piek daartussen."""
    if getattr(_local, "lines", None) is not None:
        return  # vooruit lezen: de metingen horen bij de opdracht die nu loopt
    try:
        now = time.time()
        if _meter["thread"] is None:
            def sample():
                while True:
                    _meter["peak"] = max(_meter["peak"], system.process_footprint())
                    time.sleep(0.25)
            _meter["thread"] = threading.Thread(target=sample, daemon=True)
            _meter["thread"].start()
        current = system.process_footprint()
        peak = max(_meter["peak"], current)
        since = _meter["since"] or now
        parts = [f"nu {current / 1e9:.1f} GB", f"piek {peak / 1e9:.1f} GB", f"{now - since:.1f} s"]
        for probe in MEMORY_PROBES:   # de engine meldt zijn eigen geheugen (engine.py)
            try:
                text = probe()
            except Exception:
                text = None
            if text:
                parts.append(text)
        torch = sys.modules.get("torch")
        if torch is not None and torch.backends.mps.is_available():
            # driver_allocated telt al het Metal-geheugen van het proces, dus ook Whisper in MLX
            parts.append(f"torch {torch.mps.current_allocated_memory() / 1e9:.1f} / "
                         f"Metal totaal {torch.mps.driver_allocated_memory() / 1e9:.1f} GB")
        print(f"Meting {stage}: " + ", ".join(parts), flush=True)
        _meter["peak"], _meter["since"] = 0, now
    except Exception as error:
        print(f"Meting {stage}: lukte niet ({type(error).__name__}: {error})")


def tell(kind, **data):
    """Meldt voortgang aan wie meeluistert; mag de verwerking nooit storen.
    De sporen worden ook per draad onthouden: die gaan mee in het transcript (prepare_asset)."""
    if kind == "tracks":
        _local.tracks = data.get("tracks")
    events = getattr(_local, "events", None)
    if events is not None:
        if kind == "alive":
            return   # vooruit lezen is niet de lopende opdracht: niet bewaren
        events.append((kind, data))
        return
    if LISTENER:
        try:
            LISTENER(kind, **data)
        except Exception:
            pass

# Bestanden op een asset die geen media zijn.
SIDECAR_EXTENSIONS = {".srt", ".vtt", ".json", ".xml", ".txt", ".csv", ".edl"}

# Alleen deze bestanden gaan naar ffmpeg. Op een asset staan ook plaatjes, fonts, pdf's, indexbestanden
# (.lvix) en Premiere-projecten met projectbestanden zonder extensie (archiefproef van 26 oktober).
MEDIA_EXTENSIONS = {
    ".mxf", ".mov", ".mp4", ".m4v", ".mts", ".m2ts", ".ts", ".mpg", ".mpeg", ".m2v", ".vob", ".avi",
    ".mkv", ".webm", ".wmv", ".dv", ".3gp", ".flv",
    ".wav", ".bwf", ".aif", ".aiff", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
}


class NeedsFolder(Exception):
    """Het mount point is onbekend en er mag nu geen venster komen."""


class Problem(Exception):
    """Een fout die we de gebruiker in gewone woorden kunnen melden."""


def fail(message, skip=False, local=False):
    """`skip`: het asset is overgeslagen om een gewone reden, er is niets misgegaan.
    `local`: het ligt aan deze Mac (opslag niet gemount, geen inlog, geen internet) en niet aan
    het asset. Een werker zet zo'n asset dan terug in de rij voor een andere Mac."""
    problem = Problem(message)
    problem.skip = skip
    problem.local = local
    raise problem


_system_language = []


def app_language():
    """De taal van de app: de keuze uit de voorkeuren, anders die van macOS."""
    try:
        choice = load_config().get("app_language", "auto")
    except Exception:
        choice = "auto"
    if choice in ("nl", "en"):
        return choice
    if not _system_language:
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                                 capture_output=True, text=True, timeout=5).stdout
            first = re.search(r"[A-Za-z]{2}", out)
            _system_language.append("nl" if first and first.group().lower() == "nl" else "en")
        except Exception:
            _system_language.append("en")
    return _system_language[0]


def L(dutch, english):
    """Een melding voor de gebruiker, in de taal van de app."""
    return dutch if app_language() == "nl" else english


CONFIG_LOCK = threading.RLock()


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_config(config):
    """Schrijft via een tijdelijk bestand: wie tegelijk leest, ziet de oude of de nieuwe versie, nooit een halve."""
    with CONFIG_LOCK:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        temp.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        os.replace(temp, CONFIG_PATH)


def update_config(change):
    """Leest de voorkeuren, past ze aan met `change(config)` en schrijft ze weg, in één keer.
    Zo gaat er geen wijziging verloren die een andere draad intussen deed."""
    with CONFIG_LOCK:
        config = load_config()
        change(config)
        save_config(config)
        return config


def set_config(key, value):
    update_config(lambda config: config.__setitem__(key, value))


TRANSCRIPTS_DIR = SUPPORT_DIR / "transcripts"
WAVES_DIR = SUPPORT_DIR / "waves"


def save_transcript(doc):
    """Bewaart een kopie van het transcript in het eigen model op deze Mac, met de sporenanalyse die Iconik
    niet kent. Voor de editor (Refine) en later voor het bijstellen op basis van correcties. Mag nooit een
    opdracht laten mislukken: het transcript staat dan al in Iconik."""
    try:
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{doc.source.get('asset_id') or 'zonder-id'}.json")
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        target = TRANSCRIPTS_DIR / name
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(json.dumps(tmodel.to_dict(doc), ensure_ascii=False))
        os.replace(temp, target)
    except Exception as error:
        print(f"Kopie van het transcript bewaren lukte niet: {error}")


def safe_name(asset_id, suffix):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{asset_id or 'zonder-id'}{suffix}")
