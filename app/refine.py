"""Refine: alles wat het scherm van de editor nodig heeft, behalve het scherm zelf (refine.html).

Per asset een sessie in het geheugen met het transcript (eigen model), het mediabestand en de sporen.
Het transcript komt uit de kopie op deze Mac (transcripts/), anders uit Iconik via de connector.

Het venster speelt het origineel af in stukjes die ffmpeg tijdens het kijken omzet naar MPEG-4 in
fragmented mp4 (proef van 2 november: WebKit speelt dat via Media Source Extensions, zonder libmpv).
De audio is de mix van de gebruikte sporen, of één spoor als iemand dat wil horen.

De golfvormen per spoor (50 punten per seconde) komen uit het bestand dat een opdracht bewaart
(waves/); ontbreekt dat, dan rekent een draad ze hier uit en houdt de voortgang bij.
"""
import json
import subprocess
import threading
from pathlib import Path

import numpy as np

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
    if not _reachable(media):
        raise RefineError(core.L("Het origineel is op deze Mac niet bereikbaar. Is de opslag (ISG) gekoppeld?",
                                 "The original is not reachable on this Mac. Is the storage (ISG) connected?"))
    duration, fps, has_video = _probe(media)
    tracks = core.list_tracks(media)
    known = {t.number: t for t in doc.tracks}
    used = [n for n in range(1, len(tracks) + 1) if known.get(n) and known[n].used] or [1]
    session = {"asset_id": asset_id, "doc": doc, "origin": origin, "media": media, "duration": duration,
               "fps": fps, "video": has_video, "tracks": tracks, "used": used,
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
        "media_name": Path(s["media"]).name, "duration": s["duration"], "fps": s["fps"], "video": s["video"],
        "transcript": tmodel.to_dict(doc),
        "tracks": [{"n": n, "label": s["labels"][n], "used": n in s["used"]} for n in range(1, len(s["tracks"]) + 1)],
        "chunk": CHUNK_SECONDS, "lang": core.app_language(),
    }


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

def _waves_path(asset_id):
    return core.WAVES_DIR / core.safe_name(asset_id, ".waves")


def waves(asset_id):
    """(bytes van het golfvormbestand, None) als ze er zijn, anders (None, voortgang 0 tot 1).
    Zijn ze er niet, dan begint een draad ze uit te rekenen."""
    path = _waves_path(asset_id)
    if core.read_waves(path) is not None:
        return path.read_bytes(), None
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
