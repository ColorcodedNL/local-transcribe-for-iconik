"""Sprekers: per spoor (zenders), met pyannote, stemafdrukken per zin en het samenvoegen van groepen.
"""
import os
import sys
from pathlib import Path
import numpy as np

from base import SAMPLE_RATE, SUPPORT_DIR, print  # noqa: F401
from audio import MIC_MIN_MARGIN_DB  # noqa: F401


DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"
# Lokale kopie van het sprekermodel (CC-BY-4.0, pyannote). Staat die er, dan is
# er geen Hugging Face-account of token nodig.
_BUNDLE_RES = Path(os.environ.get("LOCALTRANSCRIBE_RES") or Path(__file__).resolve().parent.parent)  # ook als de code uit een update komt
_BUNDLED_SPEAKER_MODEL = _BUNDLE_RES / "models" / "speaker-diarization-community-1"
LOCAL_SPEAKER_MODEL = (_BUNDLED_SPEAKER_MODEL if (_BUNDLED_SPEAKER_MODEL / "config.yaml").is_file()
                       else SUPPORT_DIR / "models" / "speaker-diarization-community-1")


def speaker_model_available(token=None):
    return bool(token) or (LOCAL_SPEAKER_MODEL / "config.yaml").is_file()

# Stemcontrole per zin (zie refine_speakers).
VOICE_LONG_TURN = 3.0      # beurten vanaf deze lengte bepalen hoe een stem klinkt
VOICE_MAX_CROP = 10.0      # per beurt hooguit zoveel seconden gebruiken
VOICE_MIN_SECONDS = 10.0   # een stem telt mee met minstens zoveel seconden lange beurten
VOICE_MIN_SENTENCE = 0.3   # kortere zinnen niet beoordelen
VOICE_MIN_MARGIN = 0.10    # onder deze marge blijft de keuze van het sprekermodel staan

# Korte clips zonder lange beurten: zinnen onderling vergelijken (zie cluster_sentences).
CLUSTER_MIN_SENTENCE = 1.0  # alleen zinnen vanaf deze lengte vormen de groepen
CLUSTER_DISTANCE = 0.75     # groepen samenvoegen tot de gemiddelde afstand hierboven komt


def speaker_from_tracks(words, microphones, previous):
    """De microfoon die tijdens deze zin het luidst is. Bij twijfel de vorige spreker."""
    relative = microphones["relative"]
    first = int(words[0]["start"] * 10)
    last = max(int(np.ceil(words[-1]["end"] * 10)), first + 1)
    window = relative[:, first:last]
    if window.shape[1] == 0:
        return previous if previous is not None else f"spoor-{microphones['tracks'][0]}"
    mean = 10 * np.log10(np.maximum((10 ** (window / 10)).mean(axis=1), 1e-12))
    order = np.argsort(mean)[::-1]
    if previous is not None and mean[order[0]] - mean[order[1]] < MIC_MIN_MARGIN_DB:
        return previous
    return f"spoor-{microphones['tracks'][order[0]]}"


_pipeline = None  # het sprekermodel blijft geladen tussen opdrachten


def release_models():
    """Geeft het geheugen van de modellen vrij. Een volgende opdracht laadt ze opnieuw."""
    global _pipeline
    _pipeline = None
    holder = getattr(sys.modules.get("mlx_whisper.transcribe"), "ModelHolder", None)
    if holder is not None:
        holder.model = None
        holder.model_path = None
    import gc
    gc.collect()
    try:
        import mlx.core as mx
        mx.clear_cache()
    except Exception:
        pass
    empty_gpu_cache()


def diarize(samples, token, num_speakers):
    """Geeft een lijst (start, eind, spreker) in seconden."""
    global _pipeline
    import torch
    from pyannote.audio import Pipeline

    if _pipeline is None:
        if (LOCAL_SPEAKER_MODEL / "config.yaml").is_file():
            print("Sprekermodel: lokale kopie")
            _pipeline = Pipeline.from_pretrained(str(LOCAL_SPEAKER_MODEL))
        else:
            print("Sprekermodel: via Hugging Face")
            _pipeline = Pipeline.from_pretrained(DIARIZATION_MODEL, token=token)
    pipeline = _pipeline
    audio = {"waveform": torch.from_numpy(samples).unsqueeze(0), "sample_rate": SAMPLE_RATE}
    options = {"num_speakers": num_speakers} if num_speakers else {}
    try:
        pipeline.to(torch.device("mps"))
        output = pipeline(audio, **options)
    except Exception as error:  # MPS ondersteunt niet elke bewerking
        print(f"GPU lukte niet ({type(error).__name__}), opnieuw op CPU.")
        pipeline.to(torch.device("cpu"))
        output = pipeline(audio, **options)

    diarization = getattr(output, "exclusive_speaker_diarization", None)
    if diarization is None:
        diarization = getattr(output, "speaker_diarization", output)
    return [(turn.start, turn.end, speaker) for turn, speaker in diarization]


def speaker_at(start, end, turns):
    """De spreker met de meeste overlap, anders de dichtstbijzijnde beurt."""
    best, best_overlap = None, 0.0
    for turn_start, turn_end, speaker in turns:
        overlap = min(end, turn_end) - max(start, turn_start)
        if overlap > best_overlap:
            best, best_overlap = speaker, overlap
    if best is not None:
        return best
    middle = (start + end) / 2
    return min(turns, key=lambda t: min(abs(t[0] - middle), abs(t[1] - middle)))[2]


VOICE_CACHE_EVERY = 64  # de GPU-cache van torch legen na zoveel stemafdrukken


def empty_gpu_cache():
    """Geeft de buffers terug die torch op de GPU (MPS) vasthoudt voor hergebruik."""
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


def make_voiceprint(samples):
    """Geeft een functie (start, eind) -> stemafdruk, met het model uit diarize()."""
    import torch

    embedder = _pipeline._embedding
    minimum = int(getattr(embedder, "min_num_samples", 0) or 0)
    calls = [0]

    def voiceprint(start, end):
        # Elke zin heeft een andere lengte, en torch houdt op de GPU voor elke lengte nieuwe
        # buffers aan: bij 1452 zinnen groeide dat van 9,5 naar 14,1 GB. Af en toe legen.
        calls[0] += 1
        if calls[0] % VOICE_CACHE_EVERY == 0:
            empty_gpu_cache()
        a, b = int(start * SAMPLE_RATE), int(end * SAMPLE_RATE)
        if b - a < minimum:  # te kort voor het model: symmetrisch iets ruimer nemen
            extra = (minimum - (b - a)) // 2 + 1
            a, b = max(0, a - extra), min(len(samples), b + extra)
        wave = torch.from_numpy(samples[a:b].copy())[None, None, :]
        with torch.inference_mode():
            vector = np.asarray(embedder(wave))[0]
        norm = np.linalg.norm(vector)
        return None if not np.isfinite(norm) or norm == 0 else vector / norm

    return voiceprint


def refine_speakers(pieces, turns, voiceprint):
    """Controleert per zin de spreker aan de hand van de stem.

    Het sprekermodel is goed in lange beurten, maar rekent een korte reactie van
    de ander vaak bij degene die net sprak. Daarom: van de lange beurten leren hoe
    elke stem klinkt, en elke zin daarmee vergelijken. Geeft (stukken, aantal gewisseld).
    """
    by_voice, seconds = {}, {}
    for start, end, speaker in turns:
        if end - start >= VOICE_LONG_TURN:
            vector = voiceprint(start, min(end, start + VOICE_MAX_CROP))
            if vector is not None:
                by_voice.setdefault(speaker, []).append(vector)
                seconds[speaker] = seconds.get(speaker, 0.0) + end - start
    voices = {}
    for speaker, vectors in by_voice.items():
        if seconds[speaker] >= VOICE_MIN_SECONDS:
            mean = np.mean(vectors, axis=0)
            voices[speaker] = mean / np.linalg.norm(mean)
    if len(voices) < 2:
        # Te weinig lange beurten om stemmen van te leren: zinnen onderling vergelijken.
        return cluster_sentences(pieces, voiceprint)

    refined, changed = [], 0
    for speaker, words in pieces:
        start, end = words[0]["start"], words[-1]["end"]
        # Een spreker zonder eigen stemafdruk (te weinig materiaal) laten we met rust.
        if speaker in voices and end - start >= VOICE_MIN_SENTENCE:
            vector = voiceprint(start, end)
            if vector is not None:
                scores = sorted(((float(vector @ v), s) for s, v in voices.items()), reverse=True)
                best, margin = scores[0][1], scores[0][0] - scores[1][0]
                if best != speaker and margin >= VOICE_MIN_MARGIN:
                    speaker, changed = best, changed + 1
        refined.append((speaker, words))
    return refined, f"{changed} zinnen van spreker gewisseld"


def cluster_sentences(pieces, voiceprint):
    """Sprekers vinden door zinnen onderling te vergelijken, voor korte clips.

    Zinnen van een seconde of langer worden gegroepeerd op gelijkenis van de stem.
    Kortere zinnen sluiten aan bij de groep waar ze het meest op lijken.
    """
    from scipy.cluster.hierarchy import fcluster, linkage

    vectors = []
    for _, words in pieces:
        start, end = words[0]["start"], words[-1]["end"]
        vectors.append(voiceprint(start, end) if end - start >= VOICE_MIN_SENTENCE else None)
    core = [i for i, (_, words) in enumerate(pieces)
            if vectors[i] is not None
            and words[-1]["end"] - words[0]["start"] >= CLUSTER_MIN_SENTENCE]
    if len(core) < 2:
        return pieces, "te weinig zinnen om stemmen te vergelijken"

    tree = linkage(np.array([vectors[i] for i in core]), method="average", metric="cosine")
    labels = fcluster(tree, t=CLUSTER_DISTANCE, criterion="distance")
    groups = sorted(set(labels))
    if len(groups) < 2:
        return pieces, "een stem gevonden"

    centers = {}
    for group in groups:
        mean = np.mean([vectors[i] for i, label in zip(core, labels) if label == group], axis=0)
        centers[group] = mean / np.linalg.norm(mean)
    chosen = dict(zip(core, labels))

    result, previous = [], None
    for index, (speaker, words) in enumerate(pieces):
        if index in chosen:
            group = chosen[index]
        elif vectors[index] is not None:
            group = max(centers, key=lambda g: float(vectors[index] @ centers[g]))
        else:  # te kort om te beoordelen: bij de vorige zin houden
            group = previous if previous is not None else chosen[core[0]]
        previous = group
        result.append((f"stem-{group}", words))
    return result, f"{len(groups)} stemmen gevonden door zinnen te vergelijken"


def speech_share(start, end, turns):
    """Welk deel van dit tijdvak volgens het sprekermodel spraak is (0 tot 1)."""
    if end <= start:
        return 0.0
    overlap = sum(max(0.0, min(end, t_end) - max(start, t_start)) for t_start, t_end, _ in turns)
    return min(overlap / (end - start), 1.0)
