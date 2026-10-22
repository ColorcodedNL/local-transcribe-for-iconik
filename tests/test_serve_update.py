"""Toestandsmachine van de update in serve.py. Draaien vanuit de projectmap:

    uv run --with pynacl --with requests python tests/test_serve_update.py
"""
import sys, types, threading, time, json, http.client
from http.server import ThreadingHTTPServer
sys.path.insert(0, "app")
for name in ("keyring", "numpy", "mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try: __import__(name)
        except Exception: sys.modules[name] = types.ModuleType(name)
import transcribe, serve, updater
transcribe.L = lambda nl, en: nl   # test in het Nederlands, los van de taal van de machine
fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))

# --- 1. check_update: GitHub-antwoord -> melding
R = transcribe.RELEASES_REPO; P = f"https://github.com/{R}/releases/download/v9.9.9/"
release = {"tag_name": "v9.9.9", "html_url": f"https://github.com/{R}/releases/tag/v9.9.9",
           "assets": [{"name": "code-9.9.9-shell1.zip", "browser_download_url": P + "a"}, {"name": "code-9.9.9-shell1.zip.sig", "browser_download_url": P + "b"}]}
class Resp:
    status_code = 200
    def json(self): return release
serve.requests.get = lambda *a, **k: Resp()
class Stop(Exception): pass
def once(): 
    real = serve.time.sleep
    serve.time.sleep = lambda s: (_ for _ in ()).throw(Stop())
    try: serve.check_update()
    except Stop: pass
    finally: serve.time.sleep = real
once()
check("melding: code-update beschikbaar", serve.update.get("mode") == "code" and serve.update.get("state") == "available", serve.update)
v = serve.update_view()
check("update_view geeft geen urls van de zip prijs", set(v) == {"version", "mode", "state", "waiting"} and v["version"] == "9.9.9", v)

# --- 2. installeren, wachten tot de rij leeg is, herstarten
installed, restarted = [], threading.Event()
updater.install = lambda info, running: (installed.append((info["version"], running)) or info["version"])
serve.restart_hook = restarted.set
serve.current[0] = "job"                      # er loopt een opdracht
serve.install_update()
time.sleep(0.5)
check("na klik: staat klaar, nog niet herstart zolang er iets loopt", serve.update["state"] == "ready" and not restarted.is_set() and installed == [("9.9.9", transcribe.APP_VERSION)], (serve.update, installed))
check("  het venster ziet 'wacht tot klaar'", serve.update_view()["waiting"] is True)
once()
check("  een nieuwe GitHub-controle verstoort de staat niet", serve.update["state"] == "ready")
serve.current[0] = None                       # opdracht klaar
check("  herstart zodra de rij leeg is", restarted.wait(6), "geen herstart binnen 6 s")

# --- 3. dubbel klikken installeert niet twee keer
serve.update.update(state="available"); installed.clear(); serve.restart_hook = None
gate = threading.Event()
updater.install = lambda info, running: (installed.append(1), gate.wait(3), info["version"])[-1]
serve.install_update(); serve.install_update(); serve.install_update()
time.sleep(0.3); gate.set(); time.sleep(0.3)
check("drie klikken geven één installatie", len(installed) == 1, installed)

# --- 4. fouten komen als tekst in het venster, en opnieuw proberen kan
for exc, word in ((updater.UpdateError("De handtekening klopt niet.", "Bad signature."), "handtekening"),
                  (serve.requests.ConnectionError("weg"), "verbinding")):
    serve.update.clear(); serve.update.update(version="9.9.9", mode="code", state="available", zip_url="x", sig_url="y")
    def boom(info, running, exc=exc): raise exc
    updater.install = boom
    serve.install_update(); time.sleep(0.3)
    check(f"fout wordt melding: {type(exc).__name__}", serve.update["state"] == "error" and word in serve.update["error"].lower(), serve.update)
updater.install = lambda info, running: info["version"]
serve.install_update(); time.sleep(0.3)
check("opnieuw proberen na een fout werkt", serve.update["state"] == "ready", serve.update)

# --- 5. dmg-update biedt geen installatie aan
serve.update.clear(); serve.update.update(version="9.9.9", mode="dmg", state="available", url="https://github.com/x")
updater.install = lambda info, running: (_ for _ in ()).throw(AssertionError("mag niet"))
serve.install_update(); time.sleep(0.2)
check("dmg-update: knop Installeren doet niets", serve.update["state"] == "available")

# --- 6. de route eist de header
srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
def get(path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5); c.request("GET", path, headers=headers or {})
    r = c.getresponse(); r.read(); return r.status
serve.update.clear(); serve.update.update(version="9.9.9", mode="code", state="available", zip_url="x", sig_url="y")
n = len(installed)
check("route zonder header: 403 en niets gestart", get("/update/install") == 403 and serve.update["state"] == "available")
check("route met header: 200", get("/update/install", {"X-Local-Transcribe": "1"}) == 200)
print("ALLES OK" if not fails else f"{fails} FOUT")
raise SystemExit(1 if fails else 0)
