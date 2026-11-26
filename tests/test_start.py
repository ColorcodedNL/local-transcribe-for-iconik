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
import subtitles  # noqa: E402
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
    if info["title"] == "slot.mov":   # alsof de kaart tijdens het werk op slot ging
        transcribe.fail("Kan hier niet schrijven: alleen-lezen.", place=True)
    doc = model.Transcript(language="nl", speakers=[model.Speaker("1", "Spreker 1")],
                           segments=[model.Segment(0, 900, "1", [model.Word("Hallo", 0, 900, 0.9)])],
                           engine={"name": "mlx-whisper"}, source={"item": args.item, "media": info["media"]})
    result = connectors.call(source, "write", info=info, doc=model.to_dict(doc), engine_info={"name": "mlx-whisper"})
    items.save_transcript(doc, args.item)   # zoals de pipeline: de kopie op deze Mac
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
check("draden: ontvanger, werkrij, waker, opslag, bewaren en mappen draaien", {"ontvanger", "werkrij", "waker", "opslag", "bewaren", "mappen"} <= names,
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

# --- kan tijdens het werk niet schrijven (stap 6d): wacht op een plek, geen fout, niet opnieuw geprobeerd ---
locked = media.parent / "slot.mov"
locked.write_bytes(os.urandom(5000))
serve.add_local([str(locked)])
job = max(serve.jobs, key=lambda j: j.n)
deadline = time.time() + 20
while job.status in ("wachtend", "bezig") and time.time() < deadline:
    time.sleep(0.1)
check("tijdens het werk: wacht op een plek, met de reden", job.status == serve.PLACE and "alleen-lezen" in job.message, (job.status, job.message))
import local_files  # noqa: E402
check("tijdens het werk: niet op de lijst van mislukt (een plek kiezen probeert het weer)", not local_files.skipped(locked))

# --- ondertitels vanzelf (stap 6e) ---
def run(path):
    serve.add_local([str(path)])
    job = max(serve.jobs, key=lambda j: j.n)
    deadline = time.time() + 20
    # De werkrij zet klaar en maakt daarna pas de ondertitels: wachten tot die er ook zijn (op de Mac zag de test
    # het moment ertussen, 26 november).
    while time.time() < deadline and (job.status in ("wachtend", "bezig") or (
            job.status == "klaar" and serve.setting.get("files.subtitles") and "subtitles" not in (job.result or {}))):
        time.sleep(0.1)
    return job


job = run(media)
check("ondertitels: standaard uit, geen .srt", job.status == "klaar" and not (media.parent / "clip.srt").exists()
      and "subtitles" not in (job.result or {}))
serve.setting.put("files.subtitles", True)
job = run(media)
srt = media.parent / "clip.srt"
check("ondertitels: aan, clip.srt bij het transcript", job.status == "klaar" and srt.is_file() and "Hallo" in srt.read_text(),
      (job.status, job.message))
check("ondertitels: de telling in het resultaat", job.result.get("subtitles") == {"cues": 1, "warnings": 0, "bad": 0, "failed": [], "exists": []},
      job.result)
layer = refine.load_subtitles(job.item)
check("ondertitels: dezelfde laag als in Refine, niet verouderd", layer is not None and not layer["stale"]
      and layer["target"]["names"] == ["clip.srt"], layer and layer["target"])
check("ondertitels: geschreven door de app (een volgende keer mag hij hem vervangen)",
      subtitles.unchanged(srt, transcribe.SIDECARS_FILE))
serve.setting.put("files.subtitle_formats", "both")
job = run(media)
check("ondertitels: een automatische laag wordt opnieuw gemaakt, nu ook .vtt", (media.parent / "clip.vtt").is_file()
      and job.result["subtitles"]["cues"] == 1 and refine.load_subtitles(job.item)["target"]["formats"] == "both")
kept = refine.load_subtitles(job.item)
refine.save_subtitles(job.item, json.dumps({"transcript": kept["transcript"], "touched": [True], "edits": 1, "rules": kept["rules"]}))
before = srt.read_text()
job = run(media)
check("ondertitels: aangeraakt in Refine, dan blijft de laag staan", job.result["subtitles"] == {"kept": True}
      and srt.read_text() == before and refine.load_subtitles(job.item)["edits"] == 1, job.result)
refine.discard_subtitles(job.item)
other = media.parent / "ander.mov"
other.write_bytes(os.urandom(4500))
(media.parent / "ander.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nvan iemand anders\n")
serve.setting.put("files.subtitle_formats", "srt")
job = run(other)
check("ondertitels: een .srt die de app niet schreef blijft staan", job.status == "klaar"
      and "iemand anders" in (media.parent / "ander.srt").read_text() and job.result["subtitles"]["exists"] == ["ander.srt"], job.result)
subs_folder = tmp / "ondertitels"
serve.setting.put_many({"files.subtitle_place": "folder", "files.subtitle_folder": str(subs_folder)})
job = run(media)
found = list(subs_folder.rglob("clip.srt"))
check("ondertitels: In één map, met de mappenstructuur", len(found) == 1 and found[0].parent.name == "opnames", (found, job.result))
serve.setting.put_many({"files.subtitles": False, "files.subtitle_place": "transcript"})

server.shutdown()
print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
