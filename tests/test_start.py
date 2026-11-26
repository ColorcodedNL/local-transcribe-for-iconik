"""serve.start() als geheel (stap 6a): eerst de bestanden van vóór stap 4 verhuizen, dan de lijst laden, dan de draden.
Lokale modus voegt draden toe; deze test start de ontvanger echt, met een oude supportmap, en laat een lokale
opdracht door de werkrij lopen. Alleen het netwerk (updates, het icoon van Iconik) en het transcriberen zelf zijn
vervangen.

Draaien: uv run --no-project --with pynacl --with requests --with numpy python -B tests/test_start.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
import urllib.request
from pathlib import Path

sys.path.insert(0, "app")
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
import transcribe  # noqa: E402

transcribe.L = lambda nl, en: nl
tmp = Path(tempfile.mkdtemp())
support = tmp / "support"
transcribe.CONFIG_PATH = support / "config.json"
transcribe.TRANSCRIPTS_DIR = support / "transcripts"
transcribe.WAVES_DIR = support / "waves"
transcribe.SIDECARS_FILE = support / "sidecars.json"
transcribe.LOCAL_INDEX = support / "local-files.json"

import connectors  # noqa: E402
import items  # noqa: E402
import refine  # noqa: E402
import serve  # noqa: E402
import transcript as model  # noqa: E402

refine.DRAFTS_DIR = support / "refine"
serve.KEY_FILE = support / "key"
serve.jobs_path = lambda: support / "jobs.json"
serve.check_update = lambda: None            # geen GitHub
serve.integration_icon = lambda instance=None: ""   # geen pagina van Iconik ophalen
log = []
serve.print = lambda *a, **k: log.append(" ".join(str(x) for x in a))
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


# Een supportmap zoals een Mac van vóór stap 4 hem achterliet: een los transcript en jobs.json zonder versie,
# met een opdracht die nog liep toen de app stopte.
(support / "transcripts").mkdir(parents=True)
(support / "transcripts" / "A1.json").write_text(json.dumps(model.to_dict(model.Transcript(language="nl"))))
(support / "jobs.json").write_text(json.dumps({"saved": "2026-11-01T10:00:00", "jobs": [
    {"n": 1, "asset_id": "A1", "title": "klaar", "status": "klaar"},
    {"n": 2, "asset_id": "A2", "title": "liep nog", "status": "bezig"}]}))

# Het transcriberen zelf vervangen: wat er wel echt gebeurt, is de bron openen en het transcript wegschrijven.
ran = []


def fake_process(args, hf_token, on_info=None, prepared=None):
    source = items.source(args.item)
    info = connectors.call(source, "open", key=items.key(args.item), push=True, replace=True)
    doc = model.Transcript(language="nl", speakers=[model.Speaker("1", "Spreker 1")],
                           segments=[model.Segment(0, 900, "1", [model.Word("Hallo", 0, 900, 0.9)])],
                           engine={"name": "mlx-whisper"}, source={"item": args.item, "media": info["media"]})
    result = connectors.call(source, "write", info=info, doc=model.to_dict(doc), engine_info={"name": "mlx-whisper"})
    ran.append(threading.current_thread().name)
    return {"title": info["title"], "segments": result["segments"], "speakers": 1, "pushed": True, "language": "nl"}


transcribe.process_asset = fake_process
transcribe.speaker_model_available = lambda token: True

server = serve.start(port=0)
port = server.server_address[1]

# --- eerst verhuisd, dan geladen ---
instances = [i["id"] for i in serve.setting.integrations()]
check("start: precies één Iconik, gemaakt door de migratie", len(instances) == 1, instances)
source = instances[0]
check("start: het losse transcript staat in de map van die Iconik", (support / "transcripts" / source / "A1.json").is_file()
      and not (support / "transcripts" / "A1.json").exists())
rows = {j.n: j for j in serve.jobs}
check("start: de oude regels horen bij dezelfde Iconik", rows[1].item == f"{source}:A1" and rows[2].item == f"{source}:A2",
      {n: j.item for n, j in rows.items()})
check("start: wat nog liep is afgebroken, niet hervat", rows[2].status == "afgebroken" and serve.work.empty())

# --- de draden en de ontvanger ---
names = {t.name for t in threading.enumerate()}
check("draden: ontvanger, werkrij, waker, opslag en bewaren draaien", {"ontvanger", "werkrij", "waker", "opslag", "bewaren"} <= names,
      sorted(names))
check("connectors: de lokale bron is geladen", "local" in connectors._loaded and connectors.host is not None)
with urllib.request.urlopen(f"http://127.0.0.1:{port}/window/state", timeout=5) as response:
    check("ontvanger: antwoordt", json.loads(response.read()) == {"ok": True})

# --- een lokale opdracht door de werkrij ---
media = tmp / "opnames" / "clip.mov"
media.parent.mkdir()
media.write_bytes(os.urandom(4000))
result = serve.add_local([str(media)])
check("lokaal: toegevoegd", result == {"added": 1, "skipped": 0}, result)
job = max(serve.jobs, key=lambda j: j.n)
deadline = time.time() + 20
while job.status in ("wachtend", "bezig") and time.time() < deadline:
    time.sleep(0.1)
check("lokaal: de werkrij deed hem in zijn eigen draad", job.status == "klaar" and ran == ["werkrij"], (job.status, job.message, ran))
check("lokaal: het transcript staat naast de media", (media.parent / "clip.transcript.json").is_file())
serve.save_jobs()
saved = json.loads((support / "jobs.json").read_text())
check("lokaal: jobs.json versie 2 met het lokale item", saved.get("version") == 2
      and any(r["item"] == job.item and r["item"].startswith("local:") and r["asset_id"] == "" for r in saved["jobs"]))
check("lokaal: geen Iconik-inlog nodig", "Iconik" not in job.message, job.message)

server.shutdown()
print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
