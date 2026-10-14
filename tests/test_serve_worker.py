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

sys.path.insert(0, ".")
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
check("de status bevat geen geheimen", set(state["worker"]) <= {"enabled", "last_poll", "last_asset", "error", "machine", "paused_until", "login", "login_available"}, state["worker"])
get("/worker?value=off", header)
check("uit staat ook zo in de status", serve.app_state()["worker"]["enabled"] is False)

print("\nALLES OK" if not fails else f"\n{fails} FOUT")
sys.exit(1 if fails else 0)
