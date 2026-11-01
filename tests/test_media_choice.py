"""Welk bestand van een asset gaat naar ffmpeg: alleen audio en video, en van identieke kopieën de eerste
die op deze Mac echt te vinden is (archiefproef van 26 oktober: kopieën in twee mappen, plaatjes, fonts,
.lvix, een Premiere-project).

Draaien: uv run --no-project --with pynacl --with requests python -B tests/test_media_choice.py
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
import transcribe  # noqa: E402

transcribe.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"   # nooit de echte voorkeuren aanraken
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


class FakeIconik:
    def __init__(self, files, storages=None):
        self.files, self.storages = files, storages or {}

    def get(self, path):
        if path.startswith("files/v1/assets/"):
            return {"objects": self.files}
        storage_id = path.split("/")[3]
        if storage_id in self.storages:
            return self.storages[storage_id]
        raise transcribe.Problem("geen recht")


def file(name, folder, storage="opslag-a", kind="FILE"):
    return {"type": kind, "name": name, "directory_path": folder, "storage_id": storage}


def problem_of(call):
    try:
        return call(), None
    except transcribe.Problem as problem:
        return None, problem
    except transcribe.NeedsFolder as folder:
        return None, folder


root = Path(tempfile.mkdtemp())
mount_a, mount_b = root / "A", root / "B"
(mount_a / "Footage").mkdir(parents=True)
(mount_a / "Projects").mkdir(parents=True)
(mount_b / "Footage").mkdir(parents=True)
transcribe.set_mount("opslag-a", str(mount_a))

# Geen audio of video: overgeslagen, met wat er wel staat.
iconik = FakeIconik([file("logo.png", "x"), file("logo2.png", "x"), file("font.otf", "x")])
_, problem = problem_of(lambda: transcribe.find_media_file(iconik, "a1", interactive=False))
check("alleen plaatjes en fonts: overgeslagen, geen fout",
      problem is not None and getattr(problem, "skip", False) and ".png" in str(problem) and ".otf" in str(problem), problem)

iconik = FakeIconik([file("project.prproj", "Premiere Pro Projects"), file("PLAY_BUG", "x/y"), file("clip.MP4.lvix", "z")])
_, problem = problem_of(lambda: transcribe.find_media_file(iconik, "a2", interactive=False))
check("Premiere-project, bestand zonder extensie, .lvix: overgeslagen",
      problem is not None and getattr(problem, "skip", False), problem)

iconik = FakeIconik([file("clip.srt", "x"), file("map", "x", kind="DIRECTORY")])
_, problem = problem_of(lambda: transcribe.find_media_file(iconik, "a3", interactive=False))
check("alleen een sidecar: overgeslagen, geen mediabestand",
      problem is not None and getattr(problem, "skip", False), problem)

# Hoofdletters in de extensie tellen mee.
(mount_a / "Footage" / "clip1.MXF").write_bytes(b"x")
iconik = FakeIconik([file("logo.png", "Footage"), file("clip1.MXF", "Footage")])
path, problem = problem_of(lambda: transcribe.find_media_file(iconik, "a4", interactive=False))
check("MXF in hoofdletters naast een plaatje: de MXF", path == mount_a / "Footage" / "clip1.MXF", problem or path)

# Twee kopieën, de eerste bestaat niet (meer) op deze plek: de tweede.
(mount_a / "Projects" / "clip2.mxf").write_bytes(b"x")
iconik = FakeIconik([file("clip2.mxf", "Footage_OLD"), file("clip2.mxf", "Projects")])
path, problem = problem_of(lambda: transcribe.find_media_file(iconik, "a5", interactive=False))
check("eerste kopie niet te vinden: de tweede", path == mount_a / "Projects" / "clip2.mxf", problem or path)

# Twee kopieën op verschillende opslag: de eerste staat op opslag die deze Mac niet kent, de tweede wel.
(mount_a / "Footage" / "clip3.mov").write_bytes(b"x")
iconik = FakeIconik([file("clip3.mov", "Footage", storage="opslag-b"), file("clip3.mov", "Footage")])
path, problem = problem_of(lambda: transcribe.find_media_file(iconik, "a6", interactive=False))
check("kopie op onbekende opslag en een op bekende: de bekende, zonder iets te vragen",
      path == mount_a / "Footage" / "clip3.mov", problem or path)

# Geen enkele kopie te vinden op bekende opslag, wel een op onbekende opslag: die opslag vragen.
iconik = FakeIconik([file("clip4.mov", "Footage"), file("clip4.mov", "Footage", storage="opslag-b")],
                    storages={"opslag-b": {"name": "Opslag B", "settings": {}}})
_, problem = problem_of(lambda: transcribe.find_media_file(iconik, "a7", interactive=False))
check("bekende kopie weg, onbekende opslag over: om die map vragen",
      isinstance(problem, transcribe.NeedsFolder) and "Opslag B" in str(problem), repr(problem))

# Alleen kopieën op bekende opslag, geen enkele te vinden: bestand niet gevonden (geen lokale fout).
iconik = FakeIconik([file("weg.mov", "Footage"), file("weg.mov", "Projects")])
_, problem = problem_of(lambda: transcribe.find_media_file(iconik, "a8", interactive=False))
check("geen kopie te vinden: bestand niet gevonden, niet overgeslagen",
      isinstance(problem, transcribe.Problem) and not getattr(problem, "skip", False)
      and not getattr(problem, "local", False) and "weg.mov" in str(problem), repr(problem))

# Mount point van de bekende opslag is weg: een lokale fout (de werker geeft hem door aan een andere Mac).
transcribe.set_mount("opslag-c", str(root / "bestaat-niet"))
iconik = FakeIconik([file("clip5.mov", "Footage", storage="opslag-c")])
_, problem = problem_of(lambda: transcribe.find_media_file(iconik, "a9", interactive=False))
check("opslag niet gemount: lokale fout", isinstance(problem, transcribe.Problem)
      and getattr(problem, "local", False), repr(problem))

# Een opgegeven mount point (--mount): ook daar elke kopie proberen.
(mount_b / "Footage" / "clip6.wav").write_bytes(b"x")
iconik = FakeIconik([file("clip6.wav", "Projects"), file("clip6.wav", "Footage")])
path, problem = problem_of(lambda: transcribe.find_media_file(iconik, "a10", mount_override=str(mount_b), interactive=False))
check("--mount: de kopie die daar staat", path == mount_b / "Footage" / "clip6.wav", problem or path)

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
