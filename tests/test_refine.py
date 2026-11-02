"""Refine (app/refine.py): het transcript van deze Mac openen, stukjes van het origineel voor de speler, en de
golfvormen per spoor (uit een opdracht bewaard, of achteraf uitgerekend).

Draaien: uv run --no-project --with pynacl --with requests --with numpy python -B tests/test_refine.py
Vraagt ffmpeg en ffprobe op het pad.
"""
import json
import subprocess
import sys
import tempfile
import time
import types
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
import numpy as np  # noqa: E402
import transcribe  # noqa: E402
import transcript as model  # noqa: E402
import refine  # noqa: E402

transcribe.print = lambda *a, **k: None
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


tmp = Path(tempfile.mkdtemp())
transcribe.TRANSCRIPTS_DIR = tmp / "transcripts"
transcribe.WAVES_DIR = tmp / "waves"
refine.DRAFTS_DIR = tmp / "refine"
media = tmp / "clip.mov"
# 12 s beeld met twee mono audiosporen: spoor 1 een toon, spoor 2 stil.
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=12",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
                "-map", "0:v", "-map", "1:a", "-map", "2:a", "-t", "12", "-c:v", "mpeg4", "-c:a", "pcm_s16le", str(media)],
               check=True)

pieces = [(0, [{"word": " Hallo", "start": 1.0, "end": 1.5, "probability": 0.9},
               {"word": " daar", "start": 1.6, "end": 2.0, "probability": 0.3}]),
          (1, [{"word": " Ja", "start": 5.0, "end": 5.4, "probability": 0.95}])]
doc = model.from_pieces(pieces, "nl", "Spreker",
                        tracks=[model.Track(1, "eigen microfoon", True, -12.0), model.Track(2, "stil", False, -90.0)],
                        source={"connector": "iconik", "asset_id": "A1", "title": "Proef", "media": str(media)})
transcribe.TRANSCRIPTS_DIR.mkdir(parents=True)
(transcribe.TRANSCRIPTS_DIR / "A1.json").write_text(json.dumps(model.to_dict(doc)))

# --- openen ---
view = refine.view("A1")
check("view: titel, bron en bestand", view["title"] == "Proef" and view["origin"] == "mac" and view["media_name"] == "clip.mov", view)
check("view: duur en beeldsnelheid", abs(view["duration"] - 12) < 0.2 and view["fps"] == 25.0 and view["video"], view)
check("view: sporen met wat de analyse zag", view["tracks"] == [{"n": 1, "label": "eigen microfoon", "used": True},
                                                                 {"n": 2, "label": "stil", "used": False}], view["tracks"])
check("view: transcript in het eigen model", len(view["transcript"]["segments"]) == 2
      and view["transcript"]["speakers"][1]["name"] == "Spreker 2")

try:
    refine.view("BESTAAT-NIET")
    check("view: geen kopie en geen Iconik geeft een leesbare fout", False)
except refine.RefineError as error:
    check("view: geen kopie en geen Iconik geeft een leesbare fout", "Iconik" in str(error), error)

bad = model.from_dict(model.to_dict(doc))
bad.source["media"] = str(tmp / "weg.mov")
(transcribe.TRANSCRIPTS_DIR / "A2.json").write_text(json.dumps(model.to_dict(bad)))
try:
    refine.view("A2")
    check("view: origineel onbereikbaar geeft een leesbare fout", False)
except refine.RefineError as error:
    check("view: origineel onbereikbaar geeft een leesbare fout", "ISG" in str(error), error)

# --- stukjes voor de speler ---
mix = refine.chunk("A1", 3.0, 4)
check("chunk: fragmented mp4", mix[4:8] == b"ftyp" and b"moof" in mix, mix[:16])
probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,codec_type,width",
                                   "-of", "json", "-"], input=mix, capture_output=True).stdout)
kinds = {s["codec_type"]: s for s in probe.get("streams", [])}
check("chunk: MPEG-4 beeld en AAC geluid", kinds.get("video", {}).get("codec_name") == "mpeg4"
      and kinds.get("audio", {}).get("codec_name") == "aac", probe)
check("chunk: mix gebruikt alleen de gebruikte sporen", refine._audio_args(refine.open_session("A1"), 0)[1] == [1])
check("chunk: één spoor apart te horen", refine._audio_args(refine.open_session("A1"), 2)[1] == [2])
solo = refine.chunk("A1", 0, 2, solo=2)
check("chunk: met een gekozen spoor ook mp4", solo[4:8] == b"ftyp")
refine.open_session("A1")["used"] = [1, 2]
both = refine.chunk("A1", 0, 2)
check("chunk: mix van twee sporen (amix) werkt", both[4:8] == b"ftyp")
refine.open_session("A1")["used"] = [1]
end = refine.chunk("A1", 999, 6)
check("chunk: voorbij het eind geeft het laatste stukje", end[4:8] == b"ftyp")

# --- golfvormen ---
x = np.concatenate([np.zeros(16000, np.float32), np.full(16000, 0.5, np.float32)])
p = transcribe.wave_peaks(x)
check("wave_peaks: 50 per seconde", len(p) == 100, len(p))
check("wave_peaks: stil is 0, halve schaal rond -6 dB", p[10] == 0 and 225 <= p[90] <= 232, (p[10], p[90]))
check("wave_peaks: int16 geeft hetzelfde", transcribe.wave_peaks((x * 32767).astype(np.int16))[90] == p[90])

body, fraction = refine.waves("A1")
check("waves: eerst uitrekenen, met voortgang", body is None and fraction is not None)
for _ in range(100):
    body, fraction = refine.waves("A1")
    if body is not None:
        break
    time.sleep(0.1)
peaks = transcribe.read_waves(transcribe.WAVES_DIR / "A1.waves")
check("waves: bestand met beide sporen", peaks is not None and sorted(peaks) == [1, 2], peaks and sorted(peaks))
if peaks:
    check("waves: lengte klopt met de duur", abs(len(peaks[1]) - 600) <= 3, len(peaks[1]))
    check("waves: toon hoog, stil spoor laag", np.frombuffer(peaks[1], np.uint8)[50:500].min() > 150
          and np.frombuffer(peaks[2], np.uint8).max() < 10)
check("waves: daarna direct uit het bestand", refine.waves("A1")[0] == body)

# --- golfvormen tijdens een opdracht ---
transcribe._local.peaks = {}
transcribe.track_info(3, x, "", True)
check("opdracht: track_info bewaart de golfvorm", transcribe._local.peaks.get(3) == p)
transcribe._local.peaks = None
transcribe.save_waves("B/1", {1: p})
check("opdracht: save_waves met veilige naam", transcribe.read_waves(transcribe.WAVES_DIR / "B_1.waves") == {1: p})

# --- concept ---
check("concept: eerst geen", refine.view("A1")["draft"] is None)
edited = model.to_dict(doc)
edited["speakers"][0]["name"] = "Interviewer"
edited["segments"][0]["words"][0] = ["Hoi", 1000, 1500, 1.0]
edited["source"] = {"connector": "ander", "media": "/elders"}      # het scherm bepaalt de bron niet
saved = refine.save_draft("A1", json.dumps({"transcript": edited, "edits": 3}))
draft = refine.view("A1")["draft"]
check("concept: bewaard met aantal en tijd", draft and draft["edits"] == 3 and draft["saved_at"] == saved, draft and draft["edits"])
check("concept: wijzigingen erin", draft["transcript"]["speakers"][0]["name"] == "Interviewer"
      and draft["transcript"]["segments"][0]["words"][0][0] == "Hoi")
check("concept: bron en sporen van het origineel", draft["transcript"]["source"]["media"] == str(media)
      and len(draft["transcript"]["tracks"]) == 2, draft["transcript"]["source"])
for bad_body in ("geen json", json.dumps({"transcript": {"format": "iets anders"}}), json.dumps({"edits": 1})):
    try:
        refine.save_draft("A1", bad_body)
        check("concept: onzin wordt geweigerd", False, bad_body)
    except (ValueError, KeyError, TypeError):
        check("concept: onzin wordt geweigerd", refine.view("A1")["draft"]["edits"] == 3)
(refine.DRAFTS_DIR / "A1.draft.json").write_text("{kapot")
check("concept: kapot bestand telt als geen concept", refine.view("A1")["draft"] is None)
refine.save_draft("A1", json.dumps({"transcript": edited, "edits": 1}))
refine.discard_draft("A1")
refine.discard_draft("A1")
check("concept: weggooien (ook twee keer)", refine.view("A1")["draft"] is None)

# --- opslaan in Iconik en origineel terugzetten (nagebootste Iconik) ---
class FakeIconik:
    def __init__(self):
        self.transcriptions, self.segments, self.calls, self.next = [], [], [], 1

    def get(self, path):
        self.calls.append(("GET", path))
        if path == "assets/v1/assets/A1/":
            return {"id": "A1", "title": "Proef", "versions": [{"id": "V", "status": "ACTIVE"}]}
        if path.endswith("/transcriptions/properties/"):
            return {"objects": [dict(t) for t in self.transcriptions]}
        if "/segments/?" in path:
            q = dict(x.split("=", 1) for x in path.split("?", 1)[1].split("&"))
            rows = [x for x in self.segments if x["transcription_id"] == q["transcription_id"]]
            size, page = int(q["per_page"]), int(q["page"])
            return {"objects": rows[(page - 1) * size: page * size]}
        raise AssertionError(path)

    def post(self, path, body):
        self.calls.append(("POST", path))
        if path.endswith("/transcriptions/properties/"):
            tid = f"T{self.next}"; self.next += 1
            self.transcriptions.append(dict(body, id=tid)); return {"id": tid}
        if path.endswith("/segments/bulk/"):
            self.segments += body["objects"]; return {}
        if path.endswith("/reindex/"):
            return {}
        raise AssertionError(path)


def fake_delete(client, asset_id, version_id, tid):
    client.calls.append(("DELETE", tid))
    client.transcriptions = [t for t in client.transcriptions if t["id"] != tid]
    before = len(client.segments)
    client.segments = [x for x in client.segments if x["transcription_id"] != tid]
    return before - len(client.segments)


import iconik  # noqa: E402
transcribe.delete_transcription = fake_delete
transcribe.find_version = lambda asset: "V"
fake = FakeIconik()
transcribe.make_client = lambda: fake
# Het Whisper-transcript staat in Iconik, zoals na een opdracht.
connector = iconik.IconikConnector(fake)
info = connector.open("A1", push=True)
whisper_id, _ = connector.write(info, doc, {"type": "EXTERNAL", "name": transcribe.ENGINE_NAME, "model": "m", "version": "1"})
try:
    refine.save_to_iconik("A1")
    check("opslaan: zonder concept geweigerd", False)
except refine.RefineError:
    check("opslaan: zonder concept geweigerd", True)
refine.save_draft("A1", json.dumps({"transcript": edited, "edits": 2}))
result = refine.save_to_iconik("A1")
calls_after_save = list(fake.calls)
names = [(t.get("engine_info") or {}).get("name") for t in fake.transcriptions]
check("opslaan: alleen de correctie staat in Iconik", names == [transcribe.REFINED_ENGINE], names)
back = connector.read(connector.open("A1", push=False), transcribe.REFINED_ENGINE)
check("opslaan: met de wijzigingen", back.speakers[0].name == "Interviewer" and back.segments[0].words[0].text == "Hoi"
      and back.segments[0].words[0].score == 1.0)
check("opslaan: zoekindex bijgewerkt", calls_after_save[-1] == ("POST", "assets/v1/assets/A1/reindex/"), calls_after_save[-1])
check("opslaan: origineel op deze Mac", refine.view("A1")["original_kept"]
      and json.loads((refine.DRAFTS_DIR / "A1.original.json").read_text())["transcript"]["segments"][0]["words"][0][0] == "Hallo")
v2 = refine.view("A1")
check("opslaan: concept weg, kopie is de correctie", v2["draft"] is None and v2["refined"]
      and v2["transcript"]["speakers"][0]["name"] == "Interviewer")
check("voorkeuren: één origineel bewaard", refine.originals_info()["count"] == 1)

# Een gewone transcriptie mag de correctie niet overschrijven.
try:
    connector.open("A1", push=True, replace=True)
    check("transcriberen: gecorrigeerd asset geweigerd", False)
except transcribe.Problem as problem:
    check("transcriberen: gecorrigeerd asset geweigerd", getattr(problem, "skip", False) and "Refine" in str(problem), problem)

# Tweede correctie: vervangt de eerste, het origineel blijft het Whisper-transcript.
edited["segments"][0]["words"][0] = ["Hallo!", 1000, 1500, 1.0]
refine.save_draft("A1", json.dumps({"transcript": edited, "edits": 1}))
refine.save_to_iconik("A1")
check("tweede correctie: nog steeds één transcript", len(fake.transcriptions) == 1
      and fake.transcriptions[0]["engine_info"]["name"] == transcribe.REFINED_ENGINE)
check("tweede correctie: origineel ongewijzigd",
      json.loads((refine.DRAFTS_DIR / "A1.original.json").read_text())["transcript"]["segments"][0]["words"][0][0] == "Hallo")

# Origineel terugzetten.
refine.restore_original("A1")
check("terugzetten: Whisper-transcript weer in Iconik", [t["engine_info"]["name"] for t in fake.transcriptions] == [transcribe.ENGINE_NAME])
restored = connector.read(connector.open("A1", push=False))
check("terugzetten: met de oorspronkelijke tekst", restored.segments[0].words[0].text == "Hallo" and restored.speakers[0].name == "Spreker 1")
check("terugzetten: origineel van de Mac weg, niet meer gecorrigeerd", not refine.view("A1")["original_kept"] and not refine.view("A1")["refined"])
check("terugzetten: kopie op de Mac weer met bestand en sporen", refine.view("A1")["media_name"] == "clip.mov"
      and len(refine.view("A1")["tracks"]) == 2 and refine.view("A1")["tracks"][0]["label"] == "eigen microfoon")
check("transcriberen: daarna weer toegestaan", connector.open("A1", push=True, replace=True)["refined"] == [])

# Lokale originelen legen.
refine.save_draft("A1", json.dumps({"transcript": edited, "edits": 1}))
refine.save_to_iconik("A1")
refine.clear_originals()
check("legen: geen originelen meer", refine.originals_info() == {"count": 0, "bytes": 0})
try:
    refine.restore_original("A1")
    check("legen: terugzetten kan dan niet meer", False)
except refine.RefineError:
    check("legen: terugzetten kan dan niet meer", True)

# Een transcript uit een andere bron blijft altijd staan.
fake.transcriptions.append({"id": "X", "engine_info": {"name": "iets-anders"}})
refine.save_draft("A1", json.dumps({"transcript": edited, "edits": 1}))
try:
    refine.save_to_iconik("A1")
    check("andere bron: opslaan geweigerd", False)
except transcribe.Problem:
    check("andere bron: opslaan geweigerd", any(t["id"] == "X" for t in fake.transcriptions) and refine.view("A1")["draft"] is not None)

# --- labels in de opdrachtenlijst ---
fake.transcriptions = [t for t in fake.transcriptions if t["id"] != "X"]
check("labels: correctie in Iconik", refine.marks("A1")["refined"], refine.marks("A1"))
refine.discard_draft("A1")
check("labels: gecorrigeerd zonder concept", refine.marks("A1") == {"refined": True, "draft": False}, refine.marks("A1"))
refine.save_draft("A1", json.dumps({"transcript": edited, "edits": 1}))
check("labels: gecorrigeerd met concept", refine.marks("A1") == {"refined": True, "draft": True}, refine.marks("A1"))
check("labels: onbekend asset", refine.marks("NIETS") == {"refined": False, "draft": False})

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
