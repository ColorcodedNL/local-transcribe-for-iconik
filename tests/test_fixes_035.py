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

transcribe.L = lambda nl, en: nl
tmp = Path(tempfile.mkdtemp())
transcribe.CONFIG_PATH = tmp / "config.json"
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


def get(path, headers=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.putrequest("GET", path, skip_host=True)
    c.putheader("Host", host or f"localhost:{PORT}")
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
for path in ("/clear", "/logout", "/applang?value=nl", "/language?value=nl", "/actions/install", "/settings?client_id=x"):
    status, _, _ = get(path)
    check(f"{path} zonder header geweigerd", status == 403, status)
status, _, _ = get("/state", host="evil.example:%d" % PORT)
check("andere Host-naam geweigerd (DNS-rebinding)", status == 403, status)
status, _, _ = get("/state", host=f"127.0.0.1:{PORT}")
check("127.0.0.1 als Host mag", status == 200, status)
status, _, body = get("/settings?client_id=nee", WINDOW)
check("ongeldige client-ID geeft een melding", status == 200 and not json.loads(body)["ok"], body)
status, _, body = get("/settings?client_id=ABCDEF12-1234-1234-1234-123456789ABC", WINDOW)
check("geldige client-ID wordt bewaard", json.loads(body)["ok"] and transcribe.load_config()["client_id"].startswith("abcdef12"))
status, _, body = get("/applang?value=en", WINDOW)
check("taal zetten laat de client-ID staan", transcribe.load_config().get("app_language") == "en"
      and transcribe.load_config().get("client_id", "").startswith("abcdef12"), transcribe.load_config())


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
serve.cleared[0] = 0
for i in range(600):
    serve.add_job(f"a{i}")
for job in serve.jobs:
    job["status"] = "klaar"
serve.save_jobs()
serve.jobs.clear()
serve.cleared[0] = 0
while not serve.work.empty():
    serve.work.get()
serve.load_jobs()
serve.add_job("nieuw")
numbers = [j["n"] for j in serve.jobs]
check("na herstart geen dubbele nummers", len(numbers) == len(set(numbers)) and serve.jobs[-1]["n"] == 601, serve.jobs[-1]["n"])
serve.clear_finished()
serve.jobs.clear()
while not serve.work.empty():
    serve.work.get()
serve.add_job("na opruimen")
check("na opruimen loopt de nummering door", serve.jobs[-1]["n"] > 600, serve.jobs[-1]["n"])
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
      all(config.get(f"k{i}") == i for i in range(40)) and len(config.get("mounts", {})) == 40 and config["client_id"] == "c")
transcribe.set_mount("s0", None)
check("mount vergeten", "s0" not in transcribe.load_config()["mounts"])
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
order = [c for c in iconik.calls if c[0] == "POST"]
check("push gelukt: oud pas weg na het nieuwe", problem is None and deleted == ["oud"] and len(order) == 2, (deleted, problem))
check("precies een transcript van de app over", [t["id"] for t in iconik.transcripts] == ["nieuw"], iconik.transcripts)

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
transcribe.save_config({k: v for k, v in transcribe.load_config().items() if k != "expected_languages"})

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

server.shutdown()
print("ALLES OK" if not fails else f"{fails} FOUT(EN)")
sys.exit(1 if fails else 0)
