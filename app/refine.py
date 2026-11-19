"""Refine: alles wat het scherm van de editor nodig heeft, behalve het scherm zelf (refine.html).

Per asset een sessie in het geheugen met het transcript (eigen model), het mediabestand en de sporen.
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

import settings
import subtitles
import transcribe as core
import transcript as tmodel

CHUNK_SECONDS = 6
CHUNK_WIDTH = 960           # breedte van het beeld in de stukjes; ruim genoeg voor het venster
WAVE_READ_RATE = 3200       # samplefrequentie om golfvormen uit te rekenen (64 per punt)

_sessions = {}
_lock = threading.Lock()
_wave_jobs = {}             # asset_id -> {"fraction": 0..1, "error": str} zolang er gerekend wordt


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


def _local_copy(asset_id):
    path = core.TRANSCRIPTS_DIR / core.safe_name(asset_id, ".json")
    try:
        return tmodel.from_dict(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _reachable(path):
    try:
        return bool(path) and Path(path).is_file()
    except OSError:
        return False


def _media_info_path(asset_id):
    return core.WAVES_DIR / core.safe_name(asset_id, ".media.json")


def _save_media_info(asset_id, info):
    """Duur, fps en sporen van het origineel, voor als het later niet bereikbaar is."""
    try:
        path = _media_info_path(asset_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or json.loads(path.read_text()) != info:
            path.write_text(json.dumps(info))
    except (OSError, ValueError):
        pass


def _offline_shape(asset_id, doc):
    """(duur, fps, aantal sporen) zonder het origineel: uit wat Refine eerder zag, de golfvormen en het transcript."""
    try:
        info = json.loads(_media_info_path(asset_id).read_text())
    except (OSError, ValueError):
        info = {}
    peaks = core.read_waves(_waves_path(asset_id)) or {}
    ends = [seg.end / 1000 for seg in doc.segments]
    duration = max([float(info.get("duration") or 0), max(ends, default=0)]
                   + [len(p) / core.WAVE_RATE for p in peaks.values()])
    count = max([int(info.get("tracks") or 0), max(peaks, default=0)] + [t.number for t in doc.tracks])
    return duration, float(info.get("fps") or 25), count


def open_session(asset_id, reload=False):
    """De sessie voor een asset: transcript, bestand en sporen. Gooit RefineError met een leesbare reden."""
    with _lock:
        if not reload and asset_id in _sessions:
            return _sessions[asset_id]
    doc, client, origin = _local_copy(asset_id), None, "mac"
    if doc is None:
        client = core.make_client()
        if client is None:
            raise RefineError(core.L("Geen kopie op deze Mac en niet verbonden met Iconik.",
                                     "No copy on this Mac and not connected to Iconik."))
        import iconik
        connector = iconik.IconikConnector(client)
        info = connector.open(asset_id, push=False)
        doc = connector.read(info, connector.refined_names)
        if doc is not None:
            doc.source["refined"] = True
        else:
            doc = connector.read(info)
        if doc is None:
            raise RefineError(core.L("Dit asset heeft geen transcript van Local Transcribe.",
                                     "This asset has no transcript from Local Transcribe."))
        origin = "iconik"
    media = doc.source.get("media") or ""
    if not _reachable(media):
        # Een andere Mac, een verplaatste koppeling of een opdracht van voor 2 november: via Iconik zoeken.
        try:
            client = client or core.make_client()
            media = str(core.find_media_file(client, asset_id, interactive=False)) if client else ""
        except Exception:
            media = ""
    known = {t.number: t for t in doc.tracks}
    if _reachable(media):
        duration, fps, has_video = _probe(media)
        tracks = core.list_tracks(media)
        _save_media_info(asset_id, {"duration": duration, "fps": fps, "video": has_video, "tracks": len(tracks),
                                    "name": Path(media).name})
        offline = False
    else:
        # Zonder origineel (opslag niet gekoppeld): alleen tekst bewerken, met de golfvormen uit de cache.
        media = doc.source.get("media") or ""
        duration, fps, count = _offline_shape(asset_id, doc)
        tracks, has_video, offline = [None] * count, False, True
    used = [n for n in range(1, len(tracks) + 1) if known.get(n) and known[n].used] or [1]
    session = {"asset_id": asset_id, "doc": doc, "origin": origin, "media": media, "duration": duration,
               "fps": fps, "video": has_video, "tracks": tracks, "used": used, "offline": offline,
               "labels": {n: (known[n].label if n in known else "") for n in range(1, len(tracks) + 1)}}
    with _lock:
        _sessions[asset_id] = session
    return session


def view(asset_id):
    """Wat het scherm bij het openen krijgt."""
    s = open_session(asset_id, reload=True)
    doc = s["doc"]
    return {
        "asset_id": asset_id, "title": doc.source.get("title") or asset_id, "origin": s["origin"],
        "media_name": Path(s["media"]).name, "offline": s["offline"], "duration": s["duration"], "fps": s["fps"], "video": s["video"],
        "transcript": tmodel.to_dict(doc),
        "tracks": [{"n": n, "label": s["labels"][n], "used": n in s["used"]} for n in range(1, len(s["tracks"]) + 1)],
        "chunk": CHUNK_SECONDS, "lang": core.app_language(), "draft": load_draft(asset_id),
        "refined": bool(doc.source.get("refined")), "original_kept": _original_path(asset_id).is_file(),
        "export": dict(subtitle_settings(), existing=existing_subtitles(asset_id)), "subtitles": load_subtitles(asset_id),
    }


# --- ondertitels: een eigen laag naast het transcript ---
# Ondertitels zijn er pas als iemand ze maakt (make_subtitles): het transcript verdeeld en ingepast volgens
# de regels, bewaard als laag op deze Mac (refine/<asset>.subtitles.json) en meteen als bestand geschreven,
# naast de media of op een plek die iemand in het bewaarvenster kiest. Daarna verdeelt de app niets meer
# vanzelf: verandert het transcript, dan meldt de laag dat (stale) en kiest iemand zelf voor opnieuw maken.

def _subtitles_path(asset_id):
    return DRAFTS_DIR / core.safe_name(asset_id, ".subtitles.json")


def _current_doc(asset_id):
    """Het transcript zoals het nu in Refine staat: het concept, anders het bewaarde."""
    draft = load_draft(asset_id)
    return tmodel.from_dict(draft["transcript"]) if draft else open_session(asset_id)["doc"]


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


def existing_subtitles(asset_id):
    """De namen van ondertitelbestanden die al naast de media staan (clip.srt, clip.vtt), om in te laden."""
    try:
        session = open_session(asset_id)
        if session["offline"] or not session["media"]:
            return []
        media = Path(session["media"])
        return [media.with_suffix(f".{fmt}").name for fmt in subtitles.FORMATS if media.with_suffix(f".{fmt}").is_file()]
    except (RefineError, OSError):
        return []


def _remember(**values):
    settings.update("refine.subtitles", lambda kept: kept.update(values))


def preview_subtitles(asset_id, raw):
    """De telling voor het maakvenster: hoeveel ondertitels dit worden en hoeveel er te snel zijn."""
    rules = subtitles.clean_rules(json.loads(raw or b"{}").get("rules"))
    return subtitles.preview(_current_doc(asset_id), rules)


def load_subtitles(asset_id):
    """De laag van deze Mac, of None als niemand ondertitels maakte. stale: het transcript is sindsdien veranderd."""
    try:
        data = json.loads(_subtitles_path(asset_id).read_text())
        doc = tmodel.from_dict(data["transcript"])
        target = data["target"]
        if target["formats"] not in subtitles.CHOICES or not target["base"]:
            return None
        stale = data.get("stamp") != _stamp(_current_doc(asset_id))
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


def _store(asset_id, lay, touched, rules, stamp, target, edits):
    _write_json(_subtitles_path(asset_id), {
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "edits": int(edits), "rules": rules, "stamp": stamp,
        "target": {"formats": target["formats"], "place": target["place"], "base": target["base"]},
        "touched": [bool(x) for x in touched], "transcript": tmodel.to_dict(lay)})


def _export_name(text):
    """Een bestandsnaam uit een titel: zonder tekens die in een pad iets betekenen."""
    name = "".join(" " if c in '/:\\' or ord(c) < 32 else c for c in str(text or "")).strip(" .")
    return " ".join(name.split())[:120] or "transcript"


def _choose_base(asset_id, formats, place, choose):
    """Waar de bestanden komen: (pad, door iemand zelf gekozen formaten), of (None, ()) als het bewaarvenster
    is geannuleerd. Naast de media: het mediabestand zelf, de extensie wordt die van het formaat."""
    session = open_session(asset_id)
    media = Path(session["media"]) if session["media"] else None
    reachable = media is not None and not session["offline"]
    if place == "media":
        if not reachable:
            raise RefineError(core.L("Het origineel is niet bereikbaar. Kies een andere plek, of koppel de opslag (ISG).",
                                     "The original is not reachable. Choose another place, or connect the storage (ISG)."))
        return media, ()
    if choose is None:
        raise RefineError(core.L("Een plek kiezen kan alleen vanuit het venster van de app.",
                                 "Choosing a place only works from the app's window."))
    first = subtitles.CHOICES[formats][0]
    stem = media.stem if media is not None and media.stem else _export_name(session["doc"].source.get("title") or asset_id)
    chosen = choose(str(media.parent) if reachable else None, f"{_export_name(stem)}.{first}")
    if not chosen:
        return None, ()
    path = Path(chosen)
    if path.suffix.lower() not in (".srt", ".vtt"):
        path = path.with_name(f"{path.name}.{first}")
    return path, (first,)


def _write_files(asset_id, lay, target, rules, confirmed=()):
    lay.speakers = _current_doc(asset_id).speakers     # de namen van nu, ook als ze na het maken zijn gewijzigd
    return subtitles.write_files(lay, target["base"], target["formats"], rules, core.SIDECARS_FILE, confirmed)


def _state(asset_id, files=None):
    return dict(load_subtitles(asset_id) or {}, files=files or [])


def make_subtitles(asset_id, raw, choose):
    """Maakt de ondertitels: verdeelt het transcript, past in, bewaart de laag en schrijft het bestand.
    raw: {"rules", "formats": srt | vtt | both, "place": media | other}. Vervangt een eerdere laag."""
    data = json.loads(raw or b"{}")
    rules = subtitles.clean_rules(data.get("rules"))
    formats = data.get("formats") if data.get("formats") in subtitles.CHOICES else "srt"
    place = "other" if data.get("place") == "other" else "media"
    doc = _current_doc(asset_id)
    lay = subtitles.layer(doc, rules)
    if not lay.segments:
        raise RefineError(core.L("Dit transcript heeft geen tekst.", "This transcript has no text."))
    base, confirmed = _choose_base(asset_id, formats, place, choose)
    if base is None:
        return {"cancelled": True}
    _remember(rules=rules, formats=formats, place=place)
    target = {"formats": formats, "place": place, "base": str(base)}
    _store(asset_id, lay, [False] * len(lay.segments), rules, _stamp(doc), target, 0)
    return _state(asset_id, _write_files(asset_id, lay, target, rules, confirmed))


def import_subtitles(asset_id, raw, choose):
    """Laadt een bestaand .srt- of .vtt-bestand in als de ondertitels. raw: {"rules", "name"}: de naam van een
    bestand naast de media, of leeg om er een te kiezen (choose(map) toont het venster en geeft het pad of None).
    Sprekers en woordtiming komen waar het kan uit het transcript. Bewaren schrijft daarna naar dit bestand."""
    data = json.loads(raw or b"{}")
    rules = subtitles.clean_rules(data.get("rules"))
    session = open_session(asset_id)
    media = Path(session["media"]) if session["media"] else None
    reachable = media is not None and not session["offline"]
    name = str(data.get("name") or "")
    if name:
        if name not in existing_subtitles(asset_id):
            raise RefineError(core.L("Dat bestand staat niet naast de media.", "That file is not next to the media."))
        path = media.with_name(name)
    else:
        if choose is None:
            raise RefineError(core.L("Een bestand kiezen kan alleen vanuit het venster van de app.",
                                     "Choosing a file only works from the app's window."))
        chosen = choose(str(media.parent) if reachable else None)
        if not chosen:
            return {"cancelled": True}
        path = Path(chosen)
    fmt = path.suffix.lower().lstrip(".")
    if fmt not in subtitles.FORMATS:
        raise RefineError(core.L("Alleen .srt en .vtt kunnen worden ingeladen.", "Only .srt and .vtt can be loaded."))
    try:
        if path.stat().st_size > subtitles.MAX_IMPORT:
            raise RefineError(core.L("Dat bestand is te groot voor ondertitels.", "That file is too large for subtitles."))
        found = subtitles.parse(path.read_bytes())
    except OSError as error:
        raise RefineError(core.L(f"Inladen lukte niet: {error.strerror or error}", f"Loading failed: {error.strerror or error}"))
    if not found:
        raise RefineError(core.L("In dat bestand staan geen ondertitels.", "There are no subtitles in that file."))
    doc = _current_doc(asset_id)
    lay = subtitles.import_layer(found, doc)
    beside = reachable and path.parent == media.parent and path.stem == media.stem
    target = {"formats": fmt, "place": "media" if beside else "other", "base": str(media if beside else path)}
    subtitles.adopt(path, core.SIDECARS_FILE)
    _remember(rules=rules)
    # Alles telt als met de hand gemaakt: inpassen blijft ervan af.
    _store(asset_id, lay, [True] * len(lay.segments), rules, _stamp(doc), target, 0)
    return _state(asset_id, [{"name": path.name, "path": str(path), "state": "loaded", "detail": "", "cues": len(lay.segments)}])


def save_subtitles(asset_id, raw):
    """Bewaart de laag die het scherm stuurt op deze Mac (nog niet in het bestand):
    {"transcript", "touched", "edits", "rules"}. De plek en de stempel blijven die van het maken."""
    data = json.loads(raw)
    kept = load_subtitles(asset_id)
    if kept is None:
        raise RefineError(core.L("Er zijn geen ondertitels om te bewaren.", "There are no subtitles to save."))
    lay = tmodel.from_dict(data["transcript"])        # controleert de opbouw; gooit bij onzin
    touched = list(data.get("touched") or [])[:len(lay.segments)]
    touched += [True] * (len(lay.segments) - len(touched))
    rules = subtitles.clean_rules(data.get("rules"))
    _store(asset_id, lay, touched, rules, kept["stamp"], kept["target"], int(data.get("edits") or 0))
    return load_subtitles(asset_id)["saved_at"]


def write_subtitles(asset_id, choose=None, elsewhere=False):
    """Schrijft de laag opnieuw naar zijn bestand (Bewaren), of naar een plek die iemand nu kiest."""
    kept = load_subtitles(asset_id)
    if kept is None:
        raise RefineError(core.L("Er zijn geen ondertitels om te bewaren.", "There are no subtitles to save."))
    lay, target, confirmed = tmodel.from_dict(kept["transcript"]), kept["target"], ()
    if elsewhere:
        base, confirmed = _choose_base(asset_id, target["formats"], "other", choose)
        if base is None:
            return {"cancelled": True}
        target = {"formats": target["formats"], "place": "other", "base": str(base)}
    files = _write_files(asset_id, lay, target, kept["rules"], confirmed)
    done = any(f["state"] == "written" for f in files)
    if done or elsewhere:
        _store(asset_id, lay, kept["touched"], kept["rules"], kept["stamp"], target, 0 if done else kept["edits"])
    return _state(asset_id, files)


def fit_subtitles(asset_id, raw):
    """Past de laag opnieuw in met deze regels (samenvoegen, uitloop). Wat met de hand is aangepast blijft staan."""
    kept = load_subtitles(asset_id)
    if kept is None:
        raise RefineError(core.L("Er zijn geen ondertitels om in te passen.", "There are no subtitles to fit."))
    rules = subtitles.clean_rules(json.loads(raw or b"{}").get("rules"))
    before = tmodel.from_dict(kept["transcript"])
    lay, touched = subtitles.fit_layer(before, kept["touched"], rules)
    changed = tmodel.to_dict(lay)["segments"] != tmodel.to_dict(before)["segments"]
    _remember(rules=rules)
    _store(asset_id, lay, touched, rules, kept["stamp"], kept["target"], kept["edits"] + (1 if changed else 0))
    return dict(_state(asset_id), changed=changed)


def discard_subtitles(asset_id):
    """Haalt de laag weg. Een bestand dat al geschreven is, blijft staan."""
    try:
        _subtitles_path(asset_id).unlink()
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


def chunk(asset_id, start, seconds=CHUNK_SECONDS, solo=0):
    """Een stukje van het origineel als fragmented mp4 (MPEG-4 beeld, AAC geluid)."""
    s = open_session(asset_id)
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
    return core.L("Het origineel is op deze Mac niet bereikbaar. Is de opslag (ISG) gekoppeld?",
                  "The original is not reachable on this Mac. Is the storage (ISG) connected?")


def _waves_path(asset_id):
    return core.WAVES_DIR / core.safe_name(asset_id, ".waves")


def waves(asset_id):
    """(bytes van het golfvormbestand, None) als ze er zijn, anders (None, voortgang 0 tot 1).
    Zijn ze er niet, dan begint een draad ze uit te rekenen."""
    path = _waves_path(asset_id)
    if core.read_waves(path) is not None:
        return path.read_bytes(), None
    if open_session(asset_id)["offline"]:
        raise RefineError(_offline_reason())
    with _lock:
        job = _wave_jobs.get(asset_id)
        if job is None:
            job = _wave_jobs[asset_id] = {"fraction": 0.0, "error": ""}
            threading.Thread(target=_compute_waves, args=(asset_id, job), daemon=True, name="golfvormen").start()
    if job["error"]:
        with _lock:
            _wave_jobs.pop(asset_id, None)
        raise RefineError(job["error"])
    return None, job["fraction"]


def _compute_waves(asset_id, job):
    """Leest alle sporen één keer met een lage samplefrequentie en houdt per 1/50 s de piek bij."""
    try:
        s = open_session(asset_id)
        tracks, count = s["tracks"], len(s["tracks"])
        streams = sorted({t[0] for t in tracks})
        command = ["ffmpeg", "-nostdin", "-v", "error", "-i", s["media"], "-vn"]
        if len(streams) == 1:
            command += ["-map", f"0:a:{streams[0]}"]
        else:
            inputs = "".join(f"[0:a:{i}]" for i in streams)
            command += ["-filter_complex", f"{inputs}amerge=inputs={len(streams)}[all]", "-map", "[all]"]
        command += ["-ar", str(WAVE_READ_RATE), "-f", "s16le", "-"]
        size = WAVE_READ_RATE // core.WAVE_RATE                 # samples per punt
        block = size * core.WAVE_RATE * 10 * count * 2           # 10 seconden per keer, in bytes
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
                        parts[i + 1].append(core.wave_peaks(samples[:, i].copy(), WAVE_READ_RATE))
                    done += len(samples)
                    job["fraction"] = min(done / total, 0.99)
        finally:
            process.stdout.close()
            process.wait()
        peaks = {n: b"".join(chunks) for n, chunks in parts.items()}
        if not any(peaks.values()):
            raise RefineError(core.L("Geen audio gelezen voor de golfvormen.", "No audio read for the waveforms."))
        core.write_waves(_waves_path(asset_id), peaks)
        with _lock:
            _wave_jobs.pop(asset_id, None)
    except Exception as error:
        job["error"] = str(error) or type(error).__name__


# --- concept: bewerkingen bewaren op deze Mac tot ze naar Iconik gaan (deel 3) ---

DRAFTS_DIR = core.SUPPORT_DIR / "refine"
DRAFT_LIMIT = 50 * 1024 * 1024


def _draft_path(asset_id):
    return DRAFTS_DIR / core.safe_name(asset_id, ".draft.json")


def load_draft(asset_id):
    try:
        data = json.loads(_draft_path(asset_id).read_text())
        tmodel.from_dict(data["transcript"])          # alleen een geldig transcript telt
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_draft(asset_id, raw):
    """Bewaart het concept dat het scherm stuurt: {"transcript": eigen model als dict, "edits": aantal}."""
    if not asset_id:
        raise RefineError("Geen asset.")
    data = json.loads(raw)
    doc = tmodel.from_dict(data["transcript"])        # controleert de opbouw; gooit bij onzin
    original = open_session(asset_id)["doc"]
    doc.source, doc.engine, doc.tracks = original.source, original.engine, original.tracks
    payload = {"saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "edits": int(data.get("edits") or 0),
               "transcript": tmodel.to_dict(doc)}
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _draft_path(asset_id)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False))
    temp.replace(path)
    return payload["saved_at"]


def discard_draft(asset_id):
    try:
        _draft_path(asset_id).unlink()
    except FileNotFoundError:
        pass



# --- opslaan in Iconik (deel 3) ---
# In Iconik blijft alleen de correctie staan: Iconik toont alleen het nieuwste transcript maar doorzoekt ze
# allemaal, dus een origineel ernaast zou weggecorrigeerde woorden vindbaar houden (getest 2 november).
# Het origineel blijft op deze Mac (refine/<asset>.original.json) en kan terug, tot iemand die opslag leegt.

def _original_path(asset_id):
    return DRAFTS_DIR / core.safe_name(asset_id, ".original.json")


def _complete(doc, other):
    """Vult aan wat Iconik niet bewaart (het bestand op deze Mac, de sporenanalyse) uit een ander transcript
    van hetzelfde asset."""
    for key in ("connector", "asset_id", "version_id", "title", "media"):
        if not doc.source.get(key) and other.source.get(key):
            doc.source[key] = other.source[key]
    if not doc.tracks:
        doc.tracks = list(other.tracks)


def _connector():
    client = core.make_client()
    if client is None:
        raise RefineError(core.L("Niet verbonden met Iconik.", "Not connected to Iconik."))
    import iconik
    return iconik.IconikConnector(client)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False))
    temp.replace(path)


def save_to_iconik(asset_id):
    """Zet het concept als correctie in Iconik. Het Whisper-transcript gaat daar weg en blijft op deze Mac."""
    draft = load_draft(asset_id)
    if draft is None:
        raise RefineError(core.L("Er is geen concept om op te slaan.", "There is no draft to save."))
    doc = tmodel.from_dict(draft["transcript"])
    if not any(seg.words or seg.text.strip() for seg in doc.segments):
        raise RefineError(core.L("Het transcript is leeg.", "The transcript is empty."))
    connector = _connector()
    info = connector.open(asset_id, push=True, replace=True, allow_refined=True)   # weigert een andere bron
    original = _original_path(asset_id)
    if not original.is_file():
        # Het origineel één keer bewaren: bij een tweede correctie staat het Whisper-transcript er niet meer.
        # Liefst de kopie van deze Mac (met sporen, modelrevisie en bestand); anders uit Iconik, aangevuld.
        whisper = _local_copy(asset_id)
        if whisper is None or whisper.source.get("refined"):
            whisper = connector.read(info)
            if whisper is not None:
                _complete(whisper, doc)
        if whisper is not None:
            _write_json(original, {"saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "transcript": tmodel.to_dict(whisper)})
    engine_info = {"type": "EXTERNAL", "name": core.REFINED_ENGINE, "model": doc.engine.get("model") or "",
                   "version": core.APP_VERSION}
    transcription_id, written = connector.write(info, doc, engine_info,
                                                replaces=connector.all_own_names)
    doc.source.update(transcription_id=transcription_id, refined=True)
    core.save_transcript(doc)          # de kopie op deze Mac is nu de correctie
    discard_draft(asset_id)
    with _lock:
        _sessions.pop(asset_id, None)
    print(f"Refine: correctie {transcription_id} staat op het asset ({written} regels).")
    return {"transcription_id": transcription_id, "segments": written}


def restore_original(asset_id):
    """Zet het bewaarde Whisper-transcript terug in Iconik in plaats van de correctie."""
    try:
        data = json.loads(_original_path(asset_id).read_text())
        doc = tmodel.from_dict(data["transcript"])
    except (OSError, ValueError, KeyError, TypeError):
        raise RefineError(core.L("Het origineel is niet meer op deze Mac.", "The original is no longer on this Mac."))
    connector = _connector()
    info = connector.open(asset_id, push=True, replace=True, allow_refined=True)
    engine_info = {"type": "EXTERNAL", "name": core.ENGINE_NAME, "model": doc.engine.get("model") or "",
                   "version": doc.engine.get("version") or ""}
    transcription_id, written = connector.write(info, doc, engine_info,
                                                replaces=connector.all_own_names)
    _complete(doc, open_session(asset_id)["doc"])
    doc.source.update(transcription_id=transcription_id)
    doc.source.pop("refined", None)
    core.save_transcript(doc)
    _original_path(asset_id).unlink()
    discard_draft(asset_id)
    with _lock:
        _sessions.pop(asset_id, None)
    print(f"Refine: origineel teruggezet als {transcription_id} ({written} regels).")
    return {"transcription_id": transcription_id, "segments": written}


def originals_info():
    """Hoeveel originelen van correcties er op deze Mac staan, voor de voorkeuren."""
    files = list(DRAFTS_DIR.glob("*.original.json")) if DRAFTS_DIR.is_dir() else []
    return {"count": len(files), "bytes": sum(f.stat().st_size for f in files)}


def originals_list():
    """De bewaarde originelen, nieuwste eerst, voor de lijst in Instellingen: [{"asset", "title", "connector",
    "bytes", "time"}]. "asset" is de naam van het bestand zonder achtervoegsel, zodat weghalen dezelfde vindt."""
    out = []
    for f in (DRAFTS_DIR.glob("*.original.json") if DRAFTS_DIR.is_dir() else []):
        try:
            data = json.loads(f.read_text())
            source = (data.get("transcript") or {}).get("source") or data.get("source") or {}
            stat = f.stat()
        except (OSError, ValueError, AttributeError):
            continue
        out.append({"asset": f.name[:-len(".original.json")], "title": str(source.get("title") or ""),
                    "connector": str(source.get("connector") or ""), "bytes": stat.st_size, "time": stat.st_mtime})
    return sorted(out, key=lambda item: item["time"], reverse=True)


def remove_original(asset_id):
    """Eén bewaard origineel weg (bijvoorbeeld van vertrouwelijk materiaal); de correctie zelf blijft."""
    _original_path(asset_id).unlink(missing_ok=True)


def clear_originals():
    """Gooit de bewaarde originelen weg; daarna kan een correctie niet meer terug naar het origineel."""
    for f in (DRAFTS_DIR.glob("*.original.json") if DRAFTS_DIR.is_dir() else []):
        f.unlink(missing_ok=True)



# --- alles van één asset van deze Mac ---

def forget_asset(asset_id):
    """Alles wat de app op deze Mac over dit asset bewaart weg ("Verwijder ook van deze Mac"): de kopie van het
    transcript, de golfvormen, het concept, het origineel en de ondertitellaag van Refine, en de paden van de
    ondertitelbestanden in sidecars.json. Nooit Iconik, nooit de media en nooit bestanden naast de media.
    Geeft de namen van wat weg is."""
    gone = []
    try:   # eerst de paden uit de ondertitellaag, want die gaat zo weg
        target = json.loads(_subtitles_path(asset_id).read_text()).get("target") or {}
        base = Path(target["base"]) if target.get("base") else None
    except (OSError, ValueError, AttributeError, TypeError):
        base = None
    if base is not None:
        gone += subtitles.forget([base.with_suffix(f".{fmt}") for fmt in ("srt", "vtt")], core.SIDECARS_FILE)
    for path in (core.TRANSCRIPTS_DIR / core.safe_name(asset_id, ".json"), core.WAVES_DIR / core.safe_name(asset_id, ".waves"),
                 core.WAVES_DIR / core.safe_name(asset_id, ".media.json"), _draft_path(asset_id), _original_path(asset_id),
                 _subtitles_path(asset_id)):
        try:
            path.unlink()
            gone.append(path.name)
        except FileNotFoundError:
            pass
    _marks_cache.pop(core.TRANSCRIPTS_DIR / core.safe_name(asset_id, ".json"), None)
    return gone


# --- labels in de opdrachtenlijst ---
_marks_cache = {}   # pad van de kopie -> (mtime, gecorrigeerd)


def marks(asset_id):
    """Wat de opdrachtenlijst bij een asset toont: {"refined": correctie staat in Iconik, "draft": concept op
    deze Mac, "subtitles": iemand maakte hier ondertitels}. Het venster vraagt dit elke seconde, dus de kopie
    alleen opnieuw lezen als die veranderde."""
    if not asset_id:
        return {"refined": False, "draft": False, "subtitles": False}
    copy = core.TRANSCRIPTS_DIR / core.safe_name(asset_id, ".json")
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
    return {"refined": refined, "draft": _draft_path(asset_id).is_file(), "subtitles": _subtitles_path(asset_id).is_file()}
