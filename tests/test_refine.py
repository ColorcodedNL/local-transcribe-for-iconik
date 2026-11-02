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

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
