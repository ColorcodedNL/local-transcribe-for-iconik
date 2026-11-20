"""Regressietests voor 0.3.5: inloggen, Host- en headercontrole, opdrachtnummers, voorkeuren,
push-volgorde en sporenvergelijking. Draaien vanuit de projectmap:

    uv run --no-project --with pynacl --with requests --with numpy python -B tests/test_fixes_035.py
"""
import http.client
import json
import sys
import tempfile
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "app")
kr = types.ModuleType("keyring")
_store = {}
kr.get_password = lambda s, u: _store.get((s, u))
kr.set_password = lambda s, u, p: _store.__setitem__((s, u), p)
kr.delete_password = lambda s, u: _store.pop((s, u))
sys.modules["keyring"] = kr
for name in ("mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)

import numpy as np  # noqa: E402
import transcribe, serve  # noqa: E402,E401
import settings  # noqa: E402

transcribe.L = lambda nl, en: nl
tmp = Path(tempfile.mkdtemp())
transcribe.CONFIG_PATH = tmp / "config.json"
transcribe.TRANSCRIPTS_DIR = tmp / "transcripts"   # nooit in de echte map met transcripten schrijven
serve.jobs_path = lambda: tmp / "jobs.json"
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


# --- 1. De ontvanger: inloggen, Host en header
transcribe.save_config({"client_id": "12345678-1234-1234-1234-123456789abc"})
serve.settings.update(key="k", hf_token=None, model="m", language=None, mount=None, idle_exit=False)
server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
WINDOW = {"X-Local-Transcribe": "1"}


def get(path, headers=None, host=None, session=True, method=None):
    """Een verzoek zoals het venster het doet: met de sessiecookie, en met de header erbij als POST
    (alles wat iets verandert). `session=False`: iets anders op deze Mac, zonder cookie."""
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    method = method or ("POST" if headers and "X-Local-Transcribe" in headers else "GET")
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host or f"localhost:{PORT}")
    if session:
        c.putheader("Cookie", f"{serve.SESSION_COOKIE}={serve.SESSION}")
    if method == "POST":
        c.putheader("Content-Length", "0")
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    return r.status, r.getheader("Location") or "", body


status, location, _ = get("/login")
check("/login stuurt door naar Iconik (zonder header, zoals een link)", status == 303 and "/oauth/authorize" in location, (status, location))
status, _, _ = get("/login-item?value=off")
check("/login-item zonder header geweigerd", status == 403, status)
status, _, body = get("/login-item?value=off", WINDOW)
check("/login-item met header werkt", status == 200 and json.loads(body)["ok"], status)
for path in ("/clear", "/logout", "/settings", "/actions/install", "/window/settings", "/integrations/remove?id=x"):
    status, _, _ = get(path)
    check(f"{path} zonder header geweigerd", status == 403, status)
status, _, _ = get("/state", host="evil.example:%d" % PORT)
check("andere Host-naam geweigerd (DNS-rebinding)", status == 403, status)
status, _, _ = get("/state", host=f"127.0.0.1:{PORT}")
check("127.0.0.1 als Host mag", status == 200, status)

# Sessie: alleen het venster (cookie) en de app zelf (sleutel) komen erin
for path in ("/", "/state", "/refine", "/refine/view?asset=abcdef12", "/refine/chunk?asset=abcdef12&t=0", "/refine/waves?asset=abcdef12", "/login"):
    status, _, _ = get(path, session=False)
    check(f"{path} zonder sessie geweigerd", status == 403, status)
status, _, _ = get("/state", {"Cookie": f"{serve.SESSION_COOKIE}=verkeerd"}, session=False)
check("verkeerde cookie geweigerd", status == 403, status)
status, _, _ = get("/state", {serve.KEY_HEADER: "k"}, session=False)
check("de app zelf komt erin met de sleutel", status == 200, status)
status, _, _ = get("/state", {serve.KEY_HEADER: "fout"}, session=False)
check("verkeerde sleutel geweigerd", status == 403, status)
status, _, _ = get("/clear", WINDOW, session=False)
check("iets veranderen met header maar zonder sessie geweigerd", status == 403, status)
status, _, _ = get("/logout", WINDOW, method="GET")
check("iets veranderen als GET geweigerd (405), ook met header en sessie", status == 405, status)
status, _, _ = get("/window/open?t=verkeerd", session=False)
check("/window/open met een verkeerde sessie geweigerd", status == 403, status)
c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
c.request("GET", f"/window/open?t={serve.SESSION}")
r = c.getresponse(); r.read()
cookie = r.getheader("Set-Cookie") or ""
check("/window/open geeft het venster de cookie en stuurt door naar de app",
      r.status == 303 and r.getheader("Location") == "/" and cookie.startswith(f"{serve.SESSION_COOKIE}={serve.SESSION};")
      and "HttpOnly" in cookie and "SameSite=Lax" in cookie, (r.status, cookie))
# Een pagina die de app zelf opent (een klik in Iconik bij een koude start) gaat langs /window/open.
def opened(page):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.request("GET", serve.window_url(page))
    r = c.getresponse(); r.read()
    return r.status, r.getheader("Location"), (r.getheader("Set-Cookie") or "").startswith(f"{serve.SESSION_COOKIE}=")
check("een eigen pagina openen geeft de cookie en stuurt naar die pagina",
      opened("/refine?asset=abc&tab=subtitles") == (303, "/refine?asset=abc&tab=subtitles", True), opened("/refine?asset=abc&tab=subtitles"))
check("zonder pagina naar de app", opened("") == (303, "/", True) and opened("/login") == (303, "/login", True))
check("nooit naar een ander adres", all(opened(p)[1] == "/" for p in ("//evil.example/x", "https://evil.example", "/\\evil.example", "/a\r\nSet-Cookie: x=1")),
      [opened(p)[1] for p in ("//evil.example/x", "https://evil.example", "/\\evil.example")])
status, _, body = get("/window/state", session=False)
check("tweede start ziet zonder sessie alleen dat de app er is", status == 200 and json.loads(body) == {"ok": True}, body)
status, _, body = get("/window/state")
check("het venster ziet de eigen toestand", status == 200 and "page" in json.loads(body), body)
status, _, _ = get("/window/show?page=/", WINDOW, session=False, method="GET")
check("venster naar voren halen kan zonder sessie (oudere tweede start)", status == 200, status)
status, _, _ = get("/transcribe/fout?asset_ids=x", session=False)
check("link uit Iconik met verkeerde sleutel geweigerd", status == 403, status)
status, location, _ = get("/transcribe/k?asset_ids=abcdef12", session=False)
check("link uit Iconik met de sleutel werkt zonder sessie", status == 303, (status, location))
serve.pending_ids.clear()
with serve.jobs_lock:
    serve.jobs.clear()

for path in ("/applang?value=en", "/language?value=en", "/settings?client_id=x", "/auto?value=on", "/machine-name?value=x",
             "/iconik-language?value=en"):
    status, _, _ = get(path, WINDOW)
    check(f"{path}: de oude route bestaat niet meer (dat gaat nu via POST /settings)", status == 404, status)
check("niets veranderd door de oude routes", settings.stored("general.app_language") != "en" and not settings.stored("iconik.auto_transcribe"))


def boom():
    raise ConnectionError("geen netwerk")


real_start = transcribe.start_login
transcribe.start_login = boom
status, _, body = get("/login")
transcribe.start_login = real_start
check("onverwachte fout geeft een pagina, geen verbroken verbinding", status == 500 and b"geen netwerk" in body, status)

# --- 2. Opdrachtnummers na meer dan 500 opdrachten en een herstart
RealThread = threading.Thread
threading.Thread = lambda *a, **k: SimpleNamespace(start=lambda: None)   # geen titels ophalen
serve.jobs.clear()
serve.jobs.cleared = 0
for i in range(600):
    serve.add_job(f"a{i}")
for job in serve.jobs:
    job.set(status="klaar")
serve.save_jobs()
serve.jobs.clear()
serve.jobs.cleared = 0
while not serve.work.empty():
    serve.work.get()
serve.load_jobs()
serve.add_job("nieuw")
numbers = [j.n for j in serve.jobs]
check("na herstart geen dubbele nummers", len(numbers) == len(set(numbers)) and serve.jobs[-1].n == 601, serve.jobs[-1].n)
serve.clear_finished()
serve.jobs.clear()
while not serve.work.empty():
    serve.work.get()
check("een asset-ID dat er geen kan zijn komt niet in de rij", serve.add_job("na opruimen") is False and len(serve.jobs) == 0)
serve.add_job("na-opruimen")
check("na opruimen loopt de nummering door", serve.jobs[-1].n > 600, serve.jobs[-1].n)
threading.Thread = RealThread

# --- 3. Voorkeuren: tegelijk schrijven verliest niets
transcribe.save_config({"client_id": "c"})
workers = [threading.Thread(target=transcribe.set_config, args=(f"k{i}", i)) for i in range(40)]
workers += [threading.Thread(target=transcribe.set_mount, args=(f"s{i}", f"/Volumes/V{i}")) for i in range(40)]
for w in workers:
    w.start()
for w in workers:
    w.join()
config = transcribe.load_config()
check("40 instellingen en 40 mounts tegelijk: alles bewaard",
      all(config.get(f"k{i}") == i for i in range(40)) and len(settings.stored("iconik.mounts")) == 40
      and settings.stored("iconik.client_id") == "c")
transcribe.set_mount("s0", None)
check("mount vergeten", "s0" not in settings.stored("iconik.mounts"))
check("geen tijdelijk bestand achtergebleven", not (tmp / "config.json.tmp").exists())

# --- 4. Push: eerst nieuw, dan oud weg; bij een fout blijft het oude staan


class FakeIconik:
    def __init__(self, fail_bulk=False, extra=()):
        self.calls, self.fail_bulk = [], fail_bulk
        self.transcripts = [{"id": "oud", "engine_info": {"name": transcribe.ENGINE_NAME}}, *extra]

    def get(self, path):
        self.calls.append(("GET", path))
        if path.endswith("/transcriptions/properties/"):
            return {"objects": list(self.transcripts)}
        return {"objects": []}

    def post(self, path, body):
        self.calls.append(("POST", path))
        if path.endswith("/transcriptions/properties/"):
            self.transcripts.append({"id": "nieuw", "engine_info": {"name": transcribe.ENGINE_NAME}})
            return {"id": "nieuw"}
        if self.fail_bulk:
            raise transcribe.Problem("HTTP 502")
        return {}


def run_push(iconik, still_ours=None, probability=0.99, language="nl", detected="nl", words_count=2):
    deleted = []
    transcribe.delete_transcription = lambda i, a, v, t: (deleted.append(t), i.transcripts.__setitem__(
        slice(None), [x for x in i.transcripts if x["id"] != t]), 0)[2]
    words = [{"word": f" woord{i}", "start": i * 0.5, "end": i * 0.5 + 0.5, "probability": probability}
             for i in range(words_count)]
    transcribe.detect_language = lambda samples, model: (detected, 0.9)
    mlx_whisper = types.ModuleType("mlx_whisper")
    mlx_whisper.transcribe = lambda *a, **k: {"segments": [{"text": "Hallo wereld.", "start": 0.0, "words": words}], "language": "nl"}
    sys.modules["mlx_whisper"] = mlx_whisper
    sys.modules["mlx_whisper.transcribe"] = types.ModuleType("mlx_whisper.transcribe")
    mx = types.ModuleType("mlx.core")
    mx.set_cache_limit = mx.clear_cache = lambda *a: None
    sys.modules["mlx.core"] = mx
    sys.modules["mlx"].core = mx
    transcribe.pkg_version = lambda name: "0"
    sys.modules.pop("torch", None)   # de lege vervanger heeft geen torch.backends
    transcribe.memory_report = lambda stage: None
    args = SimpleNamespace(asset_id="A", model="m", language=language, push=True, replace=True, no_speakers=True,
                           speakers=None, no_voice_check=True, still_ours=still_ours)
    prepared = {"asset": {"title": "t"}, "version_id": "V", "own": [iconik.transcripts[0]],
                "samples": np.zeros(16000, np.float32), "microphones": None, "model_path": "m"}
    try:
        transcribe.process_asset(iconik, args, None, prepared=prepared)
        return deleted, None
    except transcribe.Problem as problem:
        return deleted, problem


iconik = FakeIconik()
deleted, problem = run_push(iconik)
order = [c for c in iconik.calls if c[0] == "POST" and not c[1].endswith("/reindex/")]
check("push gelukt: oud pas weg na het nieuwe", problem is None and deleted == ["oud"] and len(order) == 2, (deleted, problem))
check("na vervangen wordt de zoekindex bijgewerkt", iconik.calls[-1][1].endswith("/reindex/"), iconik.calls[-1:])
check("precies een transcript van de app over", [t["id"] for t in iconik.transcripts] == ["nieuw"], iconik.transcripts)
import json as _json  # noqa: E402
_files = sorted(transcribe.TRANSCRIPTS_DIR.glob("*/*.json"))   # per bron een map (stap 4)
_saved = _json.loads(_files[0].read_text()) if _files else {}
check("kopie in het eigen model bewaard, met het id van het nieuwe transcript",
      len(_files) == 1 and _saved.get("format") == "local-transcribe" and _saved.get("source", {}).get("transcription_id") == "nieuw",
      (_files, _saved.get("source")))

iconik = FakeIconik(fail_bulk=True)
deleted, problem = run_push(iconik)
check("push mislukt: oud blijft, half nieuw weer weg", problem is not None and deleted == ["nieuw"]
      and [t["id"] for t in iconik.transcripts] == ["oud"], (deleted, iconik.transcripts))

iconik = FakeIconik(extra=[{"id": "andere-mac", "engine_info": {"name": transcribe.ENGINE_NAME}}])
deleted, problem = run_push(iconik)
check("transcript dat een andere Mac intussen zette gaat ook weg", sorted(deleted) == ["andere-mac", "oud"], deleted)

iconik = FakeIconik()
deleted, problem = run_push(iconik, still_ours=lambda: False)
check("overgenomen asset: niets geschreven", problem is not None and getattr(problem, "skip", False)
      and not [c for c in iconik.calls if c[0] == "POST"] and not deleted, iconik.calls)

# 0.3.6: een run onder de zekerheidsdrempel laat het eerdere transcript staan
iconik = FakeIconik()
deleted, problem = run_push(iconik, probability=0.05)
check("onder de drempel: overgeslagen, eerder transcript blijft staan", problem is not None and getattr(problem, "skip", False)
      and not deleted and [t["id"] for t in iconik.transcripts] == ["oud"]
      and ("behouden" in str(problem) or "kept" in str(problem)), (deleted, iconik.transcripts, problem))

# 0.4: een paar woorden in een onverwachte taal (muziek die Whisper als Russisch hoort) wordt overgeslagen
iconik = FakeIconik()
deleted, problem = run_push(iconik, language="auto", detected="ru")
check("2 woorden herkend als Russisch: overgeslagen, eerder transcript blijft", problem is not None
      and getattr(problem, "skip", False) and not deleted and [t["id"] for t in iconik.transcripts] == ["oud"]
      and "ru" in str(problem), (deleted, problem))
iconik = FakeIconik()
deleted, problem = run_push(iconik, language="auto", detected="ru", words_count=12)
check("een echt Russisch item met meer tekst gaat door", problem is None and deleted == ["oud"], (deleted, problem))
iconik = FakeIconik()
deleted, problem = run_push(iconik, language="auto", detected="en")
check("2 woorden Engels gaat door (verwachte taal)", problem is None, problem)
iconik = FakeIconik()
deleted, problem = run_push(iconik, language="ru", detected="ru")
check("vast gekozen taal: geen controle", problem is None, problem)
transcribe.save_config({**transcribe.load_config(), "expected_languages": ["nl", "en", "ru"]})
iconik = FakeIconik()
deleted, problem = run_push(iconik, language="auto", detected="ru")
check("verwachte talen zijn instelbaar", problem is None, problem)
settings.raw_put("transcription.expected_languages", None)   # weer de standaard

# --- 5. Sporenvergelijking op 1 kHz geeft dezelfde uitkomst als np.corrcoef
rng = np.random.default_rng(1)
a = rng.standard_normal(16000 * 30).astype(np.float32)
copy = a * 0.5 + rng.standard_normal(a.size).astype(np.float32) * 0.05
other = rng.standard_normal(a.size).astype(np.float32)
for name, b in (("kopie", copy), ("ander signaal", other)):
    full = abs(float(np.corrcoef(a, b)[0, 1]))
    fast = transcribe.similarity(a, b)
    check(f"similarity {name}: {fast:.3f} tegen corrcoef {full:.3f}", abs(full - fast) < 0.02
          and (fast > 0.9) == (full > 0.9))
check("stil spoor geeft 0", transcribe.similarity(np.zeros(16000, np.float32), a[:16000]) == 0.0)

# --- asset-ID's voor de editor, log roteren, versie in pyproject
for good in ("A1", "2df8d8e0-a790-11f0-9edb-4a3452efd6a5", "a_b-c"):
    check(f"asset-ID {good} mag", serve.valid_asset_id(good))
for bad in ("", "../../users/v1/users", "a/b", "a?b=c", "a.b", "a b", "x" * 65, None):
    check(f"asset-ID {bad!r:.30} geweigerd", not serve.valid_asset_id(bad))
for path in ("/refine/view?asset=../../users", "/refine/chunk?asset=a%2Fb&t=0", "/refine/waves?asset="):
    status, _, body = get(path)
    check(f"{path}: geweigerd zonder Iconik te vragen", status == 200 and json.loads(body) == {"error": "asset"}, body)
status, _, body = get("/refine/save?asset=..%2Fx", {**WINDOW, "Content-Type": "application/json"})
check("opslaan met een raar asset-ID geweigerd", status == 200 and json.loads(body) == {"error": "asset"}, body)

log = tmp / "serve.log"
log.write_text("x" * 100)
check("log onder de grens blijft staan", serve.rotate_log(log, limit=100) is False and log.stat().st_size == 100)
log.write_text("y" * 101)
check("log boven de grens wordt .1", serve.rotate_log(log, limit=100) is True and not log.exists()
      and (tmp / "serve.log.1").read_text() == "y" * 101)
log.write_text("z" * 200)
check("een vorige .1 wordt vervangen", serve.rotate_log(log, limit=100) is True and (tmp / "serve.log.1").read_text() == "z" * 200)
check("geen log: niets te doen", serve.rotate_log(tmp / "bestaat-niet.log") is False)

import re as _re
project = _re.search(r'^version = "([^"]+)"', Path("pyproject.toml").read_text(), _re.M).group(1)
check("versie in pyproject.toml is de versie van de app", project == transcribe.APP_VERSION, (project, transcribe.APP_VERSION))

server.shutdown()
print("ALLES OK" if not fails else f"{fails} FOUT(EN)")
sys.exit(1 if fails else 0)
