"""Wat macOS-specifiek is, achter de vaste namen van system.py.

Paden, de metingen voor auto transcribe, gekoppelde volumes, een map kiezen, wakker blijven, een link
openen, starten bij inloggen en de computernaam. Alleen aanroepen die geen beheerdersrechten vragen.
Ontbreekt een commando (bijvoorbeeld in de tests op Linux), dan geeft een functie None, "" of False en
loopt de app door.
"""
import ctypes
import functools
import os
import plistlib
import re
import socket
import subprocess
from pathlib import Path

NAME = "macOS"


# --- paden ---

def support_dir(app_name):
    return Path.home() / "Library" / "Application Support" / app_name


def logs_dir(app_name):
    return Path.home() / "Library" / "Logs" / app_name


# --- geheugen van dit proces (base.memory_report) ---

def process_footprint():
    """Geheugen van dit proces zoals Activiteitenweergave het toont (phys_footprint), in bytes."""
    try:
        import ctypes
        lib = getattr(process_footprint, "lib", None)
        if lib is None:
            lib = process_footprint.lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        info = (ctypes.c_uint64 * 48)()
        if lib.proc_pid_rusage(os.getpid(), 2, ctypes.byref(info)) != 0:  # RUSAGE_INFO_V2
            return 0
        return int(info[2 + 7])  # na de uuid (16 bytes): ri_phys_footprint
    except Exception:
        return 0


# --- metingen voor auto transcribe (resources.py) ---

PRESSURE_NORMAL = 1       # kern.memorystatus_vm_pressure_level: 1 normaal, 2 waarschuwing, 4 kritiek


def _run(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def on_mains_power():
    """True op netstroom, False op batterij, None als het niet te zien is."""
    out = _run(["pmset", "-g", "batt"])
    if "AC Power" in out:
        return True
    if "Battery Power" in out:
        return False
    return None


def memory():
    """(beschikbaar in bytes, drukniveau). Beschikbaar is vrij plus wat macOS zonder moeite vrijmaakt:
    inactief, speculatief en opschoonbaar. None waar het niet te lezen is."""
    out = _run(["vm_stat"])
    size = re.search(r"page size of (\d+) bytes", out)
    pages = {m.group(1): int(m.group(2)) for m in re.finditer(r"Pages (free|inactive|speculative|purgeable):\s+(\d+)", out)}
    available = sum(pages.values()) * int(size.group(1)) if size and pages else None
    level = _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    return available, int(level) if level.isdigit() else None


class CpuTicks:
    """Tellers van de kernel (user, system, idle, nice) via host_statistics: het verschil tussen twee
    metingen geeft het gebruik in die periode, over alle kernen samen."""

    def __init__(self):
        self.lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        self.lib.mach_host_self.restype = ctypes.c_uint
        self.host = self.lib.mach_host_self()
        self.previous = None

    def busy_percent(self):
        info = (ctypes.c_uint * 4)()
        count = ctypes.c_uint(4)
        if self.lib.host_statistics(self.host, 3, info, ctypes.byref(count)) != 0:   # 3 = HOST_CPU_LOAD_INFO
            return None
        now = list(info)
        before, self.previous = self.previous, now
        if before is None:
            return None
        delta = [(a - b) % 2 ** 32 for a, b in zip(now, before)]
        total = sum(delta)
        return 100.0 * (total - delta[2]) / total if total else None


def gpu_percent():
    """Bezetting van de GPU volgens IOAccelerator ("Device Utilization %"), of None."""
    values = [int(v) for v in re.findall(r'"Device Utilization %"\s*=\s*(\d+)', _run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"]))]
    return max(values) if values else None


# --- opslag (ISG) en mappen ---

def ask_folder(prompt, start=None):
    """Laat de gebruiker een map kiezen in een venster van macOS. None bij annuleren.
    `start` is de map waarin het venster opent, als die bestaat."""
    if start and Path(start).is_dir():
        choose = "choose folder with prompt (item 1 of argv) default location (POSIX file (item 2 of argv))"
    else:
        choose, start = "choose folder with prompt (item 1 of argv)", ""
    script = f"on run argv\ntell me to activate\nreturn POSIX path of ({choose})\nend run"
    try:
        result = subprocess.run(["osascript", "-e", script, prompt, str(start)],
                                capture_output=True, text=True)
    except OSError:
        return None   # geen osascript (de tests op Linux)
    chosen = result.stdout.strip()
    return chosen.rstrip("/") if result.returncode == 0 and chosen else None


def find_moved_mount(known, relative, volumes=Path("/Volumes")):
    """Zoekt dezelfde map op een opnieuw gekoppeld volume.

    Na een verbroken netwerkkoppeling blijft er soms een lege /Volumes/Opslag
    achter en koppelt macOS de opslag opnieuw als /Volumes/Opslag-1. Hier
    zoeken we in beide richtingen, met of zonder zo'n achtervoegsel.
    """
    try:
        inner = Path(known).relative_to(volumes)
    except ValueError:
        return None  # geen netwerkvolume onder /Volumes
    if not inner.parts:
        return None
    volume, rest = inner.parts[0], Path(*inner.parts[1:])
    base = re.sub(r"-\d+$", "", volume)
    try:
        entries = sorted(volumes.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.name != volume and re.sub(r"-\d+$", "", entry.name) == base:
            candidate = entry / rest
            try:
                if (candidate / relative).is_file():
                    return candidate
            except OSError:
                continue
    return None


def mount_reachable(path, volumes=Path("/Volumes")):
    """Is het mount point van een ISG nu bereikbaar? Alleen kijken, nooit iets onthouden of wijzigen.

    Een netwerkvolume onder /Volumes telt alleen als het echt gekoppeld is: na een verbroken koppeling
    blijft er soms een lege map /Volumes/Opslag achter, en die bestaat wel maar is niet de opslag.
    Koppelde macOS de opslag opnieuw als /Volumes/Opslag-1 (zie find_moved_mount), dan is hij ook
    bereikbaar: de opdracht zelf vindt hem daar en onthoudt dat.
    Kan blijven hangen op een netwerkkoppeling die niet meer reageert: roep dit aan met een tijdslimiet.
    """
    path = Path(path)
    try:
        inner = path.relative_to(volumes)
    except ValueError:
        return path.is_dir()          # een map op een eigen schijf: bestaat hij, dan is hij er
    if not inner.parts:
        return False
    volume, rest = inner.parts[0], Path(*inner.parts[1:])
    if os.path.ismount(volumes / volume) and (volumes / volume / rest).is_dir():
        return True
    base = re.sub(r"-\d+$", "", volume)
    try:
        entries = sorted(volumes.iterdir())
    except OSError:
        return False
    for entry in entries:
        if entry.name != volume and re.sub(r"-\d+$", "", entry.name) == base:
            try:
                if os.path.ismount(entry) and (entry / rest).is_dir():
                    return True
            except OSError:
                continue
    return False


@functools.lru_cache(maxsize=1)
def _boot_volume_name():
    """De naam van de opstartschijf zoals de Finder hem toont ("Macintosh HD")."""
    try:
        raw = subprocess.run(["diskutil", "info", "-plist", "/"], capture_output=True, timeout=10).stdout
        name = plistlib.loads(raw).get("VolumeName") if raw else ""
    except Exception:
        name = ""
    return name or "Mac"


def volume_place(path, volumes=Path("/Volumes")):
    """(naam van het volume, pad binnen het volume) van een bestand. Een volume dat macOS na een verbroken
    koppeling opnieuw koppelde als Opslag-1 heet hier gewoon Opslag (zoals find_moved_mount het ook ziet), zodat
    dezelfde clip op dezelfde plek blijft horen. Alles buiten /Volumes ligt op de opstartschijf."""
    path = Path(path)
    try:
        inner = path.relative_to(volumes)
    except ValueError:
        return _boot_volume_name(), Path(*path.parts[1:]) if path.is_absolute() else path
    if not inner.parts:
        return _boot_volume_name(), Path()
    return re.sub(r"-\d+$", "", inner.parts[0]), Path(*inner.parts[1:])


# --- wakker blijven, links, starten bij inloggen ---

def keep_awake_command(pid):
    """Het commando dat de computer wakker houdt zolang proces `pid` leeft (caffeinate -w stopt vanzelf)."""
    return ["caffeinate", "-i", "-w", str(pid)]


def reveal(path):
    """Toont een bestand in de Finder, geselecteerd."""
    subprocess.Popen(["open", "-R", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_url(url):
    """Opent een link in de gewone browser, niet in het venster van de app."""
    subprocess.Popen(["open", url])


LOGIN_PLIST = Path.home() / "Library" / "LaunchAgents" / "nl.colorcoded.local-transcribe.plist"


def login_item_on():
    return LOGIN_PLIST.exists()


def set_login_item(on, bundle, marker):
    """Opstarten bij inloggen: een LaunchAgent die de app opent. Gaat in bij de volgende login.
    `bundle`: de app (None: niets aanzetten); `marker`: dit bestand legt de LaunchAgent neer bij het starten."""
    import plistlib
    import shlex
    if not on:
        LOGIN_PLIST.unlink(missing_ok=True)
        return
    if bundle is None:
        return
    command = f"touch {shlex.quote(str(marker))}; exec /usr/bin/open -a {shlex.quote(str(bundle))}"
    LOGIN_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOGIN_PLIST.write_bytes(plistlib.dumps({
        "Label": LOGIN_PLIST.stem,
        "ProgramArguments": ["/bin/sh", "-c", command],
        "RunAtLoad": True,
    }))


# --- deze computer ---

@functools.lru_cache(maxsize=1)
def computer_name():
    """De computernaam uit Systeeminstellingen. De hostnaam van het netwerk kan er vreemd uitzien."""
    for args in (["scutil", "--get", "ComputerName"], ["scutil", "--get", "LocalHostName"]):
        try:
            name = subprocess.run(args, capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if name:
            return name
    name = socket.gethostname().split(".")[0]
    return "".join(c for c in name if c.isprintable() and c.isascii()) or "Mac"
