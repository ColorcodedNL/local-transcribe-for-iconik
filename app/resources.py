"""Is er op deze Mac ruimte voor een automatische opdracht (auto transcribe)?

Een automatische opdracht mag starten terwijl iemand op de Mac werkt, zolang er genoeg vrij is:
netstroom, geheugen, en weinig CPU en GPU in gebruik. CPU en GPU worden elke 10 seconden gemeten,
en alleen metingen waarin deze app zelf niets deed tellen: direct na een eigen opdracht staan die
hoog door de app zelf. Voor een start is minstens een minuut aan zulke metingen nodig, en het
gemiddelde gaat over hooguit de laatste drie minuten, zodat één piek niet telt.

Alleen macOS-aanroepen die geen beheerdersrechten vragen. Deze module praat niet met de rest
van de app: wat ze nodig heeft (`own_busy`) krijgt ze mee.
"""
import collections
import ctypes
import re
import subprocess
import threading
import time

SAMPLE_SECONDS = 10
WINDOW_SECONDS = 180      # het gemiddelde gaat over hooguit zoveel seconden
MIN_SECONDS = 60          # en vraagt minstens zoveel seconden aan metingen zonder eigen werk
DEFAULTS = {"auto_min_free_gb": 12.0, "auto_max_cpu": 40.0, "auto_max_gpu": 20.0}
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


class _CpuTicks:
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


class Meter:
    """Meet op de achtergrond. `own_busy()` zegt of deze app zelf bezig is; die metingen tellen niet."""

    def __init__(self, own_busy, cpu=None, gpu=gpu_percent, power=on_mains_power, mem=memory, clock=time.time):
        self.own_busy = own_busy
        self.cpu = cpu
        self.gpu = gpu
        self.power = power
        self.mem = mem
        self.clock = clock
        self.samples = collections.deque(maxlen=WINDOW_SECONDS // SAMPLE_SECONDS + 2)
        self.lock = threading.Lock()
        self.thread = None

    def start(self):
        """Begint met meten, als dat nog niet gebeurt. Het eerste minuut is er dus nog geen oordeel."""
        if self.thread is None:
            if self.cpu is None:
                try:
                    self.cpu = _CpuTicks().busy_percent
                except OSError:
                    self.cpu = lambda: None
            self.thread = threading.Thread(target=self._loop, daemon=True, name="meter")
            self.thread.start()

    def _loop(self):
        while True:
            self.sample()
            time.sleep(SAMPLE_SECONDS)

    def sample(self):
        """Eén meting. Ook de CPU-tellers worden altijd bijgewerkt, zodat de volgende meting klopt."""
        cpu, gpu = self.cpu(), self.gpu()
        with self.lock:
            self.samples.append((self.clock(), cpu, gpu, bool(self.own_busy())))

    def check(self, limits=None):
        """(klaar, reden, waarden). `reden` is None als er ruimte is, anders een van "power", "memory",
        "pressure", "measuring", "cpu", "gpu". `waarden` zijn de cijfers, voor de weergave."""
        limits = {**DEFAULTS, **(limits or {})}
        now = self.clock()
        with self.lock:
            usable = [s for s in self.samples if now - s[0] <= WINDOW_SECONDS and not s[3]]
        cpu = [s[1] for s in usable if s[1] is not None]
        gpu = [s[2] for s in usable if s[2] is not None]
        available, pressure = self.mem()
        values = {"cpu": sum(cpu) / len(cpu) if cpu else None, "gpu": sum(gpu) / len(gpu) if gpu else None,
                  "free_gb": available / 1e9 if available is not None else None, "pressure": pressure}
        if self.power() is not True:
            return False, "power", values
        if available is None or available / 1e9 < limits["auto_min_free_gb"]:
            return False, "memory", values
        if pressure is not None and pressure > PRESSURE_NORMAL:
            return False, "pressure", values
        span = (usable[-1][0] - usable[0][0]) if len(usable) > 1 else 0
        if span < MIN_SECONDS - SAMPLE_SECONDS / 2 or not cpu:
            return False, "measuring", values
        if values["cpu"] > limits["auto_max_cpu"]:
            return False, "cpu", values
        if values["gpu"] is not None and values["gpu"] > limits["auto_max_gpu"]:
            return False, "gpu", values
        return True, None, values
