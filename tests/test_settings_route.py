"""De ene ingang voor wijzigingen: POST /settings met JSON, gecontroleerd tegen het schema, alles of niets,
met wat er na het bewaren moet gebeuren (serve.AFTER_SAVE).

Draaien: uv run --no-project --with pynacl --with requests --with numpy python -B tests/test_settings_route.py
"""
import http.client
import json
import sys
import tempfile
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, "app")
# Nooit de echte sleutelhanger.
_kr = types.ModuleType("keyring")
_kr.get_password = lambda *a: None
_kr.set_password = lambda *a: None
_kr.delete_password = lambda *a: None
sys.modules["keyring"] = _kr
for name in ("mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
import transcribe, serve, worker  # noqa: E402,E401
import settings  # noqa: E402

transcribe.L = lambda nl, en: nl
tmp = Path(tempfile.mkdtemp())
transcribe.CONFIG_PATH = tmp / "config.json"   # nooit de echte voorkeuren aanraken
transcribe.TRANSCRIPTS_DIR = tmp / "transcripts"
serve.jobs_path = lambda: tmp / "jobs.json"
transcribe.save_config({"client_id": "12345678-1234-1234-1234-123456789abc", "app_language": "nl", "mounts": {"opslag-1": "/Volumes/ISG"}})
serve.settings.update(key="k", hf_token=None, model="m", language=None, mount=None, idle_exit=False)
serve.client = lambda: None   # niet ingelogd: niemand praat met Iconik
logouts = []
transcribe.clear_login = lambda: logouts.append(1)
is_busy = [False]
serve.busy = lambda: is_busy[0]
server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


def post(body, header=True, session=True, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    data = raw if raw is not None else json.dumps(body).encode()
    c.putrequest("POST", "/settings", skip_host=True)
    c.putheader("Host", f"localhost:{PORT}")
    if session:
        c.putheader("Cookie", f"{serve.SESSION_COOKIE}={serve.SESSION}")
    if header:
        c.putheader("X-Local-Transcribe", "1")
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(data)))
    c.endheaders()
    c.send(data)
    r = c.getresponse()
    out = r.read()
    try:
        return r.status, json.loads(out)
    except ValueError:
        return r.status, None


# --- wie erbij mag ---
check("zonder header: 403", post({"general.app_language": "en"}, header=False)[0] == 403 and settings.stored("general.app_language") == "nl")
check("zonder sessie: 403", post({"general.app_language": "en"}, session=False)[0] == 403 and settings.stored("general.app_language") == "nl")
_c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
_c.request("GET", "/settings?client_id=12345678-1234-1234-1234-123456789abd",
           headers={"Host": f"localhost:{PORT}", "Cookie": f"{serve.SESSION_COOKIE}={serve.SESSION}", "X-Local-Transcribe": "1"})
_status = _c.getresponse().status
check("als GET: 405 en niets veranderd", _status == 405 and settings.stored("iconik.client_id").endswith("abc"), _status)

# --- bewaren ---
status, r = post({"general.app_language": "en", "transcription.language": "uk", "iconik.auto_max_minutes": "90"})
check("meerdere tegelijk: bewaard", status == 200 and r["ok"] and settings.stored("general.app_language") == "en" and settings.stored("transcription.language") == "uk"
      and settings.stored("iconik.auto_max_minutes") == 90.0, r)
check("antwoord geeft de bewaarde waarden", r["values"] == {"general.app_language": "en", "transcription.language": "uk",
                                                            "iconik.auto_max_minutes": 90.0}, r)
check("de rest bleef staan", settings.stored("iconik.mounts") == {"opslag-1": "/Volumes/ISG"} and settings.stored("iconik.client_id").startswith("12345678"))

# --- alles of niets ---
status, r = post({"general.app_language": "nl", "iconik.auto_max_minutes": -5})
check("één ongeldig: niets bewaard", not r["ok"] and settings.stored("general.app_language") == "en" and settings.stored("iconik.auto_max_minutes") == 90.0, r)
check("melding per sleutel", list(r["errors"]) == ["iconik.auto_max_minutes"] and "Langste asset" in r["error"], r)
status, r = post({"bestaat.niet": 1})
check("onbekende instelling", not r["ok"] and "bestaat.niet" in r["errors"], r)
status, r = post({"iconik.mounts": {}})
check("mounts alleen via de mappenkiezer", not r["ok"] and settings.stored("iconik.mounts") == {"opslag-1": "/Volumes/ISG"}, r)
status, r = post({"refine.subtitles": {}})
check("ondertitels alleen via Refine", not r["ok"], r)
status, r = post({"iconik.client_id": ""})
check("client-ID niet leeg", not r["ok"] and r["error"] == "Dat lijkt geen geldige client-ID." and settings.stored("iconik.client_id").startswith("12345678"), r)
status, r = post({})
check("leeg verzoek", not r["ok"], r)
status, r = post(None, raw=b"{kapot")
check("geen JSON", status == 200 and not r["ok"] and r["error"] == "json", r)
status, r = post([1, 2])
check("JSON maar geen lijst van sleutels", not r["ok"], r)
status, _ = post(None, raw=b"x" * 20000)
check("te groot: 413", status == 413, status)

# --- wat er na het bewaren gebeurt ---
worker.wake.clear()
status, r = post({"iconik.worker_enabled": True})
check("opdrachten op afstand aan: de werker wordt wakker", r["ok"] and serve.worker_enabled() is True and worker.wake.is_set(), r)
worker.wake.clear()
serve.storage_wake.clear()
status, r = post({"iconik.auto_transcribe": True})
check("auto aan: opslag en werker meteen kijken", r["ok"] and serve.storage_wake.is_set() and worker.wake.is_set(), r)
status, r = post({"iconik.machine_name": "  Montage\n 3  " + "x" * 60})
check("naam van de Mac: opgeschoond en ingekort zoals altijd", r["ok"] and len(settings.stored("iconik.machine_name")) <= worker.MAX_NAME
      and settings.stored("iconik.machine_name").startswith("Montage 3") and worker.machine_name() == settings.stored("iconik.machine_name"), settings.stored("iconik.machine_name"))
is_busy[0] = True
status, r = post({"iconik.machine_name": "Anders", "general.app_language": "nl"})
check("naam niet tijdens een opdracht, en dan ook de rest niet", not r["ok"] and "Wacht" in r["error"]
      and settings.stored("general.app_language") == "en" and worker.machine_name().startswith("Montage 3"), r)
is_busy[0] = False
status, r = post({"iconik.iconik_language": "en"})
check("labeltaal: niet ingelogd, dus niet bewaard", not r["ok"] and "Niet ingelogd" in r["error"] and settings.stored("iconik.iconik_language") is None, r)
status, r = post({"iconik.iconik_language": ""})
check("labeltaal leeg (volgt de app): zonder Iconik bewaard", r["ok"] and settings.stored("iconik.iconik_language") == "", r)
logouts.clear()
status, r = post({"iconik.host": "https://app.iconik.io"})
check("Iconik-adres gelijk: blijft ingelogd", r["ok"] and not logouts, r)
status, r = post({"iconik.host": "https://eu.iconik.io"})
check("ander Iconik-adres: uitgelogd", r["ok"] and logouts == [1] and transcribe.api_host() == "https://eu.iconik.io", r)
status, r = post({"iconik.host": "http://eu.iconik.io"})
check("Iconik-adres zonder https geweigerd", not r["ok"] and settings.stored("iconik.host") == "https://eu.iconik.io", r)

# --- /state is voor het opdrachtenvenster; Instellingen haalt zijn gegevens bij /settings-data ---
def fetch(path):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.request("GET", path, headers={"Host": f"localhost:{PORT}", "Cookie": f"{serve.SESSION_COOKIE}={serve.SESSION}"})
    r = c.getresponse()
    body = r.read()
    return r.status, (json.loads(body) if r.status == 200 else None)


_, state = fetch("/state")
check("/state: wat het opdrachtenvenster nodig heeft", {"jobs", "account", "actions", "lang", "update", "mounts", "worker"} <= set(state), sorted(state))
check("/state: niets meer van het oude Voorkeuren-blok", not {"general", "integrations", "app_language", "spoken", "model", "originals"} & set(state), sorted(state))
_, data = fetch("/settings-data?group=iconik")
check("/settings-data: de waarden van Iconik", data["values"]["iconik.host"] == "https://eu.iconik.io"
      and [i["type"] for i in data["extra"]["instances"]] == ["iconik"], data["values"])
_, data = fetch("/settings-data?group=general")
check("/settings-data: Algemeen met de taal van de app", data["values"] == {"general.app_language": "en"}, data["values"])
check("/settings-data: een onbekende groep bestaat niet", fetch("/settings-data?group=elders")[0] == 404)

# --- elke wijziging gaat via dezelfde ingang ---
check("alle wijzigingen in serve.py via apply_settings", "set_config(" not in Path("app/serve.py").read_text())
check("elke sleutel in AFTER_SAVE bestaat", all(k in serve.setting.SCHEMA for k in serve.AFTER_SAVE))

server.shutdown()
print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
