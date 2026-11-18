"""Is er op deze Mac ruimte voor een automatische opdracht (auto transcribe)?

Een automatische opdracht mag starten terwijl iemand op de Mac werkt, zolang er genoeg vrij is:
netstroom, geheugen, en weinig CPU en GPU in gebruik. CPU en GPU worden elke 10 seconden gemeten,
en alleen metingen waarin deze app zelf niets deed tellen: direct na een eigen opdracht staan die
hoog door de app zelf. Voor een start is minstens een minuut aan zulke metingen nodig, en het
gemiddelde gaat over hooguit de laatste drie minuten, zodat één piek niet telt.

De metingen zelf verschillen per systeem en staan in de platformlaag (system.py, platform_mac.py). Deze
module praat niet met de rest van de app: wat ze nodig heeft (`own_busy`) krijgt ze mee.
"""
import collections
import threading
import time

from system import PRESSURE_NORMAL, CpuTicks, gpu_percent, memory, on_mains_power  # noqa: F401

SAMPLE_SECONDS = 10
WINDOW_SECONDS = 180      # het gemiddelde gaat over hooguit zoveel seconden
MIN_SECONDS = 60          # en vraagt minstens zoveel seconden aan metingen zonder eigen werk
DEFAULTS = {"auto_min_free_gb": 12.0, "auto_max_cpu": 40.0, "auto_max_gpu": 40.0}   # GPU: een browser en het eigen venster zitten al op 20 tot 25%



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
                    self.cpu = CpuTicks().busy_percent
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
