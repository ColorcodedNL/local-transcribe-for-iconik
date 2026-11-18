"""De spraakengines van de app en de vaste vorm waarin de rest ze gebruikt.

Een engine is een module met deze namen:
  NAME                    vaste naam, ook in het transcript (engine.name) en in Iconik (engine_info.name)
  DEFAULT_MODEL           het model als niemand iets kiest
  ensure_model(model)     het model eenmalig binnenhalen op de vaste revisie; geeft wat load nodig heeft
  start(on_progress)      voor een opdracht: geheugengrenzen zetten; on_progress(fractie 0..1) of None
  detect_language(samples, model)   (taalcode, zekerheid 0..1)
  transcribe(samples, model, language, prompt=None)
                          {"language": code, "segments": [{"start", "end", "text", "words": [{"word",
                          "start", "end", "probability"}], ...}]}; extra velden zoals no_speech_prob mogen.
                          `prompt`: woorden die vaak voorkomen (de woordenlijst, later); None voor niets
  clear_working_memory()  werkgeheugen na transcriberen teruggeven; het model blijft geladen
  release()               het model vrijgeven; een volgende opdracht laadt het opnieuw
  info(model)             {"type", "name", "model", "version"}: wat Iconik als engine_info krijgt
  revision(model)         de vaste revisie van het model, of None

`samples` zijn mono float32 op base.SAMPLE_RATE. Een engine meldt zijn geheugengebruik met
base.MEMORY_PROBES.append(functie die een korte tekst of None geeft).

Nu is er één engine, Whisper op MLX. Een tweede (bijvoorbeeld faster-whisper voor Windows en Linux, of
Parakeet) komt erbij als module met dezelfde namen en een regel in ENGINES.
"""
import whisper_mlx

ENGINES = {whisper_mlx.NAME: whisper_mlx}
DEFAULT = whisper_mlx.NAME


def get(name=None):
    """De engine met deze naam; zonder naam de standaard."""
    try:
        return ENGINES[name or DEFAULT]
    except KeyError:
        raise ValueError(f"onbekende engine: {name!r}") from None
