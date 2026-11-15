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
transcribe.CONFIG_PATH = tmp / "config.json"   # de export onthoudt zijn regels in de voorkeuren
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
# Origineel onbereikbaar (ISG niet gekoppeld): openen lukt, alleen tekst.
off = refine.view("A2")
check("offline: openen lukt zonder origineel", off["offline"] and not off["video"], off)
check("offline: duur uit het transcript, fps 25", abs(off["duration"] - 5.4) < 0.01 and off["fps"] == 25.0, off)
check("offline: sporen uit het transcript", [t["n"] for t in off["tracks"]] == [1, 2] and off["tracks"][0]["used"], off["tracks"])
check("offline: concept werkt", refine.save_draft("A2", json.dumps({"transcript": model.to_dict(bad), "edits": 1}))
      and refine.view("A2")["draft"]["edits"] == 1)
refine.discard_draft("A2")
for name, call in (("chunk", lambda: refine.chunk("A2", 0)), ("waves", lambda: refine.waves("A2"))):
    try:
        call()
        check(f"offline: {name} geeft een leesbare fout", False)
    except refine.RefineError as error:
        check(f"offline: {name} geeft een leesbare fout", "ISG" in str(error), error)
check("offline: geen golfvormen gestart", "A2" not in refine._wave_jobs)
check("view: online niet offline", refine.view("A1")["offline"] is False)

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

# Daarna de opslag weg: duur, fps en sporen uit wat Refine eerder zag, golfvormen uit de cache.
away = media.with_name("clip-weg.mov")
media.rename(away)
try:
    off = refine.view("A1")
    check("offline na eerder openen: duur en fps van het origineel", off["offline"] and abs(off["duration"] - 12) < 0.2
          and off["fps"] == 25.0, off)
    check("offline na eerder openen: golfvormen uit de cache", refine.waves("A1")[0] == body)
finally:
    away.rename(media)
check("weer online na terugkoppelen", refine.view("A1")["offline"] is False)

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
check("labels: gecorrigeerd zonder concept", refine.marks("A1") == {"refined": True, "draft": False, "subtitles": False}, refine.marks("A1"))
refine.save_draft("A1", json.dumps({"transcript": edited, "edits": 1}))
check("labels: gecorrigeerd met concept", refine.marks("A1") == {"refined": True, "draft": True, "subtitles": False}, refine.marks("A1"))
check("labels: onbekend asset", refine.marks("NIETS") == {"refined": False, "draft": False, "subtitles": False})

# --- ondertitels: bewust maken, bewaren als bestand, bewerken, inpassen ---
transcribe.SIDECARS_FILE = tmp / "sidecars.json"
asked = []


def chooser(answer):
    def choose(folder, name):
        asked.append((folder, name))
        return answer
    return choose


def body(**values):
    return json.dumps(values)


settings = refine.view("A1")["export"]
check("ondertitels: begint met de voorkeuze voor streaming, .srt naast de media",
      settings["preset"] == "streaming" and settings["rules"]["max_chars"] == 42 and settings["rules"]["linger"] == 1
      and settings["formats"] == "srt" and settings["place"] == "media", settings)
check("ondertitels: er zijn er geen tot iemand ze maakt", refine.view("A1")["subtitles"] is None and refine.load_subtitles("A1") is None)
seen = refine.preview_subtitles("A1", body(rules={}))
check("ondertitels: de voorvertoning telt zonder iets te bewaren",
      seen["cues"] >= 1 and "without_linger" in seen and refine.load_subtitles("A1") is None and not (media.parent / "clip.srt").exists(), seen)
for call, what in ((lambda: refine.save_subtitles("A1", body(transcript={}, edits=1)), "bewaren"),
                   (lambda: refine.write_subtitles("A1"), "schrijven"), (lambda: refine.fit_subtitles("A1", body()), "inpassen")):
    try:
        call()
        check(f"ondertitels: {what} zonder ondertitels geweigerd", False)
    except refine.RefineError:
        check(f"ondertitels: {what} zonder ondertitels geweigerd", True)

made = refine.make_subtitles("A1", body(rules={"max_chars": 30}, formats="srt", place="media"), chooser("x"))
srt_path = media.parent / "clip.srt"
check("maken: de laag, met de regels, niets met de hand aangepast en nog niet verouderd",
      made["rules"]["max_chars"] == 30 and made["edits"] == 0 and made["stale"] is False and not any(made["touched"])
      and len(made["touched"]) == len(made["transcript"]["segments"]) >= 1, made.get("rules"))
check("maken: het bestand staat naast de media, zonder bewaarvenster",
      [(f["name"], f["state"]) for f in made["files"]] == [("clip.srt", "written")] and srt_path.read_text(encoding="utf-8").startswith("1\n00:00:")
      and asked == [] and made["target"] == {"formats": "srt", "place": "media", "base": str(media), "names": ["clip.srt"]}, made["files"])
check("maken: uit het concept zoals het in Refine staat, zonder sprekernamen in .srt", "Spreker" not in srt_path.read_text(encoding="utf-8"))
check("maken: de keuzes zijn onthouden voor de volgende keer",
      refine.subtitle_settings()["rules"]["max_chars"] == 30 and refine.subtitle_settings()["formats"] == "srt")
check("maken: de opdrachtenlijst toont dat er ondertitels zijn", refine.marks("A1")["subtitles"] is True)
check("maken: de laag komt terug bij het openen", refine.view("A1")["subtitles"]["target"]["names"] == ["clip.srt"])

hand = json.loads(json.dumps(made["transcript"]))
hand["segments"][0]["words"] = [["Ingekort", hand["segments"][0]["start"], hand["segments"][0]["end"], 1]]
refine.save_subtitles("A1", body(transcript=hand, touched=[True] + [False] * (len(hand["segments"]) - 1), edits=1, rules=made["rules"]))
kept = refine.load_subtitles("A1")
check("bewerken: de laag is bewaard op deze Mac, het bestand nog niet bijgewerkt",
      kept["edits"] == 1 and kept["touched"][0] is True and kept["target"]["base"] == str(media) and "Ingekort" not in srt_path.read_text(encoding="utf-8"), kept["edits"])
wrote = refine.write_subtitles("A1")
check("bewaren: het bestand bijgewerkt, niets meer te bewaren",
      wrote["files"][0]["state"] == "written" and "Ingekort" in srt_path.read_text(encoding="utf-8") and wrote["edits"] == 0, wrote["files"])
try:
    refine.save_subtitles("A1", body(transcript={"segments": "onzin"}, edits=1))
    check("bewerken: onzin wordt niet bewaard", False)
except (ValueError, KeyError, TypeError, AttributeError):
    check("bewerken: onzin wordt niet bewaard", refine.load_subtitles("A1")["edits"] == 0)

fitted = refine.fit_subtitles("A1", body(rules=dict(made["rules"], linger=0)))
check("inpassen: wat met de hand is aangepast blijft staan",
      fitted["touched"][0] is True and fitted["transcript"]["segments"][0]["words"][0][0] == "Ingekort" and fitted["rules"]["linger"] == 0, fitted["touched"])

srt_path.write_text("door iemand anders bewerkt", encoding="utf-8")
refine.save_subtitles("A1", body(transcript=fitted["transcript"], touched=fitted["touched"], edits=2, rules=fitted["rules"]))
blocked = refine.write_subtitles("A1")
check("bewaren: een bestand dat daarna door iemand is gewijzigd blijft staan, de wijzigingen blijven open",
      (blocked["files"][0]["state"], blocked["files"][0]["detail"]) == ("kept", "exists") and blocked["edits"] == 2
      and srt_path.read_text(encoding="utf-8") == "door iemand anders bewerkt", blocked["files"])
(tmp / "uit").mkdir(exist_ok=True)
check("andere plek: annuleren in het bewaarvenster verandert niets",
      refine.write_subtitles("A1", chooser(None), elsewhere=True) == {"cancelled": True} and refine.load_subtitles("A1")["target"]["place"] == "media")
moved = refine.write_subtitles("A1", chooser(str(tmp / "uit" / "elders")), elsewhere=True)
check("andere plek: bewaarvenster opent naast het origineel met de naam van de media", asked[-1] == (str(media.parent), "clip.srt"), asked[-1])
check("andere plek: geschreven waar iemand koos, de extensie aangevuld, en dat is voortaan de plek",
      moved["files"][0]["name"] == "elders.srt" and (tmp / "uit" / "elders.srt").is_file() and moved["target"]["place"] == "other"
      and moved["target"]["names"] == ["elders.srt"] and moved["edits"] == 0, moved["target"])

changed = json.loads(json.dumps(refine.load_draft("A1")["transcript"]))
changed["segments"][0]["words"][0][0] = "Anders"
refine.save_draft("A1", json.dumps({"transcript": changed, "edits": 2}))
check("verouderd zodra het transcript verandert, de ondertitels zelf blijven staan",
      refine.load_subtitles("A1")["stale"] is True and refine.load_subtitles("A1")["transcript"]["segments"][0]["words"][0][0] == "Ingekort")
check("opnieuw maken: annuleren in het bewaarvenster laat de oude laag staan",
      refine.make_subtitles("A1", body(rules={}, formats="both", place="other"), chooser(None)) == {"cancelled": True}
      and refine.load_subtitles("A1")["touched"][0] is True)
again = refine.make_subtitles("A1", body(rules={}, formats="both", place="other"), chooser(str(tmp / "uit" / "nieuw.vtt")))
check("opnieuw maken: vers verdeeld uit het transcript van nu, het handwerk is weg",
      again["stale"] is False and not any(again["touched"]) and "Anders" in (tmp / "uit" / "nieuw.srt").read_text(encoding="utf-8"), again["files"])
check("opnieuw maken: allebei de formaten op de gekozen plek, .vtt met sprekers",
      [(f["name"], f["state"]) for f in again["files"]] == [("nieuw.srt", "written"), ("nieuw.vtt", "written")]
      and "<v " in (tmp / "uit" / "nieuw.vtt").read_text(encoding="utf-8"), again["files"])
try:
    refine.make_subtitles("A1", body(rules={}, formats="srt", place="other"), None)
    check("maken: een andere plek zonder venster geeft een leesbare fout", False)
except refine.RefineError:
    check("maken: een andere plek zonder venster geeft een leesbare fout", True)
try:
    refine.make_subtitles("A2", body(rules={}, formats="srt", place="media"), chooser("x"))
    check("maken: naast de media kan niet zonder origineel", False)
except refine.RefineError as error:
    check("maken: naast de media kan niet zonder origineel", refine.load_subtitles("A2") is None, str(error))
away = refine.make_subtitles("A2", body(rules={}, formats="srt", place="other"), chooser(str(tmp / "uit" / "zonder-origineel.srt")))
check("maken: zonder origineel wel op een andere plek, het bewaarvenster zonder map",
      asked[-1][0] is None and asked[-1][1].endswith(".srt") and away["files"][0]["state"] == "written", asked[-1])
refine.discard_subtitles("A1")
# --- een bestaand bestand inladen ---
srt_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHallo daar\n\n2\n00:00:05,000 --> 00:00:06,500\nJa, door iemand anders gemaakt.\n", encoding="utf-8")
check("inladen: wat naast de media staat wordt aangeboden", refine.view("A1")["export"]["existing"] == ["clip.srt"], refine.view("A1")["export"]["existing"])
loaded = refine.import_subtitles("A1", body(rules={}, name="clip.srt"), None)
check("inladen: de ondertitels uit het bestand, als met de hand gemaakt, met de sprekers uit het transcript",
      [" ".join(w[0] for w in s["words"]) for s in loaded["transcript"]["segments"]] == ["Hallo daar", "Ja, door iemand anders gemaakt."]
      and all(loaded["touched"]) and [s["speaker"] for s in loaded["transcript"]["segments"]] == ["1", "2"]
      and loaded["files"][0]["state"] == "loaded" and loaded["target"] == {"formats": "srt", "place": "media", "base": str(media), "names": ["clip.srt"]}, loaded["target"])
shorter = json.loads(json.dumps(loaded["transcript"]))
shorter["segments"][1]["words"] = shorter["segments"][1]["words"][:1]
refine.save_subtitles("A1", body(transcript=shorter, touched=loaded["touched"], edits=1, rules=loaded["rules"]))
check("inladen: Bewaren schrijft terug naar het bestand dat is ingeladen",
      refine.write_subtitles("A1")["files"][0]["state"] == "written" and "iemand anders" not in srt_path.read_text(encoding="utf-8") and "Hallo daar" in srt_path.read_text(encoding="utf-8"))
check("inladen: inpassen blijft van ingeladen ondertitels af", refine.fit_subtitles("A1", body(rules={}))["changed"] is False)
for name in ("../geheim.srt", "clip.mov", "bestaat-niet.vtt"):
    try:
        refine.import_subtitles("A1", body(name=name), None)
        check(f"inladen: {name} geweigerd", False)
    except refine.RefineError:
        check(f"inladen: {name} geweigerd", True)
picked = tmp / "uit" / "los bestand.vtt"
picked.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Spreker 2>Los bestand\n", encoding="utf-8")
opened = []
other_file = refine.import_subtitles("A1", body(rules={}), lambda folder: (opened.append(folder), str(picked))[1])
check("inladen: een ander bestand kiezen opent naast het origineel, en dat bestand is daarna de plek",
      opened == [str(media.parent)] and other_file["target"]["place"] == "other" and other_file["target"]["names"] == ["los bestand.vtt"]
      and other_file["transcript"]["segments"][0]["speaker"] == "2", other_file["target"])
check("inladen: annuleren bij het kiezen verandert niets",
      refine.import_subtitles("A1", body(rules={}), lambda folder: None) == {"cancelled": True} and refine.load_subtitles("A1")["target"]["names"] == ["los bestand.vtt"])
(tmp / "uit" / "leeg.srt").write_text("geen ondertitels hier", encoding="utf-8")
(tmp / "uit" / "plaatje.png").write_bytes(b"png")
for bad, what in ((tmp / "uit" / "leeg.srt", "een bestand zonder ondertitels"), (tmp / "uit" / "plaatje.png", "een ander soort bestand"), (tmp / "uit" / "weg.srt", "een bestand dat er niet is")):
    try:
        refine.import_subtitles("A1", body(rules={}), lambda folder, bad=bad: str(bad))
        check(f"inladen: {what} geweigerd", False)
    except refine.RefineError:
        check(f"inladen: {what} geweigerd, de ondertitels blijven", refine.load_subtitles("A1")["target"]["names"] == ["los bestand.vtt"])
try:
    refine.import_subtitles("A1", body(rules={}), None)
    check("inladen: kiezen zonder venster geeft een leesbare fout", False)
except refine.RefineError:
    check("inladen: kiezen zonder venster geeft een leesbare fout", True)
refine.discard_subtitles("A1")
check("verwijderen: het label in de opdrachtenlijst is weg", refine.marks("A1")["subtitles"] is False)
check("verwijderen: de laag weg, de bestanden blijven staan",
      refine.load_subtitles("A1") is None and (tmp / "uit" / "nieuw.srt").is_file() and refine.view("A1")["subtitles"] is None)
refine.save_draft("A1", json.dumps({"transcript": edited, "edits": 1}))   # terug naar het concept van hierboven

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
