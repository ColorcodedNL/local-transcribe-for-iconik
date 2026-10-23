"""Geheugenmeting van de audiostap (en optioneel Whisper) op een lokaal bestand.

Geen Iconik, geen token. Meet het geheugen van dit proces zoals Activiteitenweergave het toont,
vier keer per seconde, en laat per functie zien hoeveel er tijdens die functie bij kwam.

Draaien: uv run scripts/probe_memory.py <pad naar bestand> [--whisper] [--speakers]
(--speakers doet ook --whisper: de stemcontrole werkt op de zinnen uit Whisper)
"""
import functools
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import transcribe  # noqa: E402

GB = 1e9
peak = [0]
stats = {}


def sample():
    while True:
        peak[0] = max(peak[0], transcribe._footprint())
        time.sleep(0.25)


def watch(name):
    """Vervangt transcribe.<name> door een versie die tijd en geheugen per aanroep bijhoudt."""
    original = getattr(transcribe, name)

    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        before = transcribe._footprint()
        outer = peak[0]
        peak[0] = before
        start = time.time()
        try:
            return original(*args, **kwargs)
        finally:
            top = max(peak[0], transcribe._footprint())
            s = stats.setdefault(name, {"calls": 0, "seconds": 0.0, "rise": 0, "top": 0})
            s["calls"] += 1
            s["seconds"] += time.time() - start
            s["rise"] = max(s["rise"], top - before)
            s["top"] = max(s["top"], top)
            peak[0] = max(outer, top)
    setattr(transcribe, name, wrapped)


def line(label):
    now = transcribe._footprint()
    print(f"{label:<28} nu {now / GB:5.1f} GB   piek {max(peak[0], now) / GB:5.1f} GB", flush=True)


if len(sys.argv) < 2 or not Path(sys.argv[1]).is_file():
    sys.exit("Gebruik: uv run scripts/probe_memory.py <pad naar bestand> [--whisper]")
path = Path(sys.argv[1])
transcribe.tell = lambda *a, **k: None   # geen venster
threading.Thread(target=sample, daemon=True).start()
for name in ("list_tracks", "read_track", "read_all_tracks", "analyse_tracks", "measure_track", "similarity"):
    watch(name)

print(f"Bestand: {path.name} ({path.stat().st_size / GB:.1f} GB)\n")
line("begin")
start = time.time()
samples, microphones = transcribe.load_audio_tracks(path)
print(f"\nAudio: {len(samples) / transcribe.SAMPLE_RATE / 60:.1f} min, {time.time() - start:.0f} s, "
      f"samples {samples.nbytes / GB:.2f} GB ({samples.dtype})")
line("na audiostap")
import gc  # noqa: E402
gc.collect()
line("na gc.collect")

print("\nPer functie (stijging = hoeveel er tijdens die functie bij kwam):")
for name, s in stats.items():
    print(f"  {name:<16} {s['calls']:>3}x  {s['seconds']:7.1f} s   stijging {s['rise'] / GB:5.2f} GB   "
          f"top {s['top'] / GB:5.1f} GB")

if "--whisper" in sys.argv or "--speakers" in sys.argv:
    import mlx.core as mx
    import mlx_whisper
    mx.set_cache_limit(int(2 * GB))
    model_path = transcribe.ensure_model("mlx-community/whisper-large-v3-mlx")
    peak[0] = transcribe._footprint()
    language, certainty = transcribe.detect_language(samples, model_path)
    line(f"taal ({language}, {certainty:.0%})")

    # Elke 30 s een tussenstand, zodat een kortstondige piek zichtbaar wordt.
    done = threading.Event()
    log = []

    def follow():
        while not done.wait(30):
            log.append((time.time() - t0, transcribe._footprint(), peak[0], mx.get_active_memory(),
                        mx.get_cache_memory(), mx.get_peak_memory()))
            peak[0] = transcribe._footprint()
    t0 = time.time()
    threading.Thread(target=follow, daemon=True).start()
    mx.reset_peak_memory()
    result = mlx_whisper.transcribe(samples, path_or_hf_repo=model_path, language=language,
                                    word_timestamps=True, verbose=False)
    done.set()
    print(f"\nWhisper: {time.time() - t0:.0f} s, {len(result.get('segments', []))} segmenten, "
          f"MLX piek {mx.get_peak_memory() / GB:.1f} GB")
    print("  tijd    nu     piek   MLX actief  cache  MLX piek tot dan")
    for t, now, top, active, cache, mlx_peak in log:
        print(f"  {t:5.0f}s {now / GB:5.1f}  {top / GB:5.1f}   {active / GB:5.1f}     {cache / GB:4.1f}   {mlx_peak / GB:5.1f}")
    line("na whisper")


if "--speakers" in sys.argv:
    import torch

    def metal():
        return torch.mps.driver_allocated_memory() / GB if torch.backends.mps.is_available() else 0.0

    # Het sprekermodel: de kopie in de geïnstalleerde app, anders in de build, anders Hugging Face.
    for folder in (transcribe.LOCAL_SPEAKER_MODEL,
                   Path("/Applications/Local Transcribe for Iconik.app/Contents/Resources/models/speaker-diarization-community-1"),
                   Path(__file__).resolve().parent.parent / "build/Local Transcribe for Iconik.app/Contents/Resources/models/speaker-diarization-community-1"):
        if (Path(folder) / "config.yaml").is_file():
            transcribe.LOCAL_SPEAKER_MODEL = Path(folder)
            break
    import os
    mx.clear_cache()
    peak[0] = transcribe._footprint()
    t0 = time.time()
    turns = transcribe.diarize(samples, os.environ.get("HF_TOKEN"), None)
    print(f"\nSprekers: {len(turns)} beurten, {len({s for _, _, s in turns})} sprekers, {time.time() - t0:.0f} s, "
          f"Metal totaal {metal():.1f} GB")
    line("na sprekers")

    kept = [s for s in result["segments"] if not transcribe.is_hallucination(s, turns, language)]
    pieces = [(transcribe.speaker_at(w[0]["start"], w[-1]["end"], turns), w)
              for s in kept for w in transcribe.split_sentences(s["words"])]

    # Tel de aanroepen van het stemmodel en volg het geheugen elke 10 s.
    calls = [0]
    voiceprint = transcribe.make_voiceprint(samples)

    def counted(start, end):
        calls[0] += 1
        return voiceprint(start, end)
    done, log = threading.Event(), []

    def follow():
        while not done.wait(10):
            log.append((time.time() - t0, transcribe._footprint(), peak[0], metal(), calls[0]))
            peak[0] = transcribe._footprint()
    peak[0] = transcribe._footprint()
    t0 = time.time()
    threading.Thread(target=follow, daemon=True).start()
    refined, note = transcribe.refine_speakers(pieces, turns, counted)
    done.set()
    print(f"\nStemcontrole: {note}, {calls[0]} keer het stemmodel voor {len(pieces)} zinnen, {time.time() - t0:.0f} s")
    print("  tijd    nu     piek   Metal  aanroepen")
    for t_, now, top, m, c in log:
        print(f"  {t_:5.0f}s {now / GB:5.1f}  {top / GB:5.1f}  {m:5.1f}  {c:>6}")
    line("na stemcontrole")
    import gc
    gc.collect()
    line("na gc.collect")
    if torch.backends.mps.is_available():
        torch.mps.empty_cache()
    line("na mps empty_cache")
