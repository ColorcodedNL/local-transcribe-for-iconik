"""Whisper op MLX (Apple Silicon): de eerste engine. De vaste vorm staat in engine.py."""
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path

from base import ENGINE_NAME, L, MEMORY_PROBES, SAMPLE_RATE, fail, load_config, print, tell  # noqa: F401
import settings

# Hoeveel GB MLX na een opdracht in zijn buffer mag houden. Hoort bij deze engine, dus hier aangemeld.
CACHE_SETTING = settings.register(settings.Setting(
    "transcription.mlx_cache_gb", "number", 2.0, ("Buffer van het model (GB)", "Model buffer (GB)"),
    flat="mlx_cache_gb", minimum=0, advanced=True))

NAME = ENGINE_NAME   # "mlx-whisper"; zo herkennen oudere versies het transcript in Iconik
DEFAULT_MODEL = "mlx-community/whisper-large-v3-mlx"


LANGUAGE_WINDOWS = 5  # aantal stukken van 30 seconden waarop de taal wordt bepaald


# Vaste revisie per model: elke Mac draait dezelfde, geteste versie, en een wijziging op Hugging Face
# komt pas mee na een test (zie docs/RELEASING.md). Een model dat hier niet staat volgt de nieuwste.
MODEL_REVISIONS = {
    "mlx-community/whisper-large-v3-mlx": "49e6aa286ad60c14352c404340ded53710378a11",
}


def ensure_model(repo):
    """Haalt het Whisper-model eenmalig binnen als het er nog niet is, met voortgang voor het venster.

    Geeft het lokale pad van het model terug. Met een repo-naam raadpleegt mlx_whisper bij elke
    load eerst Hugging Face; een Mac die net uit de slaapstand komt en nog geen verbinding heeft,
    bleef daar hangen. Met een pad gebeurt er niets met het netwerk.
    Met een vaste revisie (een commit-hash) zoekt de cache direct die map op, ook zonder internet."""
    import os
    import threading
    from huggingface_hub import HfApi, constants, snapshot_download

    if os.path.isdir(repo):
        return repo
    revision = MODEL_REVISIONS.get(repo)
    try:
        return snapshot_download(repo, revision=revision, local_files_only=True)  # staat al in de cache
    except Exception:
        pass

    print(L("Whisper-model wordt eenmalig gedownload (ongeveer 3 GB)...",
            "Downloading the Whisper model once (about 3 GB)..."))
    tell("stage", name="model")
    total = 0
    try:
        info = HfApi().model_info(repo, revision=revision, files_metadata=True)
        total = sum(item.size or 0 for item in info.siblings)
    except Exception:
        pass  # zonder totaal laat het venster een lopende balk zien

    folder = Path(constants.HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))
    finished = threading.Event()

    def watch():
        while not finished.wait(0.7):
            try:
                size = sum(f.stat().st_size for f in (folder / "blobs").glob("*") if f.is_file())
            except OSError:
                continue
            if total:
                tell("progress", fraction=min(size / total, 0.99))

    threading.Thread(target=watch, daemon=True).start()
    try:
        return snapshot_download(repo, revision=revision)
    except Exception as error:
        fail(L(f"Het Whisper-model kon niet worden gedownload ({type(error).__name__}). Dat is eenmalig nodig, "
               "controleer de internetverbinding en probeer het opnieuw.",
               f"The Whisper model could not be downloaded ({type(error).__name__}). This is needed once, "
               "check the internet connection and try again."), local=True)
    finally:
        finished.set()


def detect_language(samples, model_repo):
    """Bepaalt de gesproken taal op de luidste stukken van de opname, niet alleen
    op de eerste 30 seconden: daar zit vaak een leader of muziek.
    Geeft (taalcode, zekerheid) terug."""
    import mlx.core as mx
    import mlx_whisper  # noqa: F401  laadt mlx_whisper.transcribe, waar ModelHolder hieronder vandaan komt
    from mlx_whisper.audio import N_FRAMES, log_mel_spectrogram, pad_or_trim

    model = sys.modules["mlx_whisper.transcribe"].ModelHolder.get_model(model_repo, mx.float16)
    window = 30 * SAMPLE_RATE
    starts = list(range(0, max(len(samples) - window, 0) + 1, window))
    starts.sort(key=lambda start: -float((samples[start:start + window] ** 2).mean()))
    chosen = starts[:LANGUAGE_WINDOWS]
    totals = {}
    for start in chosen:
        mel = log_mel_spectrogram(samples[start:start + window], n_mels=model.dims.n_mels)
        mel = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
        _, probabilities = model.detect_language(mel)
        for code, probability in probabilities.items():
            totals[code] = totals.get(code, 0.0) + float(probability)
    best = max(totals, key=totals.get)
    return best, totals[best] / len(chosen)


# --- de vaste vorm (engine.py) ---

def start(on_progress=None):
    """Voor een opdracht: de cachegrens van MLX zetten en de voortgang van Whisper volgen."""
    import mlx_whisper  # noqa: F401  pas hier laden, dat scheelt opstarttijd bij fouten eerder in de opdracht
    import mlx.core as mx

    # MLX bewaart vrijgegeven werkgeheugen voor hergebruik. Bij Whisper groeit dat zonder
    # grens uit tot vele GB die niet meer gebruikt worden. Instelbaar via mlx_cache_gb.
    try:
        cache_gb = float(load_config().get("mlx_cache_gb", 2))
    except (TypeError, ValueError):
        cache_gb = 2.0
    mx.set_cache_limit(int(cache_gb * 1e9))

    if on_progress:
        # Whisper toont zijn voortgang met tqdm; daar lezen we op mee.
        try:
            import tqdm as tqdm_module

            class Watched(tqdm_module.tqdm):
                def update(self, n=1):
                    result = super().update(n)
                    if self.total:
                        on_progress(min(self.n / self.total, 1.0))
                    return result

            sys.modules["mlx_whisper.transcribe"].tqdm = type("Shim", (), {"tqdm": Watched})
        except Exception:
            pass


def transcribe(samples, model, language=None, prompt=None):
    import mlx_whisper
    options = dict(path_or_hf_repo=model, language=language, word_timestamps=True,
                   verbose=False)  # verbose=False: voortgangsbalk in de terminal
    if prompt:
        options["initial_prompt"] = prompt
    return mlx_whisper.transcribe(samples, **options)


def clear_working_memory():
    try:
        import mlx.core as mx
        mx.clear_cache()
    except Exception:
        pass


def release():
    holder = getattr(sys.modules.get("mlx_whisper.transcribe"), "ModelHolder", None)
    if holder is not None:
        holder.model = None
        holder.model_path = None


def info(model):
    return {"type": "EXTERNAL", "name": NAME, "model": model, "version": pkg_version("mlx-whisper")}


def revision(model):
    return MODEL_REVISIONS.get(model)


def memory_text():
    mx = sys.modules.get("mlx.core")
    if mx is None:
        return None
    return f"MLX actief {mx.get_active_memory() / 1e9:.1f} / cache {mx.get_cache_memory() / 1e9:.1f} GB"


MEMORY_PROBES.append(memory_text)
