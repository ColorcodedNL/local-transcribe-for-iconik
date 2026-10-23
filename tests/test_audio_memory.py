"""Tests voor het zuinige lezen en analyseren van audiosporen (0.3.6).

read_pcm moet byte voor byte hetzelfde geven als de oude subprocess.run(capture_output=True),
ook bij een onbekende of te krappe duur. analyse_tracks zet sporen pas om als ze nodig zijn;
de keuzes moeten hetzelfde blijven. Het lezen wordt overgeslagen als er geen ffmpeg is.

Draaien vanuit de projectmap:
    uv run --no-project --with pynacl --with requests --with numpy python -B tests/test_audio_memory.py
"""
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, "app")
for name in ("keyring", "mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
import transcribe  # noqa: E402

transcribe.tell = lambda *a, **k: None
transcribe.print = lambda *a, **k: None
results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(("OK    " if ok else "FOUT  ") + label + (f"  ({detail})" if detail and not ok else ""))


SR = transcribe.SAMPLE_RATE


def same(a, b):
    """Gelijk op hooguit 1 na (de kleinste stap van 16-bit). ffmpeg op macOS zet bij het omzetten naar
    16 kHz een beetje willekeurige ruis (dither): twee keer dezelfde opdracht verschilt daar al op 1."""
    return a.shape == b.shape and (not a.size or int(np.abs(a.astype(np.int64) - b.astype(np.int64)).max()) <= 1)
rng = np.random.default_rng(3)


def speech(n, on=0.5):
    env = np.repeat((rng.random(n // 1600 + 1) < on).astype(float), 1600)[:n]
    return rng.normal(0, 0.2, n) * env


def to16(cols):
    return np.clip(np.stack(cols, 1) * 32767, -32768, 32767).astype(np.int16)


# --- Lezen met ffmpeg -------------------------------------------------------------------------

if shutil.which("ffmpeg") and shutil.which("ffprobe"):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "sporen.mkv"
        # Drie audiostromen: stereo, mono, stereo (vijf sporen), 70 seconden.
        subprocess.run(["ffmpeg", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=70",
                        "-f", "lavfi", "-i", "anoisesrc=d=70:a=0.1:seed=5",
                        "-f", "lavfi", "-i", "sine=frequency=1000:duration=70",
                        "-filter_complex", "[0:a][1:a]amerge=inputs=2[s1];[2:a][1:a]amerge=inputs=2[s3]",
                        "-map", "[s1]", "-map", "1:a", "-map", "[s3]", "-c:a", "pcm_s16le", str(path)], check=True)
        tracks = transcribe.list_tracks(path)
        check("vijf sporen in drie stromen", len(tracks) == 5, tracks)

        def old_read_all(p, tr):
            streams = sorted({t[0] for t in tr})
            inputs = "".join(f"[0:a:{i}]" for i in streams)
            command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(p), "-vn", "-filter_complex",
                       f"{inputs}amerge=inputs={len(streams)}[all]", "-map", "[all]",
                       "-ar", str(SR), "-f", "s16le", "-"]
            data = np.frombuffer(subprocess.run(command, capture_output=True).stdout, np.int16)
            return data[: len(data) // len(tr) * len(tr)].reshape(-1, len(tr))

        reference = old_read_all(path, tracks)
        check("duur via ffprobe", abs((transcribe.media_seconds(path) or 0) - 70) < 0.1)
        new = transcribe.read_all_tracks(path, tracks)
        check("alle sporen: gelijk aan de oude manier", same(new, reference),
              f"{new.shape} tegen {reference.shape}")

        # Onbekende duur en te kleine blokken: de tabel moet groeien zonder iets te verliezen.
        saved_seconds, saved_block = transcribe.media_seconds, transcribe.PCM_BLOCK
        transcribe.media_seconds = lambda p: None
        transcribe.PCM_BLOCK = 1_000_000
        new = transcribe.read_all_tracks(path, tracks)
        check("onbekende duur, kleine blokken: gelijk", same(new, reference))
        transcribe.media_seconds = lambda p: 3.0   # veel te krap geschat
        new = transcribe.read_all_tracks(path, tracks)
        check("te krappe schatting: gelijk", same(new, reference))
        transcribe.media_seconds, transcribe.PCM_BLOCK = saved_seconds, saved_block

        old_one = np.frombuffer(subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-map", "0:a:1",
             "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"], capture_output=True).stdout, np.int16)
        one = transcribe.read_track(path, tracks[2])
        check("één spoor: gelijk aan de oude manier",
              one.dtype == np.float32 and same(np.round(one * 32768.0).astype(np.int16), old_one))
        part = transcribe.read_track(path, tracks[2], seconds=10)
        check("één spoor met seconden: 10 s", len(part) == 10 * SR, len(part))

        broken = Path(folder) / "kapot.mov"
        broken.write_bytes(b"geen video")
        try:
            transcribe.read_all_tracks(broken, tracks)
            check("kapot bestand geeft een fout", False)
        except transcribe.Problem as error:
            check("kapot bestand geeft een fout met de melding van ffmpeg", "ffmpeg" in str(error), error)
else:
    print("OVERGESLAGEN  lezen met ffmpeg (geen ffmpeg op dit systeem)")

# --- Analyseren -------------------------------------------------------------------------------

n = SR * 60
a, b = speech(n), speech(n)
silence = np.zeros(n)

samples, mics = transcribe.analyse_tracks(to16([a + .1 * b, b + .1 * a]))
check("eigen microfoons: beide sporen, met niveaus", mics is not None and mics["tracks"] == [1, 2])
check("eigen microfoons: mix is float32 van de volle lengte", samples.dtype == np.float32 and len(samples) == n)

shown = []
transcribe.tell = lambda kind, **d: shown.append(d) if kind == "tracks" else None
samples, mics = transcribe.analyse_tracks(to16([a, a * .5, silence, b * .3 + a * .05]))
transcribe.tell = lambda *x, **k: None
notes = [t["note"] for t in shown[0]["tracks"]]
check("dubbel opgenomen spoor herkend", notes[1] == "zelfde signaal als spoor 1", notes)
check("stil spoor herkend", notes[2] == "stil", notes)
check("venster krijgt alle sporen met niveauverloop", len(shown[0]["tracks"]) == 4
      and all(len(t["env"]) == 160 for t in shown[0]["tracks"]))

samples, mics = transcribe.analyse_tracks(to16([a, a * .98 + rng.normal(0, .001, n)]), mix_same_source=True)
check("stereo met dezelfde bron wordt gemixt", mics is None and samples.dtype == np.float32
      and np.allclose(samples, ((to16([a])[:, 0] / 32768.0) * 1.98 / 2), atol=0.01))

samples, mics = transcribe.analyse_tracks(to16([silence, a * .4]))
check("een bruikbaar spoor: dat spoor", mics is None
      and np.array_equal(samples, to16([a * .4])[:, 0].astype(np.float32) / 32768.0))

try:
    transcribe.analyse_tracks(to16([silence, silence]))
    check("alleen stilte geeft een fout", False)
except transcribe.Problem:
    check("alleen stilte geeft een overgeslagen opdracht", True)

# Geheugen: de analyse mag niet meer alle sporen tegelijk als float32 vasthouden.
import tracemalloc  # noqa: E402
big = to16([a, a, b, b, silence, silence, a + .1 * b, a + .1 * b])   # 8 sporen van een minuut, 7,7 MB
tracemalloc.start()
transcribe.analyse_tracks(big)
_, top = tracemalloc.get_traced_memory()
tracemalloc.stop()
all_floats = big.shape[0] * big.shape[1] * 4
check("analyse houdt niet alle sporen tegelijk als float32 vast", top < all_floats * 0.6,
      f"piek {top / 1e6:.1f} MB, alle sporen als float32 {all_floats / 1e6:.1f} MB")

print()
print(f"{sum(results)} van {len(results)} goed")
sys.exit(0 if all(results) else 1)
