"""Refine: alles wat het scherm van de editor nodig heeft, behalve het scherm zelf (refine.html).

Per item (items.py) een sessie in het geheugen met het transcript (eigen model), het mediabestand en de sporen.
Het transcript komt uit de kopie op deze Mac (transcripts/), anders uit Iconik via de connector.

Het venster speelt het origineel af in stukjes die ffmpeg tijdens het kijken omzet naar MPEG-4 in
fragmented mp4 (proef van 2 november: WebKit speelt dat via Media Source Extensions, zonder libmpv).
De audio is de mix van de gebruikte sporen, of één spoor als iemand dat wil horen.

De golfvormen per spoor (50 punten per seconde) komen uit het bestand dat een opdracht bewaart
(waves/); ontbreekt dat, dan rekent een draad ze hier uit en houdt de voortgang bij.
"""
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

import items
import settings
import subtitles
import audio
import base as appbase   # (base is hier ook de naam van een bestand zonder extensie)
import connectors
import transcript as tmodel

CHUNK_SECONDS = 6
CHUNK_WIDTH = 960           # breedte van het beeld in de stukjes; ruim genoeg voor het venster
WAVE_READ_RATE = 3200       # samplefrequentie om golfvormen uit te rekenen (64 per punt)

_sessions = {}
_lock = threading.Lock()
_wave_jobs = {}             # item -> {"fraction": 0..1, "error": str} zolang er gerekend wordt


class RefineError(Exception):
    pass


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,r_frame_rate:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=60).stdout
    info = json.loads(out or "{}")
    duration = float(info.get("format", {}).get("duration") or 0)
    fps = 25.0
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video":
            num, _, den = (stream.get("r_frame_rate") or "25/1").partition("/")
            try:
                fps = float(num) / float(den or 1) or 25.0
            except (ValueError, ZeroDivisionError):
                pass
            break
    return duration, fps, any(s.get("codec_type") == "video" for s in info.get("streams", []))


def _local_copy(item):
    path = items.path(appbase.TRANSCRIPTS_DIR, item, ".json")
    try:
        return tmodel.from_dict(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _reachable(path):
    try:
        return bool(path) and Path(path).is_file()
    except OSError:
        return False


def _media_info_path(item):
    return items.path(appbase.WAVES_DIR, item, ".media.json")


def _save_media_info(item, info):
    """Duur, fps en sporen van het origineel, voor als het later niet bereikbaar is."""
    try:
        path = _media_info_path(item)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or json.loads(path.read_text()) != info:
            path.write_text(json.dumps(info))
    except (OSError, ValueError):
        pass


def _offline_shape(item, doc):
    """(duur, fps, aantal sporen) zonder het origineel: uit wat Refine eerder zag, de golfvormen en het transcript."""
    try:
        info = json.loads(_media_info_path(item).read_text())
    except (OSError, ValueError):
        info = {}
    peaks = audio.read_waves(_waves_path(item)) or {}
    ends = [seg.end / 1000 for seg in doc.segments]
    duration = max([float(info.get("duration") or 0), max(ends, default=0)]
                   + [len(p) / audio.WAVE_RATE for p in peaks.values()])
    count = max([int(info.get("tracks") or 0), max(peaks, default=0)] + [t.number for t in doc.tracks])
    return duration, float(info.get("fps") or 25), count


def open_session(item, reload=False):
    """De sessie voor een item: transcript, bestand en sporen. Gooit RefineError met een leesbare reden."""
    with _lock:
        if not reload and item in _sessions:
            return _sessions[item]
    doc, origin = _local_copy(item), "mac"
    if doc is None and not items.is_iconik(item):
        raise RefineError(appbase.L("Er is geen transcript van dit bestand op deze Mac.",
                                 "There is no transcript of this file on this Mac."))
    if doc is None:
        source = items.source(item)
        if not connectors.call(source, "status").get("connected"):
            raise RefineError(appbase.L("Geen kopie op deze Mac en niet verbonden met Iconik.",
                                     "No copy on this Mac and not connected to Iconik."))
        info = connectors.call(source, "open", key=items.asset_id(item), push=False)
        found = connectors.call(source, "read", info=info, which="refined")
        if found is not None:
            doc = tmodel.from_dict(found)
            doc.source["refined"] = True
        else:
            found = connectors.call(source, "read", info=info)
            doc = tmodel.from_dict(found) if found is not None else None
        if doc is None:
            raise RefineError(appbase.L("Dit asset heeft geen transcript van Local Transcribe.",
                                     "This asset has no transcript from Local Transcribe."))
        origin = "iconik"
    media = doc.source.get("media") or ""
    if not _reachable(media) and items.is_iconik(item):
        # Een andere Mac, een verplaatste koppeling of een opdracht van voor 2 november: via Iconik zoeken.
        try:
            source = items.source(item)
            info = connectors.call(source, "open", key=items.asset_id(item), push=False)
            media = (connectors.call(source, "find_media", info=info) or [""])[0]
        except Exception:
            media = ""
    known = {t.number: t for t in doc.tracks}
    if _reachable(media):
        duration, fps, has_video = _probe(media)
        tracks = audio.list_tracks(media)
        _save_media_info(item, {"duration": duration, "fps": fps, "video": has_video, "tracks": len(tracks),
                                    "name": Path(media).name})
        offline = False
    else:
        # Zonder origineel (opslag niet gekoppeld): alleen tekst bewerken, met de golfvormen uit de cache.
        media = doc.source.get("media") or ""
        duration, fps, count = _offline_shape(item, doc)
        tracks, has_video, offline = [None] * count, False, True
    used = [n for n in range(1, len(tracks) + 1) if known.get(n) and known[n].used] or [1]
    session = {"item": item, "doc": doc, "origin": origin, "media": media, "duration": duration,
               "fps": fps, "video": has_video, "tracks": tracks, "used": used, "offline": offline,
               "labels": {n: (known[n].label if n in known else "") for n in range(1, len(tracks) + 1)}}
    with _lock:
        _sessions[item] = session
    return session


def view(item):
    """Wat het scherm bij het openen krijgt."""
    s = open_session(item, reload=True)
    doc = s["doc"]
    return {
        "item": item, "asset_id": items.asset_id(item), "title": doc.source.get("title") or items.key(item),
        "origin": s["origin"],
        "media_name": Path(s["media"]).name, "offline": s["offline"], "duration": s["duration"], "fps": s["fps"], "video": s["video"],
        "transcript": tmodel.to_dict(doc),
        "tracks": [{"n": n, "label": s["labels"][n], "used": n in s["used"]} for n in range(1, len(s["tracks"]) + 1)],
        "chunk": CHUNK_SECONDS, "lang": appbase.app_language(), "draft": load_draft(item),
        "refined": bool(doc.source.get("refined")), "original_kept": _original_path(item).is_file(),
        "export": dict(subtitle_settings(), existing=existing_subtitles(item)), "subtitles": load_subtitles(item),
    }


# --- ondertitels: een eigen laag naast het transcript ---
# Ondertitels zijn er pas als iemand ze maakt (make_subtitles): het transcript verdeeld en ingepast volgens
# de regels, bewaard als laag op deze Mac (refine/<bron>/<asset>.subtitles.json) en meteen als bestand geschreven,
# naast de media of op een plek die iemand in het bewaarvenster kiest. Daarna verdeelt de app niets meer
# vanzelf: verandert het transcript, dan meldt de laag dat (stale) en kiest iemand zelf voor opnieuw maken.

def _subtitles_path(item):
    return items.path(DRAFTS_DIR, item, ".subtitles.json")


def _current_doc(item):
    """Het transcript zoals het nu in Refine staat: het concept, anders het bewaarde."""
    draft = load_draft(item)
    return tmodel.from_dict(draft["transcript"]) if draft else open_session(item)["doc"]


def _stamp(doc):
    """Verandert zodra de tekst, de tijden of de sprekers van de blokken veranderen."""
    rows = [[s.start, s.end, s.speaker, s.display_text()] for s in doc.segments]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()[:16]


def subtitle_settings():
    """Wat het maakvenster toont: de regels en de keuzes van de vorige keer, de voorkeuzes en de grenzen."""
    kept = settings.get("refine.subtitles")
    rules = subtitles.clean_rules(kept.get("rules"))
    return {"rules": rules, "preset": subtitles.preset_of(rules), "presets": subtitles.PRESETS, "limits": subtitles.LIMITS,
            "formats": kept.get("formats") if kept.get("formats") in subtitles.CHOICES else "srt",
            "place": "other" if kept.get("place") == "other" else "media"}


def existing_subtitles(item):
    """De namen van ondertitelbestanden die al naast de media staan (clip.srt, clip.vtt), om in te laden."""
    try:
        session = open_session(item)
        if session["offline"] or not session["media"]:
            return []
        media = Path(session["media"])
        return [media.with_suffix(f".{fmt}").name for fmt in subtitles.FORMATS if media.with_suffix(f".{fmt}").is_file()]
    except (RefineError, OSError):
        return []


def _remember(**values):
    settings.update("refine.subtitles", lambda kept: kept.update(values))


def preview_subtitles(item, raw):
    """De telling voor het maakvenster: hoeveel ondertitels dit worden en hoeveel er te snel zijn."""
    rules = subtitles.clean_rules(json.loads(raw or b"{}").get("rules"))
    return subtitles.preview(_current_doc(item), rules)


def load_subtitles(item):
    """De laag van deze Mac, of None als niemand ondertitels maakte. stale: het transcript is sindsdien veranderd."""
    try:
        data = json.loads(_subtitles_path(item).read_text())
        doc = tmodel.from_dict(data["transcript"])
        target = data["target"]
        if target["formats"] not in subtitles.CHOICES or not target["base"]:
            return None
        stale = data.get("stamp") != _stamp(_current_doc(item))
    except (OSError, ValueError, KeyError, TypeError, RefineError):
        return None
    touched = [bool(x) for x in (data.get("touched") or [])][:len(doc.segments)]
    touched += [False] * (len(doc.segments) - len(touched))
    base = Path(target["base"])
    return {"transcript": data["transcript"], "touched": touched, "edits": int(data.get("edits") or 0),
            "saved_at": data.get("saved_at") or "", "rules": subtitles.clean_rules(data.get("rules")),
            "stamp": data.get("stamp") or "", "stale": stale,
            "target": {"formats": target["formats"], "place": "other" if target.get("place") == "other" else "media",
                       "base": str(base), "names": [base.with_suffix(f".{fmt}").name for fmt in subtitles.CHOICES[target["formats"]]]}}


def _store(item, lay, touched, rules, stamp, target, edits):
    _write_json(_subtitles_path(item), {
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "edits": int(edits), "rules": rules, "stamp": stamp,
        "target": {"formats": target["formats"], "place": target["place"], "base": target["base"]},
        "touched": [bool(x) for x in touched], "transcript": tmodel.to_dict(lay)})


def _export_name(text):
    """Een bestandsnaam uit een titel: zonder tekens die in een pad iets betekenen."""
    name = "".join(" " if c in '/:\\' or ord(c) < 32 else c for c in str(text or "")).strip(" .")
    return " ".join(name.split())[:120] or "transcript"


def _choose_base(item, formats, place, choose):
    """Waar de bestanden komen: (pad, door iemand zelf gekozen formaten), of (None, ()) als het bewaarvenster
    is geannuleerd. Naast de media: het mediabestand zelf, de extensie wordt die van het formaat."""
    session = open_session(item)
    media = Path(session["media"]) if session["media"] else None
    reachable = media is not None and not session["offline"]
    if place == "media":
        if not reachable:
            raise RefineError(appbase.L("Het origineel is niet bereikbaar. Kies een andere plek, of koppel de opslag (ISG).",
                                     "The original is not reachable. Choose another place, or connect the storage (ISG)."))
        return media, ()
    if choose is None:
        raise RefineError(appbase.L("Een plek kiezen kan alleen vanuit het venster van de app.",
                                 "Choosing a place only works from the app's window."))
    first = subtitles.CHOICES[formats][0]
    stem = media.stem if media is not None and media.stem else _export_name(session["doc"].source.get("title") or items.key(item))
    chosen = choose(str(media.parent) if reachable else None, f"{_export_name(stem)}.{first}")
    if not chosen:
        return None, ()
    path = Path(chosen)
    if path.suffix.lower() not in (".srt", ".vtt"):
        path = path.with_name(f"{path.name}.{first}")
    return path, (first,)


def _write_files(item, lay, target, rules, confirmed=()):
    lay.speakers = _current_doc(item).speakers     # de namen van nu, ook als ze na het maken zijn gewijzigd
    return subtitles.write_files(lay, target["base"], target["formats"], rules, appbase.SIDECARS_FILE, confirmed)


def _state(item, files=None):
    return dict(load_subtitles(item) or {}, files=files or [])


def make_subtitles(item, raw, choose):
    """Maakt de ondertitels: verdeelt het transcript, past in, bewaart de laag en schrijft het bestand.
    raw: {"rules", "formats": srt | vtt | both, "place": media | other}. Vervangt een eerdere laag."""
    data = json.loads(raw or b"{}")
    rules = subtitles.clean_rules(data.get("rules"))
    formats = data.get("formats") if data.get("formats") in subtitles.CHOICES else "srt"
    place = "other" if data.get("place") == "other" else "media"
    doc = _current_doc(item)
    lay = subtitles.layer(doc, rules)
    if not lay.segments:
        raise RefineError(appbase.L("Dit transcript heeft geen tekst.", "This transcript has no text."))
    base, confirmed = _choose_base(item, formats, place, choose)
    if base is None:
        return {"cancelled": True}
    _remember(rules=rules, formats=formats, place=place)
    target = {"formats": formats, "place": place, "base": str(base)}
    _store(item, lay, [False] * len(lay.segments), rules, _stamp(doc), target, 0)
    return _state(item, _write_files(item, lay, target, rules, confirmed))


def import_subtitles(item, raw, choose):
    """Laadt een bestaand .srt- of .vtt-bestand in als de ondertitels. raw: {"rules", "name"}: de naam van een
    bestand naast de media, of leeg om er een te kiezen (choose(map) toont het venster en geeft het pad of None).
    Sprekers en woordtiming komen waar het kan uit het transcript. Bewaren schrijft daarna naar dit bestand."""
    data = json.loads(raw or b"{}")
    rules = subtitles.clean_rules(data.get("rules"))
    session = open_session(item)
    media = Path(session["media"]) if session["media"] else None
    reachable = media is not None and not session["offline"]
    name = str(data.get("name") or "")
    if name:
        if name not in existing_subtitles(item):
            raise RefineError(appbase.L("Dat bestand staat niet naast de media.", "That file is not next to the media."))
        path = media.with_name(name)
    else:
        if choose is None:
            raise RefineError(appbase.L("Een bestand kiezen kan alleen vanuit het venster van de app.",
                                     "Choosing a file only works from the app's window."))
        chosen = choose(str(media.parent) if reachable else None)
        if not chosen:
            return {"cancelled": True}
        path = Path(chosen)
    fmt = path.suffix.lower().lstrip(".")
    if fmt not in subtitles.FORMATS:
        raise RefineError(appbase.L("Alleen .srt en .vtt kunnen worden ingeladen.", "Only .srt and .vtt can be loaded."))
    try:
        if path.stat().st_size > subtitles.MAX_IMPORT:
            raise RefineError(appbase.L("Dat bestand is te groot voor ondertitels.", "That file is too large for subtitles."))
        found = subtitles.parse(path.read_bytes())
    except OSError as error:
        raise RefineError(appbase.L(f"Inladen lukte niet: {error.strerror or error}", f"Loading failed: {error.strerror or error}"))
    if not found:
        raise RefineError(appbase.L("In dat bestand staan geen ondertitels.", "There are no subtitles in that file."))
    doc = _current_doc(item)
    lay = subtitles.import_layer(found, doc)
    beside = reachable and path.parent == media.parent and path.stem == media.stem
    target = {"formats": fmt, "place": "media" if beside else "other", "base": str(media if beside else path)}
    subtitles.adopt(path, appbase.SIDECARS_FILE)
    _remember(rules=rules)
    # Alles telt als met de hand gemaakt: inpassen blijft ervan af.
    _store(item, lay, [True] * len(lay.segments), rules, _stamp(doc), target, 0)
    return _state(item, [{"name": path.name, "path": str(path), "state": "loaded", "detail": "", "cues": len(lay.segments)}])


def save_subtitles(item, raw):
    """Bewaart de laag die het scherm stuurt op deze Mac (nog niet in het bestand):
    {"transcript", "touched", "edits", "rules"}. De plek en de stempel blijven die van het maken."""
    data = json.loads(raw)
    kept = load_subtitles(item)
    if kept is None:
        raise RefineError(appbase.L("Er zijn geen ondertitels om te bewaren.", "There are no subtitles to save."))
    lay = tmodel.from_dict(data["transcript"])        # controleert de opbouw; gooit bij onzin
    touched = list(data.get("touched") or [])[:len(lay.segments)]
    touched += [True] * (len(lay.segments) - len(touched))
    rules = subtitles.clean_rules(data.get("rules"))
    _store(item, lay, touched, rules, kept["stamp"], kept["target"], int(data.get("edits") or 0))
    return load_subtitles(item)["saved_at"]


def write_subtitles(item, choose=None, elsewhere=False):
    """Schrijft de laag opnieuw naar zijn bestand (Bewaren), of naar een plek die iemand nu kiest."""
    kept = load_subtitles(item)
    if kept is None:
        raise RefineError(appbase.L("Er zijn geen ondertitels om te bewaren.", "There are no subtitles to save."))
    lay, target, confirmed = tmodel.from_dict(kept["transcript"]), kept["target"], ()
    if elsewhere:
        base, confirmed = _choose_base(item, target["formats"], "other", choose)
        if base is None:
            return {"cancelled": True}
        target = {"formats": target["formats"], "place": "other", "base": str(base)}
    files = _write_files(item, lay, target, kept["rules"], confirmed)
    done = any(f["state"] == "written" for f in files)
    if done or elsewhere:
        _store(item, lay, kept["touched"], kept["rules"], kept["stamp"], target, 0 if done else kept["edits"])
    return _state(item, files)


def fit_subtitles(item, raw):
    """Past de laag opnieuw in met deze regels (samenvoegen, uitloop). Wat met de hand is aangepast blijft staan."""
    kept = load_subtitles(item)
    if kept is None:
        raise RefineError(appbase.L("Er zijn geen ondertitels om in te passen.", "There are no subtitles to fit."))
    rules = subtitles.clean_rules(json.loads(raw or b"{}").get("rules"))
    before = tmodel.from_dict(kept["transcript"])
    lay, touched = subtitles.fit_layer(before, kept["touched"], rules)
    changed = tmodel.to_dict(lay)["segments"] != tmodel.to_dict(before)["segments"]
    _remember(rules=rules)
    _store(item, lay, touched, rules, kept["stamp"], kept["target"], kept["edits"] + (1 if changed else 0))
    return dict(_state(item), changed=changed)


def discard_subtitles(item):
    """Haalt de laag weg. Een bestand dat al geschreven is, blijft staan."""
    try:
        _subtitles_path(item).unlink()
    except FileNotFoundError:
        pass


def _audio_args(session, solo):
    """ffmpeg-argumenten voor de audio van een stukje: de gebruikte sporen gemixt, of één spoor."""
    chosen = [solo] if solo else session["used"]
    chosen = [n for n in chosen if 1 <= n <= len(session["tracks"])]
    if not chosen:
        return ["-an"], []
    parts, labels = [], []
    for k, n in enumerate(chosen):
        stream, channel, channels = session["tracks"][n - 1]
        pan = f"pan=mono|c0=c{channel}" if channels > 1 else "anull"
        parts.append(f"[0:a:{stream}]{pan}[t{k}]")
        labels.append(f"[t{k}]")
    if len(labels) == 1:
        graph = ";".join(parts) + f";{labels[0]}anull[mix]"
    else:
        graph = ";".join(parts) + f";{''.join(labels)}amix=inputs={len(labels)}:normalize=0,alimiter=limit=0.95[mix]"
    return ["-filter_complex", graph, "-map", "[mix]", "-c:a", "aac", "-b:a", "128k", "-ac", "2"], chosen


def chunk(item, start, seconds=CHUNK_SECONDS, solo=0):
    """Een stukje van het origineel als fragmented mp4 (MPEG-4 beeld, AAC geluid)."""
    s = open_session(item)
    if s["offline"]:
        raise RefineError(_offline_reason())
    start = max(0.0, min(float(start), max(s["duration"] - 0.05, 0)))
    seconds = max(0.5, min(float(seconds), 30.0))
    audio, _ = _audio_args(s, int(solo or 0))
    video = (["-map", "0:v:0", "-vf", f"scale={CHUNK_WIDTH}:-2", "-c:v", "mpeg4", "-q:v", "4", "-g", "25"]
             if s["video"] else [])
    # Een stukje begint altijd op tijd 0; de speler zet het zelf op de juiste plek (timestampOffset per stukje).
    # Achter elkaar plakken liep per stukje 23 ms uit (AAC vult aan), na een half uur seconden naast het beeld.
    command = ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-i", s["media"], "-t", f"{seconds:.3f}",
               *video, *audio, "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1"]
    result = subprocess.run(command, capture_output=True, timeout=120)
    if result.returncode or not result.stdout:
        raise RefineError("ffmpeg: " + result.stderr.decode(errors="replace").strip()[-300:])
    return result.stdout


# --- golfvormen ---

def _offline_reason():
    return appbase.L("Het origineel is op deze Mac niet bereikbaar. Is de opslag (ISG) gekoppeld?",
                  "The original is not reachable on this Mac. Is the storage (ISG) connected?")


def _waves_path(item):
    return items.path(appbase.WAVES_DIR, item, ".waves")


def waves(item):
    """(bytes van het golfvormbestand, None) als ze er zijn, anders (None, voortgang 0 tot 1).
    Zijn ze er niet, dan begint een draad ze uit te rekenen."""
    path = _waves_path(item)
    if audio.read_waves(path) is not None:
        return path.read_bytes(), None
    if open_session(item)["offline"]:
        raise RefineError(_offline_reason())
    with _lock:
        job = _wave_jobs.get(item)
        if job is None:
            job = _wave_jobs[item] = {"fraction": 0.0, "error": ""}
            threading.Thread(target=_compute_waves, args=(item, job), daemon=True, name="golfvormen").start()
    if job["error"]:
        with _lock:
            _wave_jobs.pop(item, None)
        raise RefineError(job["error"])
    return None, job["fraction"]


def _compute_waves(item, job):
    """Leest alle sporen één keer met een lage samplefrequentie en houdt per 1/50 s de piek bij."""
    try:
        s = open_session(item)
        tracks, count = s["tracks"], len(s["tracks"])
        streams = sorted({t[0] for t in tracks})
        command = ["ffmpeg", "-nostdin", "-v", "error", "-i", s["media"], "-vn"]
        if len(streams) == 1:
            command += ["-map", f"0:a:{streams[0]}"]
        else:
            inputs = "".join(f"[0:a:{i}]" for i in streams)
            command += ["-filter_complex", f"{inputs}amerge=inputs={len(streams)}[all]", "-map", "[all]"]
        command += ["-ar", str(WAVE_READ_RATE), "-f", "s16le", "-"]
        size = WAVE_READ_RATE // audio.WAVE_RATE                 # samples per punt
        block = size * audio.WAVE_RATE * 10 * count * 2           # 10 seconden per keer, in bytes
        total = max(s["duration"] * WAVE_READ_RATE, 1)
        parts = {n: [] for n in range(1, count + 1)}
        rest, done = b"", 0
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                data = process.stdout.read(block)
                if not data:
                    break
                data = rest + data
                frame = size * count * 2
                usable = len(data) // frame * frame
                rest = data[usable:]
                if usable:
                    samples = np.frombuffer(data[:usable], np.int16).reshape(-1, count)
                    for i in range(count):
                        parts[i + 1].append(audio.wave_peaks(samples[:, i].copy(), WAVE_READ_RATE))
                    done += len(samples)
                    job["fraction"] = min(done / total, 0.99)
        finally:
            process.stdout.close()
            process.wait()
        peaks = {n: b"".join(chunks) for n, chunks in parts.items()}
        if not any(peaks.values()):
            raise RefineError(appbase.L("Geen audio gelezen voor de golfvormen.", "No audio read for the waveforms."))
        audio.write_waves(_waves_path(item), peaks)
        with _lock:
            _wave_jobs.pop(item, None)
    except Exception as error:
        job["error"] = str(error) or type(error).__name__


# --- concept: bewerkingen bewaren op deze Mac tot ze naar Iconik gaan (deel 3) ---

DRAFTS_DIR = appbase.SUPPORT_DIR / "refine"
DRAFT_LIMIT = 50 * 1024 * 1024


def _draft_path(item):
    return items.path(DRAFTS_DIR, item, ".draft.json")


def load_draft(item):
    try:
        data = json.loads(_draft_path(item).read_text())
        tmodel.from_dict(data["transcript"])          # alleen een geldig transcript telt
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_draft(item, raw):
    """Bewaart het concept dat het scherm stuurt: {"transcript": eigen model als dict, "edits": aantal}."""
    if not item:
        raise RefineError("Geen asset.")
    data = json.loads(raw)
    doc = tmodel.from_dict(data["transcript"])        # controleert de opbouw; gooit bij onzin
    original = open_session(item)["doc"]
    doc.source, doc.engine, doc.tracks = original.source, original.engine, original.tracks
    payload = {"saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "edits": int(data.get("edits") or 0),
               "transcript": tmodel.to_dict(doc)}
    path = _draft_path(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False))
    temp.replace(path)
    return payload["saved_at"]


def discard_draft(item):
    try:
        _draft_path(item).unlink()
    except FileNotFoundError:
        pass



# --- opslaan in Iconik (deel 3) ---
# In Iconik blijft alleen de correctie staan: Iconik toont alleen het nieuwste transcript maar doorzoekt ze
# allemaal, dus een origineel ernaast zou weggecorrigeerde woorden vindbaar houden (getest 2 november).
# Het origineel blijft op deze Mac (refine/<bron>/<asset>.original.json) en kan terug, tot iemand die opslag leegt.

def _original_path(item):
    return items.path(DRAFTS_DIR, item, ".original.json")


def _complete(doc, other):
    """Vult aan wat Iconik niet bewaart (het bestand op deze Mac, de sporenanalyse) uit een ander transcript
    van hetzelfde asset."""
    for key in ("connector", "item", "asset_id", "version_id", "title", "media"):
        if not doc.source.get(key) and other.source.get(key):
            doc.source[key] = other.source[key]
    if not doc.tracks:
        doc.tracks = list(other.tracks)


def _source(item):
    """Het exemplaar waar het item vandaan komt, als het een transcript kan terugschrijven."""
    if not items.is_iconik(item):
        raise RefineError(appbase.L("Dit item komt niet uit Iconik.", "This item does not come from Iconik."))
    source = items.source(item)
    if not connectors.can(source, "write_back") or not connectors.call(source, "status").get("connected"):
        raise RefineError(appbase.L("Niet verbonden met Iconik.", "Not connected to Iconik."))
    return source


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False))
    temp.replace(path)


def save_to_iconik(item, version=""):
    """Zet het concept als correctie in Iconik. Het Whisper-transcript gaat daar weg en blijft op deze Mac.
    `version`: de versie van de app, voor engine_info."""
    draft = load_draft(item)
    if draft is None:
        raise RefineError(appbase.L("Er is geen concept om op te slaan.", "There is no draft to save."))
    doc = tmodel.from_dict(draft["transcript"])
    if not any(seg.words or seg.text.strip() for seg in doc.segments):
        raise RefineError(appbase.L("Het transcript is leeg.", "The transcript is empty."))
    source = _source(item)
    info = connectors.call(source, "open", key=items.asset_id(item), push=True, replace=True, allow_refined=True)   # weigert een andere bron
    original = _original_path(item)
    if not original.is_file():
        # Het origineel één keer bewaren: bij een tweede correctie staat het Whisper-transcript er niet meer.
        # Liefst de kopie van deze Mac (met sporen, modelrevisie en bestand); anders uit Iconik, aangevuld.
        whisper = _local_copy(item)
        if whisper is None or whisper.source.get("refined"):
            found = connectors.call(source, "read", info=info)
            whisper = tmodel.from_dict(found) if found is not None else None
            if whisper is not None:
                _complete(whisper, doc)
        if whisper is not None:
            _write_json(original, {"saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "transcript": tmodel.to_dict(whisper)})
    engine_info = {"type": "EXTERNAL", "name": appbase.REFINED_ENGINE, "model": doc.engine.get("model") or "",
                   "version": version}
    result = connectors.call(source, "write", info=info, doc=tmodel.to_dict(doc), engine_info=engine_info, replace="all")
    transcription_id, written = result["id"], result["segments"]
    doc.source.update(transcription_id=transcription_id, refined=True)
    items.save_transcript(doc, item)    # de kopie op deze Mac is nu de correctie
    discard_draft(item)
    with _lock:
        _sessions.pop(item, None)
    print(f"Refine: correctie {transcription_id} staat op het asset ({written} regels).")
    return {"transcription_id": transcription_id, "segments": written}


def restore_original(item):
    """Zet het bewaarde Whisper-transcript terug in Iconik in plaats van de correctie."""
    try:
        data = json.loads(_original_path(item).read_text())
        doc = tmodel.from_dict(data["transcript"])
    except (OSError, ValueError, KeyError, TypeError):
        raise RefineError(appbase.L("Het origineel is niet meer op deze Mac.", "The original is no longer on this Mac."))
    source = _source(item)
    info = connectors.call(source, "open", key=items.asset_id(item), push=True, replace=True, allow_refined=True)
    engine_info = {"type": "EXTERNAL", "name": appbase.ENGINE_NAME, "model": doc.engine.get("model") or "",
                   "version": doc.engine.get("version") or ""}
    result = connectors.call(source, "write", info=info, doc=tmodel.to_dict(doc), engine_info=engine_info, replace="all")
    transcription_id, written = result["id"], result["segments"]
    _complete(doc, open_session(item)["doc"])
    doc.source.update(transcription_id=transcription_id)
    doc.source.pop("refined", None)
    items.save_transcript(doc, item)
    _original_path(item).unlink()
    discard_draft(item)
    with _lock:
        _sessions.pop(item, None)
    print(f"Refine: origineel teruggezet als {transcription_id} ({written} regels).")
    return {"transcription_id": transcription_id, "segments": written}


def originals_info():
    """Hoeveel originelen van correcties er op deze Mac staan, voor de voorkeuren."""
    files = [f for _, f in items.files(DRAFTS_DIR, ".original.json")]
    return {"count": len(files), "bytes": sum(f.stat().st_size for f in files)}


def originals_list():
    """De bewaarde originelen, nieuwste eerst, voor de lijst in Instellingen: [{"item", "title", "connector",
    "bytes", "time"}]."""
    out = []
    for item, f in items.files(DRAFTS_DIR, ".original.json"):
        try:
            data = json.loads(f.read_text())
            source = (data.get("transcript") or {}).get("source") or data.get("source") or {}
            stat = f.stat()
        except (OSError, ValueError, AttributeError):
            continue
        out.append({"item": item, "title": str(source.get("title") or ""),
                    "connector": str(source.get("connector") or ""), "bytes": stat.st_size, "time": stat.st_mtime})
    return sorted(out, key=lambda item: item["time"], reverse=True)


def remove_original(item):
    """Eén bewaard origineel weg (bijvoorbeeld van vertrouwelijk materiaal); de correctie zelf blijft."""
    _original_path(item).unlink(missing_ok=True)


def clear_originals():
    """Gooit de bewaarde originelen weg; daarna kan een correctie niet meer terug naar het origineel."""
    for _, f in items.files(DRAFTS_DIR, ".original.json"):
        f.unlink(missing_ok=True)



# --- alles van één item van deze Mac ---

def forget_item(item):
    """Alles wat de app op deze Mac over dit item bewaart weg ("Verwijder ook van deze Mac"): de kopie van het
    transcript, de golfvormen, het concept, het origineel en de ondertitellaag van Refine, en de paden van de
    ondertitelbestanden in sidecars.json. Nooit Iconik, nooit de media en nooit bestanden naast de media.
    Geeft de namen van wat weg is."""
    gone = []
    try:   # eerst de paden uit de ondertitellaag, want die gaat zo weg
        target = json.loads(_subtitles_path(item).read_text()).get("target") or {}
        base = Path(target["base"]) if target.get("base") else None
    except (OSError, ValueError, AttributeError, TypeError):
        base = None
    if base is not None:
        gone += subtitles.forget([base.with_suffix(f".{fmt}") for fmt in ("srt", "vtt")], appbase.SIDECARS_FILE)
    for path in (items.path(appbase.TRANSCRIPTS_DIR, item, ".json"), _waves_path(item), _media_info_path(item),
                 _draft_path(item), _original_path(item), _subtitles_path(item)):
        try:
            path.unlink()
            gone.append(path.name)
        except FileNotFoundError:
            pass
    _marks_cache.pop(items.path(appbase.TRANSCRIPTS_DIR, item, ".json"), None)
    return gone


# --- labels in de opdrachtenlijst ---
_marks_cache = {}   # pad van de kopie -> (mtime, gecorrigeerd)


def marks(item):
    """Wat de opdrachtenlijst bij een item toont: {"refined": correctie staat in Iconik, "draft": concept op
    deze Mac, "subtitles": iemand maakte hier ondertitels}. Het venster vraagt dit elke seconde, dus de kopie
    alleen opnieuw lezen als die veranderde."""
    if not items.valid(item):
        return {"refined": False, "draft": False, "subtitles": False}
    copy = items.path(appbase.TRANSCRIPTS_DIR, item, ".json")
    refined = False
    try:
        mtime = copy.stat().st_mtime
        cached = _marks_cache.get(copy)
        if cached and cached[0] == mtime:
            refined = cached[1]
        else:
            refined = bool(json.loads(copy.read_text()).get("source", {}).get("refined"))
            _marks_cache[copy] = (mtime, refined)
    except (OSError, ValueError, AttributeError):
        pass
    return {"refined": refined, "draft": _draft_path(item).is_file(), "subtitles": _subtitles_path(item).is_file()}
