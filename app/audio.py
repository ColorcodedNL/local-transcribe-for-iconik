"""Audio: sporen lezen met ffmpeg, meten, indelen (dialoog, tijdcode, stil, kopie) en de golfvorm voor Refine.
"""
import json
import mmap
import os
import subprocess
import tempfile
import time
import numpy as np

from base import L, SAMPLE_RATE, WAVES_DIR, _local, fail, print, safe_name, tell  # noqa: F401


# Spoorkeuze bij bestanden met losse audiosporen (zie analyse_tracks).
TRACK_SILENT_DB = -65.0         # gemiddeld niveau waaronder een spoor als stil telt
TIMECODE_REGULARITY = 0.85      # tijdcode: vrijwel alle pulsen in een blok op twee vaste lengtes
TIMECODE_SHARE = 0.5            # en dat in meer dan de helft van de blokken met signaal
TRACK_COPY_CORRELATION = 0.9    # vanaf deze gelijkenis dragen twee sporen hetzelfde signaal
MIC_ACTIVE_DB = -12.0           # binnen zoveel dB van de luide momenten telt als spraak
MIC_DOMINANT_DB = 6.0           # zoveel luider dan de rest telt als "dit spoor overheerst"
MIC_MIN_DOMINANCE = 0.10        # een eigen microfoon overheerst minstens dit deel van de tijd
MIC_MIN_MARGIN_DB = 3.0         # kleiner verschil in een zin: de vorige spreker aanhouden


def envelope(x, buckets=160):
    """Het verloop van het niveau van een spoor, als waarden van 0 tot 1 (-60 tot 0 dB)."""
    size = max(len(x) // buckets, 1)
    rows = x[: len(x) // size * size].reshape(-1, size)
    if not len(rows):
        return []
    db = 10 * np.log10(np.maximum((rows.astype(np.float64) ** 2).mean(axis=1), 1e-12))
    return [round(float(v), 2) for v in np.clip((db + 60) / 60, 0, 1)]


WAVE_RATE = 50   # punten per seconde in de golfvorm voor de tijdlijn van Refine
WAVES_FORMAT = "local-transcribe-waves"


def wave_peaks(x, rate=SAMPLE_RATE):
    """De golfvorm van een spoor voor de tijdlijn: per 1/50 s de piek, als 0 tot 255 (-60 tot 0 dB).
    `x` als float (-1 tot 1) of int16. Zonder kopie van het hele spoor: alleen max en min per stuk."""
    size = rate // WAVE_RATE
    count = len(x) // size
    if not count:
        return b""
    rows = x[: count * size].reshape(count, size)
    peak = np.maximum(rows.max(axis=1), -rows.min(axis=1)).astype(np.float64)
    if np.issubdtype(x.dtype, np.integer):
        peak /= 32768.0
    db = 20 * np.log10(np.maximum(peak, 1e-6))
    return np.clip((db + 60) / 60 * 255, 0, 255).astype(np.uint8).tobytes()


def write_waves(path, peaks):
    """Golfvormen per spoor naar een bestand: een regel JSON met de opbouw, daarna de bytes achter elkaar."""
    numbers = sorted(peaks)
    head = {"format": WAVES_FORMAT, "version": 1, "rate": WAVE_RATE,
            "tracks": [{"n": n, "length": len(peaks[n])} for n in numbers]}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "wb") as f:
        f.write(json.dumps(head).encode() + b"\n")
        for n in numbers:
            f.write(peaks[n])
    os.replace(temp, path)


def read_waves(path):
    """Terug uit write_waves: {spoornummer: bytes}, of None als het bestand er niet (goed) is."""
    try:
        raw = path.read_bytes()
        line, _, body = raw.partition(b"\n")
        head = json.loads(line)
        if head.get("format") != WAVES_FORMAT or head.get("rate") != WAVE_RATE:
            return None
        peaks, at = {}, 0
        for track in head["tracks"]:
            peaks[int(track["n"])] = body[at: at + int(track["length"])]
            at += int(track["length"])
        return peaks
    except (OSError, ValueError, KeyError, TypeError):
        return None


def track_info(number, x, note, used):
    # De golfvorm voor Refine meteen meenemen: de audio is hier toch al gelezen.
    if getattr(_local, "peaks", None) is not None:
        _local.peaks[number] = wave_peaks(x)
    rms = float(np.sqrt((x.astype(np.float64) ** 2).mean())) if len(x) else 0.0
    return {"n": number, "db": round(20 * np.log10(max(rms, 1e-6)), 1),
            "note": note, "used": used, "env": envelope(x)}


def list_tracks(path):
    """Alle audiosporen als (stream, kanaal, aantal kanalen in die stream)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index,channels", "-of", "json", str(path)],
        capture_output=True, text=True).stdout
    tracks = []
    for audio_index, stream in enumerate(json.loads(out or "{}").get("streams", [])):
        channels = int(stream.get("channels") or 1)
        tracks += [(audio_index, channel, channels) for channel in range(channels)]
    return tracks


def media_seconds(path):
    """Duur van het bestand volgens ffprobe, of None."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    try:
        return float(json.loads(out or "{}").get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None


PCM_BLOCK = 64 * 1024 * 1024  # bytes per extra blok als de duur onbekend of te krap is
ALIVE_EVERY = 10              # seconden tussen twee tekens van leven tijdens het lezen
PCM_READ = 1024 * 1024        # hooguit zoveel bytes per keer uit de pijp van ffmpeg


def pcm_block(size):
    """Een int16-tabel van `size` bytes in eigen geheugen (mmap). macOS geeft gewoon vrijgegeven
    grote blokken niet terug aan het systeem maar houdt ze vast voor hergebruik in het proces; dan
    bleef de ruwe audio van een lang bestand na afloop als bezet geheugen staan. Een mmap gaat bij
    het vrijgeven van de laatste verwijzing gegarandeerd terug."""
    return np.frombuffer(mmap.mmap(-1, max(size, 2)), np.int16)


def read_pcm(command, channels, seconds=None):
    """Draait ffmpeg met s16le op stdout en leest dat in blokken direct in een int16-tabel.

    Met subprocess.run(capture_output=True) stond de hele uitvoer eerst als bytes in het geheugen
    en daarna nog eens als een samengevoegde kopie: bij 12 sporen van anderhalf uur ruim 4 GB.
    Met de duur vooraf is er één tabel van de juiste grootte. Geeft (samples, returncode, stderr).

    Zolang er audio binnenkomt meldt het lezen elke ALIVE_EVERY seconden dat het nog leeft. Een groot
    bestand over een trage verbinding (40 GB over wifi) leest langer dan de waakhond geduld heeft,
    en zonder die melding lijkt dat op vastlopen. Levert ffmpeg niets meer, dan komt er ook geen melding."""
    frame = 2 * channels
    size = int((seconds + 5) * SAMPLE_RATE) * frame if seconds else PCM_BLOCK // frame * frame
    blocks, current, used = [], pcm_block(size), 0
    with tempfile.TemporaryFile() as errors:   # een bestand: een volle stderr-pijp kan ffmpeg niet laten wachten
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        try:
            view = memoryview(current).cast("B")
            alive = time.monotonic()
            while True:
                if used == len(view):
                    blocks.append(current)
                    current, used = pcm_block(PCM_BLOCK), 0
                    view = memoryview(current).cast("B")
                # readinto1: wat er nu is. Gewoon readinto wacht tot de hele tabel vol is, en bij een
                # bekende duur is dat het hele bestand: dan komt er onderweg geen teken van leven.
                read = process.stdout.readinto1(view[used:used + PCM_READ])
                if not read:
                    break
                used += read
                if time.monotonic() - alive >= ALIVE_EVERY:
                    alive = time.monotonic()
                    tell("alive")
            view.release()
        finally:
            process.stdout.close()
            code = process.wait()
        errors.seek(0)
        message = errors.read().decode(errors="replace")
    current = current[: used // 2]
    data = np.concatenate(blocks + [current]) if blocks else current
    return data[: len(data) // channels * channels], code, message


def read_track(path, track=None, seconds=None):
    """Leest een spoor (of, zonder spoor, de gewone mix) als mono 16 kHz."""
    command = ["ffmpeg", "-nostdin", "-v", "error"]
    if seconds:
        command += ["-t", str(seconds)]
    command += ["-i", str(path), "-vn"]
    if track is not None:
        audio_index, channel, channels = track
        command += ["-map", f"0:a:{audio_index}"]
        if channels > 1:
            command += ["-af", f"pan=mono|c0=c{channel}"]
    command += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    data, code, errors = read_pcm(command, 1, seconds or media_seconds(path))
    if code != 0 and not len(data):
        fail(L("ffmpeg kon geen audio lezen: ", "ffmpeg could not read audio: ") + errors[:300])
    return data.astype(np.float32) / 32768.0


def measure_track(x):
    """Kengetallen van een spoor, of None als er te weinig audio is."""
    if len(x) < SAMPLE_RATE // 2:
        return None
    frame = SAMPLE_RATE * 30 // 1000
    frames = x[: len(x) // frame * frame].reshape(-1, frame)
    levels = np.sqrt((frames ** 2).mean(axis=1))
    rms, peak = float(np.sqrt((x ** 2).mean())), float(np.abs(x).max())
    active = levels > 10 ** (-50 / 20)
    loud = 20 * np.log10(np.maximum(levels[active], 1e-9)) if active.any() else np.array([0.0])
    # Tijdcode (LTC) is een vast bitpatroon met maar twee pulslengtes; spraak en muziek
    # gaan grillig door nul. Per blok van 100 ms kijken, zodat ook een spoor waar de
    # tijdcode maar een deel van de tijd op staat herkend wordt.
    block = SAMPLE_RATE // 10
    blocks = x[: len(x) // block * block].reshape(-1, block)
    coded_candidates = np.flatnonzero(np.sqrt((blocks ** 2).mean(axis=1)) > 10 ** (-50 / 20))
    if len(coded_candidates) > 400:
        coded_candidates = coded_candidates[np.linspace(0, len(coded_candidates) - 1, 400).astype(int)]
    coded = 0
    for index in coded_candidates:
        gaps = np.diff(np.flatnonzero(np.diff(np.signbit(blocks[index]))))
        if len(gaps) > 20 and np.sort(np.bincount(gaps))[::-1][:2].sum() / len(gaps) > TIMECODE_REGULARITY:
            coded += 1
    regular = coded / len(coded_candidates) if len(coded_candidates) else 0.0
    return {
        "regelmaat": regular,                    # deel van de luide blokken met een strak pulspatroon
        "rms": 20 * np.log10(max(rms, 1e-9)),
        "piek": 20 * np.log10(max(peak, 1e-9)),
        "actief": float(active.mean()),          # deel van de tijd met geluid
        "crest": peak / max(rms, 1e-9),          # piek ten opzichte van gemiddelde
        "spreiding": float(loud.std()),          # hoe sterk het niveau wisselt (dB)
    }


def classify_track(d):
    if d is None or d["rms"] < TRACK_SILENT_DB:
        return "stil"
    if d["regelmaat"] > TIMECODE_SHARE:
        return "tijdcode of toon"
    if d["actief"] > 0.97 and d["spreiding"] < 2.0 and d["crest"] < 3.0:
        return "constant signaal"  # bijvoorbeeld een testtoon
    if d["actief"] < 0.03:
        return "vrijwel stil"
    return "geluid"


def read_all_tracks(path, tracks):
    """Leest alle sporen in een keer: een tabel (samples, sporen) in 16 kHz."""
    streams = sorted({track[0] for track in tracks})
    command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn"]
    if len(streams) == 1:
        command += ["-map", f"0:a:{streams[0]}"]
    else:
        inputs = "".join(f"[0:a:{index}]" for index in streams)
        command += ["-filter_complex", f"{inputs}amerge=inputs={len(streams)}[all]", "-map", "[all]"]
    command += ["-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    data, code, errors = read_pcm(command, len(tracks), media_seconds(path))
    if code != 0 and not len(data):
        fail(L("ffmpeg kon de sporen niet lezen: ", "ffmpeg could not read the tracks: ") + errors[:300])
    return data[: len(data) // len(tracks) * len(tracks)].reshape(-1, len(tracks))


SIMILARITY_STEP = 16  # 1 kHz uit 16 kHz


def similarity(a, b, step=SIMILARITY_STEP):
    """Hoe sterk twee sporen op elkaar lijken (absolute correlatie, 0 tot 1), op elk 16e sample (1 kHz).
    Voor "is dit dezelfde microfoon" is dat genoeg, en het kost een paar MB in plaats van bijna 2 GB
    per paar per uur audio met np.corrcoef op de volle sporen."""
    x = a[::step].astype(np.float64)
    y = b[::step].astype(np.float64)
    x -= x.mean()
    y -= y.mean()
    scale = float(np.sqrt((x @ x) * (y @ y)))
    return abs(float(x @ y)) / scale if scale > 0 else 0.0


def analyse_tracks(data, mix_same_source=False):
    """Bepaalt welke sporen gebruikt worden. Geeft (samples, microfoons) terug.

    `microfoons` is None, of bij twee of meer eigen microfoons een tabel met per
    100 ms het niveau van elke microfoon, om per zin de spreker uit af te lezen.
    Met `mix_same_source` (stereo) worden kanalen met dezelfde bron gemixt in
    plaats van dat er een gekozen wordt.
    """
    count = data.shape[1]

    def floats(i):
        # Een spoor als float32, pas op het moment dat het nodig is. Alle sporen tegelijk
        # omzetten kostte bij 12 sporen van anderhalf uur ruim 4 GB, ook voor stille sporen.
        return data[:, i].astype(np.float32) / 32768.0

    # Eén spoor tegelijk: meten, en van de bruikbare alleen bewaren wat later nodig is
    # (een uitgedunde kopie voor de vergelijking en het niveau per 100 ms).
    frame = SAMPLE_RATE // 10
    frames = len(data) // frame
    measured, kinds, infos, thin, power_of = [], [], [], {}, {}
    for i in range(count):
        x = floats(i)
        measured.append(measure_track(x))
        kinds.append(classify_track(measured[-1]))
        infos.append(track_info(i + 1, x, "", False))
        if kinds[-1] == "geluid":
            thin[i] = x[::SIMILARITY_STEP].copy()
            power_of[i] = (x[: frames * frame].reshape(frames, frame) ** 2).mean(axis=1)
        del x
    usable = [i for i in range(count) if kinds[i] == "geluid"]

    # Sporen die (vrijwel) hetzelfde signaal dragen horen bij elkaar: een microfoon
    # die dubbel is opgenomen. Per groep blijft het beste spoor over.
    group = {i: i for i in usable}
    for a in usable:
        for b in usable:
            if a < b and similarity(thin[a], thin[b], step=1) > TRACK_COPY_CORRELATION:
                root_a, root_b = group[a], group[b]
                for key in group:
                    if group[key] == root_b:
                        group[key] = root_a
    clipped = {i: int((np.abs(data[:, i].astype(np.int32)) >= 32700).sum()) for i in usable}
    chosen = {}
    for i in usable:
        best = chosen.get(group[i])
        if best is None or (clipped[i], -measured[i]["rms"]) < (clipped[best], -measured[best]["rms"]):
            chosen[group[i]] = i
    mics = sorted(chosen.values())

    notes = {i: kinds[i] for i in range(count)}
    for i in usable:
        if i not in mics:
            notes[i] = f"zelfde signaal als spoor {chosen[group[i]] + 1}"
    tell("tracks", tracks=[{**infos[i], "note": notes[i], "used": i in mics} for i in range(count)])
    if not mics:
        for i in range(count):
            print(f"  spoor {i + 1}: {notes[i]}")
        fail(L("Geen spoor met geluid gevonden in dit bestand.", "No track with audio found in this file."), skip=True)

    # Niveau per 100 ms, per spoor gelijkgetrokken op zijn eigen luide momenten.
    power = np.array([power_of[i] for i in mics]) if frames else np.zeros((len(mics), 0))
    level = 10 * np.log10(np.maximum(power, 1e-12))
    reference = np.array([np.percentile(row, 90) if len(row) else 0.0 for row in level])
    relative = level - reference[:, None]

    dominant = np.zeros(len(mics))
    if len(mics) > 1 and frames:
        # Alleen momenten waarop ergens echt gesproken wordt, niet de pauzes met ruis.
        active = relative.max(axis=0) > MIC_ACTIVE_DB
        for k in range(len(mics)):
            others = np.delete(relative, k, axis=0).max(axis=0)
            dominant[k] = float(((relative[k] - others) > MIC_DOMINANT_DB)[active].mean()) if active.any() else 0.0
    own = [k for k in range(len(mics)) if dominant[k] >= MIC_MIN_DOMINANCE]

    for i in range(count):
        extra = ""
        if i in mics and len(mics) > 1 and dominant[mics.index(i)] >= 0.005:   # 0% zegt niets, en leest als een tegenspraak
            extra = f"  overheerst {dominant[mics.index(i)]:4.0%} van de tijd"
        level_text = f"{measured[i]['rms']:6.1f} dB" if measured[i] else "     leeg"
        print(f"  spoor {i + 1}: {level_text}  {notes[i]}{extra}")

    if len(own) >= 2:
        # Elk van deze sporen is geregeld duidelijk het luidst: eigen microfoons.
        gains = 10 ** ((reference[own].mean() - reference[own]) / 20)
        mix = sum(floats(mics[k]) * gain for k, gain in zip(own, gains))
        peak = float(np.abs(mix).max()) if len(mix) else 0.0
        if peak > 0.99:
            mix = mix * (0.99 / peak)
        print("  gebruikt: sporen " + " + ".join(str(mics[k] + 1) for k in own)
              + " samen; de spreker volgt uit het luidste spoor per zin")
        return mix.astype(np.float32), {"relative": relative[own], "tracks": [mics[k] + 1 for k in own]}

    if len(own) < 2 and mix_same_source and len(mics) > 1:
        print("  gebruikt: sporen " + " + ".join(str(i + 1) for i in mics) + " gemixt")
        return (sum(floats(i) for i in mics) / len(mics)).astype(np.float32), None
    if own:
        best = mics[own[0]]
    else:
        # Geen spoor overheerst: dezelfde bron op verschillende microfoons. Een microfoon
        # dicht op de spreker heeft het grootste verschil tussen spraak en pauzes.
        contrast = [float(np.percentile(row, 90) - np.percentile(row, 10)) if len(row) else 0.0
                    for row in level]
        best = mics[int(np.argmax(contrast))]
        if len(mics) > 1:
            print("  Meerdere sporen met dezelfde bron; gekozen is het spoor met de helderste spraak.")
    print(f"  gebruikt: spoor {best + 1}")
    return floats(best), None


def load_audio_tracks(path, track_number=None):
    """Audio als mono 16 kHz plus, bij eigen microfoons per spreker, de niveaus per spoor.

    Bij meer dan een spoor of kanaal wordt gekeken wat er op elk staat: stilte en
    tijdcode vallen af, eigen microfoons worden samengevoegd, een gewone
    stereomix wordt gemixt.
    """
    tracks = list_tracks(path)
    if not tracks:
        fail(L("Dit bestand heeft geen audio.", "This file has no audio."), skip=True)
    if track_number is not None:
        if not 1 <= track_number <= len(tracks):
            fail(L(f"Spoor {track_number} bestaat niet; dit bestand heeft {len(tracks)} sporen.",
                   f"Track {track_number} does not exist; this file has {len(tracks)} tracks."))
        print(f"  gebruikt: spoor {track_number}")
        samples = read_track(path, tracks[track_number - 1])
        tell("tracks", tracks=[track_info(track_number, samples, "gekozen spoor", True)])
        return samples, None
    if len(tracks) == 1:
        samples = read_track(path)
        tell("tracks", tracks=[track_info(1, samples, "geluid", True)])
        return samples, None
    print(f"Sporen:  {len(tracks)}")
    # Stereo is meestal een gewone mix, maar er kan ook tijdcode of een eigen
    # microfoon per kanaal op staan. Daarom altijd eerst kijken wat erop staat.
    stereo = len({t[0] for t in tracks}) == 1 and len(tracks) == 2
    return analyse_tracks(read_all_tracks(path, tracks), mix_same_source=stereo)


def save_waves(asset_id, peaks):
    """Bewaart de golfvormen van de sporen voor de tijdlijn van Refine. Mag nooit een opdracht laten mislukken."""
    if not peaks:
        return
    try:
        write_waves(WAVES_DIR / safe_name(asset_id, ".waves"), peaks)
    except Exception as error:
        print(f"Golfvormen bewaren lukte niet: {error}")
