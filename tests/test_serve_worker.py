"""De schakelaar voor de werker in serve.py: route, beveiliging, status voor het venster.

Draaien: uv run --no-project --with pynacl --with requests python -B tests/test_serve_worker.py
"""
import http.client
import sys
import tempfile
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, "app")
for name in ("keyring", "numpy", "mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
import transcribe, serve, worker  # noqa: E402,E401

transcribe.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"   # nooit de echte voorkeuren aanraken
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def get(path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
    conn.request("GET", path, headers=headers or {})
    return conn.getresponse().status


header = {"X-Local-Transcribe": "1"}
check("standaard staat de werker uit", serve.worker_enabled() is False)
check("zonder header: 403 en niets veranderd", get("/worker?value=on") == 403 and serve.worker_enabled() is False)

worker.pause()
worker.wake.clear()
check("met header aan: 200", get("/worker?value=on", header) == 200)
check("  staat nu in de voorkeuren", serve.worker_enabled() is True and transcribe.load_config()["worker_enabled"] is True)
check("  en de werker wordt wakker gemaakt", worker.wake.is_set())
check("  en een pauze na een fout vervalt", worker.paused() is False and worker.state["paused_until"] is None)
check("onbekende waarde verandert niets", get("/worker?value=misschien", header) == 200 and serve.worker_enabled() is True)
worker.pause(reason="Geen internet")
worker.wake.clear()
check("hervatten zonder header: 403 en de pauze blijft", get("/worker?value=resume") == 403 and worker.paused() is True)
check("hervatten met header: pauze weg en de werker wordt wakker", get("/worker?value=resume", header) == 200
      and worker.paused() is False and worker.wake.is_set() and serve.worker_enabled() is True)
check("met header uit: 200 en uit", get("/worker?value=off", header) == 200 and serve.worker_enabled() is False)
check("andere voorkeuren blijven staan", (transcribe.save_config({**transcribe.load_config(), "language": "nl"}) or True)
      and get("/worker?value=on", header) == 200 and transcribe.load_config().get("language") == "nl")

try:
    transcribe.fail("x", local=True)
except transcribe.Problem as problem:
    check("fail(local=True) markeert de fout als een fout van deze Mac", problem.local is True and problem.skip is False)
try:
    transcribe.fail("x")
except transcribe.Problem as problem:
    check("fail zonder local is een fout van het asset", problem.local is False)

state = serve.app_state()
check("het venster krijgt de status mee", state["worker"]["enabled"] is True and state["worker"]["machine"], state.get("worker"))
check("de status bevat geen geheimen", set(state["worker"]) <= {"enabled", "last_poll", "last_asset", "error", "machine", "paused_until", "pause_reason", "login", "login_available", "iconik_language", "last_auto", "auto", "auto_on", "auto_audio", "awake"}, state["worker"])
get("/worker?value=off", header)
check("uit staat ook zo in de status", serve.app_state()["worker"]["enabled"] is False)

# taal van de velden in Iconik
calls = []


class StubQueue:
    def __init__(self, connection):
        pass

    def setup(self, create=False, language="nl"):
        calls.append(("setup", create, language))

    def apply_language(self, language):
        calls.append(("apply", language))


real_queue, real_client = serve.remote.Queue, serve.client
serve.remote.Queue, serve.client = StubQueue, lambda: object()
check("taal van de velden: zonder header 403", get("/iconik-language?value=en") == 403 and not calls)
check("taal van de velden: onbekende taal doet niets", get("/iconik-language?value=fr", header) == 200 and not calls
      and "iconik_language" not in transcribe.load_config())
check("taal van de velden: Engels kiezen maakt eerst de velden af en zet dan de labels",
      get("/iconik-language?value=en", header) == 200 and calls == [("setup", True, "en"), ("apply", "en")])
check("  en onthoudt de keuze", transcribe.load_config().get("iconik_language") == "en"
      and serve.worker_languages()[1] == "en")
serve.client = lambda: None
calls.clear()
before = transcribe.load_config().get("iconik_language")
get("/iconik-language?value=nl", header)
check("  niet ingelogd: niets gewijzigd, keuze blijft", not calls and transcribe.load_config().get("iconik_language") == before)
serve.remote.Queue, serve.client = real_queue, real_client
check("  en het venster krijgt de gekozen taal mee", serve.app_state()["worker"]["iconik_language"] == "en")

# --- auto transcribe: schakelaars, instellingen, wakker houden
check("auto: zonder header 403 en niets veranderd", get("/auto?value=on") == 403
      and not transcribe.load_config().get("auto_transcribe"))
worker.wake.clear()
check("auto aan met header", get("/auto?value=on", header) == 200 and transcribe.load_config().get("auto_transcribe") is True
      and worker.wake.is_set())
check("audio apart aan te zetten", get("/auto?audio=on", header) == 200 and transcribe.load_config().get("auto_audio") is True
      and transcribe.load_config().get("auto_transcribe") is True)
state = serve.app_state()["worker"]
check("het venster krijgt auto en audio mee", state["auto_on"] is True and state["auto_audio"] is True and "awake" in state)
get("/auto?audio=off", header)
check("audio uit laat auto aan", transcribe.load_config().get("auto_audio") is False and transcribe.load_config().get("auto_transcribe") is True)


class FakeMeter:
    def __init__(self):
        self.started, self.verdict, self.limits = False, (True, None, {"cpu": 9.6, "gpu": 8.0}), None

    def start(self):
        self.started = True

    def check(self, limits=None):
        self.limits = limits
        return self.verdict


real_meter = serve.meter
serve.meter = FakeMeter()
config = transcribe.load_config()
transcribe.save_config({**config, "mounts": {}})
check("auto zonder gekoppelde opslag: niets, met reden", serve.auto_settings() is None
      and worker.state["auto"]["reason"] == "storage", worker.state["auto"])
transcribe.save_config({**config, "mounts": {"opslag-1": "/Volumes/ISG"}, "auto_max_gpu": 50, "auto_max_minutes": 90,
                        "auto_max_cpu": "veel"})
got = serve.auto_settings()
check("auto met ruimte: instellingen voor de werker", got == {"storages": ["opslag-1"], "audio": False, "max_minutes": 90.0}, got)
check("de meter is gestart en krijgt de grenzen uit de voorkeuren, rare waarden worden de standaard",
      serve.meter.started and serve.meter.limits == {"auto_min_free_gb": 12.0, "auto_max_cpu": 40.0, "auto_max_gpu": 50.0},
      serve.meter.limits)
check("de status voor het venster", worker.state["auto"] == {"ready": True, "reason": None, "values": {"cpu": 9.6, "gpu": 8.0}},
      worker.state["auto"])
check("menubalk: klaar", "automatisch" in (serve.auto_status_text() or "").lower()
      or "automatically" in (serve.auto_status_text() or "").lower(), serve.auto_status_text())
serve.meter.verdict = (False, "gpu", {"gpu": 63.0})
check("geen ruimte: niets, met reden", serve.auto_settings() is None and worker.state["auto"]["reason"] == "gpu")
check("menubalk: waarop hij wacht", "GPU 63%" in (serve.auto_status_text() or ""), serve.auto_status_text())
get("/auto?value=off", header)
check("auto uit: niets en geen status", serve.auto_settings() is None and worker.state["auto"] is None
      and serve.auto_status_text() is None)
serve.meter = real_meter


class FakeProcess:
    def __init__(self, args, **kw):
        self.args, self.done = args, False
        started.append(self)

    def poll(self):
        return 0 if self.done else None

    def terminate(self):
        self.done = True

    def wait(self, timeout=None):
        return 0


started = []
real_popen = serve.subprocess.Popen
serve.subprocess.Popen = FakeProcess
want = [True]
serve.keep_awake(lambda: want[0], once=True)
check("wakker houden: caffeinate -i -w met het eigen proces", len(started) == 1 and started[0].args[:3] == ["caffeinate", "-i", "-w"]
      and started[0].args[3] == str(serve.os.getpid()) and serve.awake["on"], started and started[0].args)
serve.keep_awake(lambda: want[0], once=True)
check("blijft het er één", len(started) == 1)
started[0].done = True
serve.keep_awake(lambda: want[0], once=True)
check("weggevallen caffeinate wordt opnieuw gestart", len(started) == 2)
want[0] = False
serve.keep_awake(lambda: want[0], once=True)
check("niet meer nodig: gestopt", started[1].done and serve.awake["process"] is None and not serve.awake["on"])
serve.subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("caffeinate"))
want[0] = True
serve.keep_awake(lambda: want[0], once=True)
check("zonder caffeinate: geen crash, niet wakker", not serve.awake["on"])
serve.subprocess.Popen = real_popen

real_busy, real_power = serve.busy, serve.resources.on_mains_power
serve.busy = lambda: True
check("tijdens een opdracht: wakker, ook op batterij", (setattr(serve.resources, "on_mains_power", lambda: False) or True)
      and serve.want_awake())
serve.busy = lambda: False
transcribe.save_config({**transcribe.load_config(), "worker_enabled": True, "auto_transcribe": True})
check("tussen opdrachten op batterij: niet", not serve.want_awake())
serve.resources.on_mains_power = lambda: True
check("tussen opdrachten met auto aan op netstroom: wakker", serve.want_awake())
transcribe.save_config({**transcribe.load_config(), "worker_enabled": False})
check("opdrachten op afstand uit: niet", not serve.want_awake())
serve.busy, serve.resources.on_mains_power = real_busy, real_power

# --- modellen: met auto aan pas vrijgeven als er even niets meer komt
released = []
real_release, real_busy = serve.release_models_now, serve.busy
serve.release_models_now = lambda: released.append(1)
serve.busy = lambda: False
transcribe.save_config({**transcribe.load_config(), "worker_enabled": True, "auto_transcribe": True})
check("auto actief als opdrachten op afstand en auto aan staan", serve.auto_active())
serve.activity["done"] = 100.0
serve.release_later(100.0, sleep=lambda s: None)
check("niets meer gebeurd: modellen vrij", released == [1])
serve.release_later(100.0, sleep=lambda s: serve.activity.__setitem__("done", 200.0))
check("intussen een nieuwe opdracht klaar: deze draad doet niets", released == [1])
serve.busy = lambda: True
serve.release_later(200.0, sleep=lambda s: None)
check("er loopt een opdracht: niets vrijgeven", released == [1])
transcribe.save_config({**transcribe.load_config(), "auto_transcribe": False})
check("auto uit: niet actief", not serve.auto_active())
serve.release_models_now, serve.busy = real_release, real_busy

# --- Cmd+Q met "Na deze opdracht": niets nieuws, daarna afsluiten
transcribe.save_config({**transcribe.load_config(), "worker_enabled": True, "auto_transcribe": True})
check("normaal mag de werker oppakken", serve.worker_may_take())
quits = []
serve.quit_hook = lambda: quits.append(1)
serve.quit_asking.set()
check("zolang de vraag bij Cmd+Q openstaat: niets nieuws", not serve.worker_may_take())
serve.quit_asking.clear()
check("Annuleren: de werker mag weer", serve.worker_may_take())
serve.quit_after.set()
check("na deze opdracht: de werker pakt niets nieuws (ook geen auto)", not serve.worker_may_take())
real_busy, real_power = serve.busy, serve.resources.on_mains_power
serve.busy, serve.resources.on_mains_power = (lambda: False), (lambda: True)
check("na deze opdracht: tussen opdrachten niet meer wakker houden", not serve.want_awake())
serve.busy = lambda: True
check("maar de lopende opdracht houdt de Mac nog wel wakker", serve.want_awake())
serve.busy, serve.resources.on_mains_power = real_busy, real_power
serve.request_quit()
check("klaar: de app sluit af via de haak", quits == [1])
check("de voorkeur voor opdrachten op afstand blijft staan", serve.worker_enabled())
serve.quit_after.clear()
serve.quit_hook = None
transcribe.save_config({**transcribe.load_config(), "worker_enabled": False, "auto_transcribe": False})

print("\nALLES OK" if not fails else f"\n{fails} FOUT")
sys.exit(1 if fails else 0)
