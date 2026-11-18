"""Eén opdracht van begin tot eind: asset openen, audio lezen, transcriberen, sprekers, filters, wegschrijven.
Ook de commandoregel (transcribe.py <asset_id>).
"""
import argparse
import json
import os
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path
import transcript as tmodel

from base import (  # noqa: F401
    ENGINE_NAME, L, LISTENER, SAMPLE_RATE, _local, fail, load_config, memory_report, print, save_transcript, tell,
)
from iconik_api import make_client  # noqa: F401
from audio import load_audio_tracks, save_waves  # noqa: F401
from speakers import (  # noqa: F401
    diarize, empty_gpu_cache, make_voiceprint, refine_speakers, speaker_at, speaker_from_tracks, speaker_model_available,
)
from filters import (  # noqa: F401
    MIN_CONFIDENCE, MIN_WORDS_OTHER_LANGUAGE, drop_repeats, expected_languages, is_hallucination, split_sentences,
)
from whisper_mlx import MODEL_REVISIONS, detect_language, ensure_model  # noqa: F401


def main():
    parser = argparse.ArgumentParser(description="Whisper-transcript naar Iconik.")
    parser.add_argument("asset_id")
    parser.add_argument("--model", default="mlx-community/whisper-large-v3-mlx")
    parser.add_argument("--language", default="auto", help="Taalcode zoals nl of en; auto herkent de taal.")
    parser.add_argument("--mount", help="Lokaal pad van de storage, eenmalig, zonder het te onthouden.")
    parser.add_argument("--ask-mount", action="store_true",
                        help="Opnieuw vragen waar de storage op deze Mac staat.")
    parser.add_argument("--push", action="store_true", help="Schrijf het transcript naar Iconik.")
    parser.add_argument("--replace", action="store_true",
                        help="Vervang een eerder transcript van dit script.")
    parser.add_argument("--speakers", type=int, help="Aantal sprekers, als je dat weet.")
    parser.add_argument("--no-speakers", action="store_true", help="Sprekerherkenning overslaan.")
    parser.add_argument("--track", type=int, help="Nummer van het audiospoor (1, 2, ...).")
    parser.add_argument("--no-voice-check", action="store_true",
                        help="Stemcontrole per zin overslaan.")
    parser.add_argument("--out", default="/tmp/whisper-test", help="Map voor de proefoutput.")
    args = parser.parse_args()

    iconik = make_client()
    if iconik is None:
        fail("Niet ingelogd. Log in via de app, of zet ICONIK_APP_ID en ICONIK_TOKEN (source ~/.iconik-env).")
    print("Iconik:  " + ("ingelogd als gebruiker" if iconik.uses_login else "persoonlijk token"))

    hf_token = os.environ.get("HF_TOKEN")
    if not args.no_speakers and not speaker_model_available(hf_token):
        fail("Geen sprekermodel: de lokale kopie ontbreekt en er is geen HF_TOKEN. Gebruik --no-speakers om zonder te draaien.")

    process_asset(iconik, args, hf_token)


def prepare_asset(iconik, args, on_info=None, interactive=True):
    """Alles voor de GPU: controles bij Iconik, het bestand vinden en de audio lezen.
    Kan vooruit draaien terwijl een andere opdracht transcribeert."""
    import iconik as connectors   # hier pas: iconik.py importeert deze module
    connector = connectors.IconikConnector(iconik)
    info = connector.open(args.asset_id, args.push, args.replace)
    asset, version_id, own = info["asset"], info["version_id"], info["own"]
    path = connector.find_media(info, args.mount, getattr(args, "ask_mount", False), interactive)[0]
    print(f"Bestand: {path}")

    print(f"Model:   {args.model}")
    model_path = ensure_model(args.model)
    memory_report("begin")
    tell("stage", name="audio")
    _local.tracks, _local.peaks = None, {}
    try:
        samples, microphones = load_audio_tracks(path, getattr(args, "track", None))
        tracks, peaks = getattr(_local, "tracks", None) or [], _local.peaks
    finally:
        _local.peaks = None
    print(f"Audio:   {len(samples) / SAMPLE_RATE:.0f} seconden")
    memory_report("audio")
    if on_info:
        on_info(asset.get("title"), len(samples) / SAMPLE_RATE)
    return {"asset": asset, "version_id": version_id, "own": own, "info": info, "media_path": str(path), "tracks": tracks, "peaks": peaks,
            "samples": samples, "microphones": microphones, "model_path": model_path}


def nothing_written(args, own):
    """Eén slechte run (verkeerd spoor, kapotte audio, muziek) mag een eerder goed transcript niet kosten:
    dat blijft staan. Tot 0.3.6 werd het bij te weinig zekerheid verwijderd."""
    if args.push and args.replace and own:
        return L("Het eerdere transcript is behouden.", "The earlier transcript was kept.")
    return L("Er is niets naar Iconik geschreven.", "Nothing was written to Iconik.")


def process_asset(iconik, args, hf_token, on_info=None, prepared=None):
    """Transcribeert een asset. `args` heeft de velden van de opties hierboven.
    Geeft een korte samenvatting terug; gooit Problem bij een fout.
    `on_info(titel, seconden)` wordt aangeroepen zodra die bekend zijn.
    `prepared` is de uitkomst van prepare_asset als die al vooruit gelezen is."""
    if prepared is None:
        prepared = prepare_asset(iconik, args, on_info)
    else:
        memory_report("audio (vooruit gelezen)")
    asset, version_id, own = prepared["asset"], prepared["version_id"], prepared["own"]
    samples, microphones = prepared["samples"], prepared["microphones"]
    model_path = prepared.get("model_path") or args.model  # een lokaal pad: laden raakt het netwerk niet
    print("Transcriberen...")
    tell("stage", name="transcriberen")
    import mlx_whisper  # pas hier laden, dat scheelt opstarttijd bij fouten hierboven
    import mlx.core as mx

    # MLX bewaart vrijgegeven werkgeheugen voor hergebruik. Bij Whisper groeit dat zonder
    # grens uit tot vele GB die niet meer gebruikt worden. Instelbaar via mlx_cache_gb.
    try:
        cache_gb = float(load_config().get("mlx_cache_gb", 2))
    except (TypeError, ValueError):
        cache_gb = 2.0
    mx.set_cache_limit(int(cache_gb * 1e9))

    if LISTENER:
        # Whisper toont zijn voortgang met tqdm; daar lezen we op mee.
        try:
            import tqdm as tqdm_module

            class Watched(tqdm_module.tqdm):
                def update(self, n=1):
                    result = super().update(n)
                    if self.total:
                        tell("progress", fraction=min(self.n / self.total, 1.0))
                    return result

            sys.modules["mlx_whisper.transcribe"].tqdm = type("Shim", (), {"tqdm": Watched})
        except Exception:
            pass

    language = args.language
    if not language or language == "auto":
        try:
            language, certainty = detect_language(samples, model_path)
            print(f"Taal:    {language} (herkend, {certainty:.0%})")
        except Exception as error:
            language = None  # dan bepaalt Whisper het zelf op het begin van de opname
            print(f"Taal:    herkennen lukte niet ({type(error).__name__}: {error}), Whisper kiest zelf")
    else:
        print(f"Taal:    {language} (ingesteld)")
    memory_report("taal")

    result = mlx_whisper.transcribe(
        samples,
        path_or_hf_repo=model_path,
        language=language,
        word_timestamps=True,
        verbose=False,  # voortgangsbalk in de terminal
    )
    memory_report("transcriberen")
    mx.clear_cache()  # Whisper blijft geladen, het losse werkgeheugen gaat terug voor de sprekerherkenning

    language = language or result.get("language") or "nl"
    tell("language", code=language)

    turns = None
    if not args.no_speakers:
        print("Sprekers herkennen...")
        tell("stage", name="sprekers")
        turns = diarize(samples, hf_token, args.speakers)
        memory_report("sprekers")

    kept, dropped = [], []
    for segment in result["segments"]:
        reason = is_hallucination(segment, turns, language)
        if reason:
            dropped.append((segment, reason))
        else:
            kept.append(segment)

    print(f"\nSegmenten: {len(kept)} behouden, {len(dropped)} weggefilterd")
    empty = sum(1 for _, reason in dropped if reason == "geen woorden")
    if empty:
        print(f"  waarvan {empty} lege segmenten")
    for segment, reason in dropped:
        if reason != "geen woorden":
            print(f"  weg  [{segment['start']:7.2f}s] {segment['text'].strip()!r}  ({reason})")

    # Meting: hoe zeker is Whisper van de tekst? Verzonnen tekst scoort laag.
    scores = [w.get("probability", 0.0) for s in kept for w in s.get("words") or []]
    if scores:
        ordered = sorted(scores)
        low = sum(1 for p in scores if p < 0.5) / len(scores)
        print(f"Zekerheid: gemiddeld {sum(scores) / len(scores):.2f}, "
              f"mediaan {ordered[len(ordered) // 2]:.2f}, "
              f"{low:.0%} van {len(scores)} woorden onder 0.50")
        if sum(scores) / len(scores) < MIN_CONFIDENCE:
            for s in kept[:5]:
                print(f"  [{s['start']:7.2f}s] {s['text'].strip()[:70]}")
            note = nothing_written(args, own)
            mean = sum(scores) / len(scores)
            fail(L(f"Geen verstaanbare spraak (zekerheid {mean:.2f}, grens {MIN_CONFIDENCE}). {note}",
                   f"No intelligible speech (confidence {mean:.2f}, threshold {MIN_CONFIDENCE}). {note}"), skip=True)
    if not kept:
        fail(L("Geen spraak gevonden, er is niets om te pushen.", "No speech found, nothing to send."), skip=True)

    # Muziek zonder spraak wordt soms als een andere taal herkend, met één verzonnen regel (gezien:
    # Nederlandse spots met muziek als Russisch). Een paar woorden in een onverwachte taal: overslaan.
    # Een echt anderstalig item heeft meer tekst. Een vast gekozen taal is een bewuste keuze: dan niet.
    words = sum(len(s.get("words") or []) for s in kept)
    expected = expected_languages()
    if (not args.language or args.language == "auto") and language not in expected and words < MIN_WORDS_OTHER_LANGUAGE:
        note = nothing_written(args, own)
        fail(L(f"Geen verstaanbare spraak: {words} woorden herkend als {language}, verwacht {', '.join(expected)}. {note}",
               f"No intelligible speech: {words} words recognised as {language}, expected {', '.join(expected)}. {note}"),
             skip=True)

    # Stukken: (spreker van pyannote of None, woorden)
    if microphones and not args.no_speakers:
        pieces, previous = [], None
        for segment in kept:
            for words in split_sentences(segment["words"]):
                previous = speaker_from_tracks(words, microphones, previous)
                pieces.append((previous, words))
        print("Sprekers: afgelezen uit de sporen")
    elif not turns:
        if turns is not None:
            print(L("Geen sprekers gevonden, alles onder Spreker 1.", "No speakers found, everything under Speaker 1."))
        pieces = [(None, s["words"]) for s in kept]
    else:
        pieces = []
        for segment in kept:
            for words in split_sentences(segment["words"]):
                pieces.append((speaker_at(words[0]["start"], words[-1]["end"], turns), words))
        if not args.no_voice_check:
            pieces, note = refine_speakers(pieces, turns, make_voiceprint(samples))
            empty_gpu_cache()
            print(f"Stemcontrole: {note}")
            memory_report("stemcontrole")

    torch = sys.modules.get("torch")
    if torch is not None and torch.backends.mps.is_available():
        torch.mps.empty_cache()

    pieces, repeated = drop_repeats(pieces)
    if repeated:
        print(f"Herhalingen: {repeated} regels weggehaald uit lussen van dezelfde zin")

    # Sprekers nummeren op volgorde van eerste optreden.
    numbers = {}
    for speaker, _ in pieces:
        numbers.setdefault(speaker, len(numbers))
    pieces = [(numbers[speaker], words) for speaker, words in pieces]
    print(f"Sprekers: {len(numbers)}, stukken: {len(pieces)}")

    engine_info = {
        "type": "EXTERNAL",
        "name": ENGINE_NAME,
        "model": args.model,
        "version": pkg_version("mlx-whisper"),
    }
    # Het transcript in het eigen model; de payload voor Iconik komt daaruit. De sporen en de vaste
    # modelrevisie staan alleen in het model: Iconik kent er geen velden voor.
    doc = tmodel.from_pieces(
        pieces, language, L("Spreker", "Speaker"),
        engine=dict(engine_info, revision=MODEL_REVISIONS.get(args.model)),
        tracks=tmodel.tracks_from_infos(prepared.get("tracks")),
        source={"connector": "iconik", "asset_id": args.asset_id, "version_id": version_id,
                "title": asset.get("title") or "", "media": prepared.get("media_path") or ""})
    properties = dict(tmodel.to_iconik(doc, "", version_id)[0], engine_info=engine_info)

    if not args.push:
        segments = tmodel.to_iconik(doc, "<transcription_id>", version_id)[1]
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{args.asset_id}.iconik.json"
        out_file.write_text(json.dumps(
            {"properties": properties, "segments": segments}, ensure_ascii=False, indent=2
        ))
        (out_dir / f"{args.asset_id}.transcript.json").write_text(
            json.dumps(tmodel.to_dict(doc), ensure_ascii=False, indent=2))
        print()
        for segment in segments[:25]:
            seconds = segment["time_start_milliseconds"] / 1000
            number = segment["transcription"]["speaker"] + 1
            print(f"  {seconds:7.2f}s  {L('Spreker', 'Speaker')} {number}  {segment['segment_text']}")
        if len(segments) > 25:
            print(f"  ... en nog {len(segments) - 25} stukken")
        print("\nProefrun klaar, niets naar Iconik geschreven.")
        print(f"Volledige payload: {out_file}")
        return {"title": asset.get("title"), "segments": len(segments),
                "speakers": len(numbers), "pushed": False, "language": language}

    tell("stage", name="schrijven")
    import iconik as connectors
    info = prepared.get("info") or {"asset_id": args.asset_id, "version_id": version_id,
                                    "title": asset.get("title") or "", "own": own}
    transcription_id, written = connectors.IconikConnector(iconik).write(
        info, doc, engine_info, getattr(args, "still_ours", None))
    print(f"\nKlaar. Transcript {transcription_id} staat op het asset.")
    doc.source["transcription_id"] = transcription_id
    save_transcript(doc)
    save_waves(args.asset_id, prepared.get("peaks"))
    return {"title": asset.get("title"), "segments": written,
            "speakers": len(numbers), "pushed": True, "language": language}
