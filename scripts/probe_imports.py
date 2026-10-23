"""Welke pakketten laadt een volledige job echt? Om de bundel te verkleinen zonder te gokken.

Laadt de app-modules zoals de app (zonder venster), draait de audiostap, Whisper, de sprekerherkenning
en de stemcontrole op de eerste minuten van een bestand, en zet de geladen modules af tegen de
pakketten in de omgeving. Geen Iconik, geen token.

Draaien: uv run scripts/probe_imports.py <pad naar bestand> [minuten, standaard 5]
"""
import importlib.metadata as md
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import transcribe  # noqa: E402
import serve  # noqa: E402,F401  (laadt wat de app bij de start laadt, behalve het venster)
import worker  # noqa: E402,F401
import resources  # noqa: E402,F401
import updater  # noqa: E402,F401

path = Path(sys.argv[1])
minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 5
transcribe.tell = lambda *a, **k: None

samples, microphones = transcribe.load_audio_tracks(path)
samples = samples[: int(minutes * 60 * transcribe.SAMPLE_RATE)]
model = transcribe.ensure_model("mlx-community/whisper-large-v3-mlx")
language, _ = transcribe.detect_language(samples, model)
import mlx_whisper  # noqa: E402
result = mlx_whisper.transcribe(samples, path_or_hf_repo=model, language=language, word_timestamps=True, verbose=None)
for folder in (Path("/Applications/Local Transcribe for Iconik.app/Contents/Resources/models/speaker-diarization-community-1"),):
    if (folder / "config.yaml").is_file():
        transcribe.LOCAL_SPEAKER_MODEL = folder
turns = transcribe.diarize(samples, None, None)
kept = [s for s in result["segments"] if not transcribe.is_hallucination(s, turns, language)]
pieces = [(transcribe.speaker_at(w[0]["start"], w[-1]["end"], turns), w)
          for s in kept for w in transcribe.split_sentences(s["words"])]
voiceprint = transcribe.make_voiceprint(samples)
transcribe.refine_speakers(pieces, turns, voiceprint)
transcribe.cluster_sentences(pieces, voiceprint)   # de route voor korte clips
print(f"\nGedaan: {len(kept)} segmenten, {len({s for _, _, s in turns})} sprekers\n")

# Geladen modules naar pakketten (distributies).
owners = md.packages_distributions()
loaded = {name.split(".")[0] for name, mod in list(sys.modules.items()) if isinstance(mod, types.ModuleType)}
used = {dist for top in loaded for dist in owners.get(top, [])}
site = Path(md.distribution("numpy").locate_file("")).resolve()


def size_mb(dist):
    total = 0
    for f in md.distribution(dist).files or []:
        p = Path(dist and md.distribution(dist).locate_file(f))
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total / 1e6


rows = []
for dist in sorted({d.metadata["Name"] for d in md.distributions()}, key=str.lower):
    rows.append((size_mb(dist), dist, dist in used or dist.replace("-", "_") in used))
print("Niet geladen (kandidaten om weg te laten), groot naar klein:")
for size, dist, is_used in sorted(rows, reverse=True):
    if not is_used and size >= 0.5:
        print(f"  {size:7.1f} MB  {dist}")
print("\nGeladen, groot naar klein:")
for size, dist, is_used in sorted(rows, reverse=True):
    if is_used and size >= 5:
        print(f"  {size:7.1f} MB  {dist}")
