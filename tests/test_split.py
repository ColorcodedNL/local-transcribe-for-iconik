"""De splitsing van transcribe.py (november 2026): transcribe is nu een vaste ingang voor losse delen.

Controleert dat alles wat transcribe vroeger had er nog is, dat transcribe.X = ... aankomt in het deel dat X
gebruikt, dat lezen altijd de actuele waarde geeft, dat de delen geen kring vormen, dat ze allemaal in een
code-update meegaan en dat ze ook laden zoals bij een code-update (uit het geheugen, via app_shell).

Draaien: uv run --no-project --with pynacl --with requests --with numpy python -B tests/test_split.py
"""
import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "app")
import transcribe  # noqa: E402
import base  # noqa: E402
import audio  # noqa: E402
import pipeline  # noqa: E402
import iconik_api  # noqa: E402

# make_manifest.py draait bij importeren meteen; daarom de lijst uit de tekst halen
CODE_FILES = ast.literal_eval(re.search(r"^CODE_FILES = (\[.*?\])", Path("tools/make_manifest.py").read_text(), re.M).group(1))
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


# Alle namen op het hoogste niveau van transcribe.py vlak voor de splitsing (commit e7a4f0b), behalve
# _footprint: dat is system.process_footprint geworden (platformlaag).
OLD_NAMES = """
argparse base64 builtins hashlib json mmap os re secrets subprocess sys tempfile threading time
pkg_version Path np requests tmodel DEFAULT_HOST REDIRECT_URI OAUTH_SCOPE INTERNAL_NAME DISPLAY_NAME
APP_VERSION RELEASES_REPO OLD_APP_NAME SUPPORT_DIR KEYCHAIN_SERVICE BULK_LIMIT CONFIG_PATH
SIDECARS_FILE PAGE_SIZE ENGINE_NAME REFINED_ENGINE SAMPLE_RATE TRACK_SILENT_DB TIMECODE_REGULARITY
TIMECODE_SHARE TRACK_COPY_CORRELATION MIC_ACTIVE_DB MIC_DOMINANT_DB MIC_MIN_DOMINANCE
MIC_MIN_MARGIN_DB DIARIZATION_MODEL _BUNDLE_RES _BUNDLED_SPEAKER_MODEL LOCAL_SPEAKER_MODEL LISTENER
_local print _meter memory_report tell envelope WAVE_RATE WAVES_FORMAT wave_peaks
write_waves read_waves track_info speaker_model_available SENTENCE_END REPEAT_LIMIT VOICE_LONG_TURN
VOICE_MAX_CROP VOICE_MIN_SECONDS VOICE_MIN_SENTENCE VOICE_MIN_MARGIN CLUSTER_MIN_SENTENCE
CLUSTER_DISTANCE SIDECAR_EXTENSIONS MEDIA_EXTENSIONS FIRST_WORD_MIN_PROBABILITY MIN_SPEECH_SHARE
MIN_CONFIDENCE EXPECTED_LANGUAGES MIN_WORDS_OTHER_LANGUAGE HALLUCINATION_PATTERNS LANGUAGE_WINDOWS
NeedsFolder Problem fail _system_language app_language L LOGIN_LOCK api_host
_login_read load_login save_login clear_login HAS_LOGIN_SECONDS _has_login has_login
token_request start_login finish_login Iconik make_client delete_transcription find_version
CONFIG_LOCK load_config save_config update_config set_config set_mount ask_folder find_moved_mount
mount_reachable find_media_file list_tracks media_seconds PCM_BLOCK ALIVE_EVERY PCM_READ pcm_block
read_pcm read_track measure_track classify_track read_all_tracks SIMILARITY_STEP similarity
analyse_tracks speaker_from_tracks load_audio_tracks _pipeline release_models diarize speaker_at
split_sentences drop_repeats VOICE_CACHE_EVERY empty_gpu_cache make_voiceprint refine_speakers
cluster_sentences speech_share MODEL_REVISIONS ensure_model detect_language is_hallucination
to_iconik_segment main prepare_asset TRANSCRIPTS_DIR WAVES_DIR save_transcript safe_name save_waves
expected_languages nothing_written process_asset
""".split()

PART_NAMES = [p.__name__ for p in transcribe.PARTS]

# --- alles nog bereikbaar ---
missing = [n for n in OLD_NAMES if not hasattr(transcribe, n)]
check(f"alle {len(OLD_NAMES)} oude namen bereikbaar via transcribe", not missing, missing)
check("Problem is overal dezelfde klasse", transcribe.Problem is base.Problem is iconik_api.Problem)
check("from transcribe import ... werkt", __import__("transcribe", fromlist=["Iconik"]).Iconik is iconik_api.Iconik)

# --- toewijzen komt aan waar de code het gebruikt ---
old_path = transcribe.CONFIG_PATH
with tempfile.TemporaryDirectory() as tmp:
    transcribe.CONFIG_PATH = Path(tmp) / "config.json"
    check("toewijzen: CONFIG_PATH in base", base.CONFIG_PATH == Path(tmp) / "config.json")
    transcribe.set_config("proef", 1)
    check("toewijzen: load_config leest het nieuwe pad", transcribe.load_config() == {"proef": 1}
          and (Path(tmp) / "config.json").exists())
transcribe.CONFIG_PATH = old_path
heard = []
quiet = lambda *a, **k: heard.append(a)  # noqa: E731
real_print = base.print
transcribe.print = quiet
check("toewijzen: print in elk deel dat print kent", all(getattr(p, "print", quiet) is quiet for p in transcribe.PARTS))
audio.print("x")
check("toewijzen: audio gebruikt het nieuwe print", heard == [("x",)])
transcribe.print = real_print
check("toewijzen: terugzetten werkt ook", pipeline.print is real_print and audio.print is real_print)
transcribe.NIEUW_IN_TEST = 5
check("toewijzen: een nieuwe naam blijft op transcribe zelf", transcribe.NIEUW_IN_TEST == 5
      and not any("NIEUW_IN_TEST" in p.__dict__ for p in transcribe.PARTS))
del transcribe.NIEUW_IN_TEST

# --- lezen is live ---
base.LISTENER = "luisteraar"
check("lezen: een waarde die een deel zelf verandert, ziet transcribe meteen", transcribe.LISTENER == "luisteraar")
base.LISTENER = None
transcribe.LISTENER = "via ingang"
check("lezen en toewijzen: LISTENER in base en pipeline", base.LISTENER == pipeline.LISTENER == "via ingang")
transcribe.LISTENER = None

# --- geen kringen: elk deel importeert alleen uit eerdere delen ---
for i, name in enumerate(PART_NAMES):
    tree = ast.parse(Path(f"app/{name}.py").read_text())
    wrong = [n.module for n in tree.body if isinstance(n, ast.ImportFrom) and n.module in PART_NAMES
             and PART_NAMES.index(n.module) >= i]
    wrong += [a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names
              if a.name == "transcribe" or (a.name in PART_NAMES and PART_NAMES.index(a.name) >= i)]
    check(f"volgorde: {name} importeert alleen uit eerdere delen", not wrong, wrong)
    check(f"volgorde: {name} importeert transcribe niet", "transcribe" not in
          {getattr(n, "module", None) for n in tree.body if isinstance(n, ast.ImportFrom)})

# --- connectors (stap 5c): Iconik en Refine praten niet via de ingang transcribe ---
for name in ("iconik.py", "iconik_queue.py", "refine.py", "connectors.py", "keychain.py", "worker.py"):
    tree = ast.parse(Path(f"app/{name}").read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    check(f"connectors: {name} importeert transcribe niet", "transcribe" not in imported, sorted(imported))
check("code-update: iconik_queue.py in CODE_FILES", "iconik_queue.py" in CODE_FILES)

# --- code-updates en buildscripts ---
check("code-update: alle delen in CODE_FILES", all(f"{n}.py" in CODE_FILES for n in PART_NAMES + ["transcribe"]))
version_line = re.search(r'^APP_VERSION = "([0-9.]+)"', Path("app/transcribe.py").read_text(), re.M)
check("build: APP_VERSION staat nog in transcribe.py", version_line and version_line.group(1) == transcribe.APP_VERSION)
import engine  # noqa: E402
import whisper_mlx  # noqa: E402
NAMES_IN_FORM = ["NAME", "DEFAULT_MODEL", "ensure_model", "start", "detect_language", "transcribe",
                 "clear_working_memory", "release", "info", "revision"]
for name, module in engine.ENGINES.items():
    missing = [n for n in NAMES_IN_FORM if not hasattr(module, n)]
    check(f"engine {name}: heeft de vaste vorm uit engine.py", not missing and module.NAME == name, missing)
check("engine: standaard is Whisper op MLX, met de oude naam voor Iconik",
      engine.get() is whisper_mlx and whisper_mlx.NAME == "mlx-whisper" == base.ENGINE_NAME)
try:
    engine.get("bestaat-niet")
    check("engine: onbekende naam geeft een nette fout", False)
except ValueError:
    check("engine: onbekende naam geeft een nette fout", True)
check("engine: alleen whisper_mlx kent MLX", not any("mlx" in Path(f"app/{n}.py").read_text()
      for n in PART_NAMES if n not in ("whisper_mlx", "engine", "base")))
# --- platformlaag: macOS alleen in platform_mac.py (en in de Mac-shell zelf) ---
import system  # noqa: E402
import platform_mac  # noqa: E402
missing = [n for n in system.NAMES if not hasattr(platform_mac, n) or not hasattr(system, n)]
check("platform: platform_mac heeft alle namen van system", not missing, missing)
check("platform: system geeft de Mac-versie door", system.mount_reachable is platform_mac.mount_reachable
      and system.computer_name is platform_mac.computer_name)
MAC_ONLY = ["osascript", "caffeinate", "pmset", "vm_stat", "ioreg", "scutil", "LaunchAgents", "libSystem",
            '["open"', '"Library"', "/Volumes\")"]   # ["open": het macOS-commando, niet een functie die open heet
SHELL = {"platform_mac.py", "app_main.py", "main.py", "app_shell.py"}
found = {f.name: [w for w in MAC_ONLY if w in f.read_text()] for f in Path("app").glob("*.py") if f.name not in SHELL}
found = {k: v for k, v in found.items() if v}
check("platform: geen macOS-aanroepen buiten platform_mac.py en de Mac-shell", not found, found)
check("platform: support-map via de platformlaag", base.SUPPORT_DIR == platform_mac.support_dir(base.INTERNAL_NAME))
check("code-update: platformlaag in CODE_FILES", "system.py" in CODE_FILES and "platform_mac.py" in CODE_FILES)
check("sbom: modelrevisie staat in whisper_mlx.py",
      re.search(r'"[\w.-]+/[\w.-]+":\s*"[0-9a-f]{40}"', Path("app/whisper_mlx.py").read_text()) is not None)

# --- laden zoals bij een code-update: uit het geheugen, via app_shell ---
probe = """
import sys
from pathlib import Path
sys.path.insert(0, "app")
import app_shell
names = %r
app_shell.use_code({n: Path("app", n).read_bytes() for n in names}, "9.9.9")
import transcribe, base, pipeline
assert base.__spec__.origin == "code-9.9.9/base.py", base.__spec__.origin
assert pipeline.__spec__.origin == "code-9.9.9/pipeline.py"
transcribe.SAMPLE_RATE = 8000
assert base.SAMPLE_RATE == pipeline.SAMPLE_RATE == 8000
print("geladen")
""" % ([n for n in CODE_FILES if n.endswith(".py")],)
result = subprocess.run([sys.executable, "-B", "-c", probe], capture_output=True, text=True)
check("code-update: transcribe en de delen laden uit het geheugen en sturen door",
      result.returncode == 0 and "geladen" in result.stdout, (result.stdout + result.stderr)[-300:])

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
