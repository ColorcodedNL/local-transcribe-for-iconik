#!/usr/bin/env python3
"""Meet per audiospoor wat erop staat: stilte, spraak, of een constant signaal zoals tijdcode.

Leest alleen. Gebruik:
    uv run tools/probe_tracks.py "<map, bestand of asset-ID>" > sporen.txt
Bekijkt per bestand de eerste 60 seconden van elk audiospoor.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

RATE = 16000
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 60
MEDIA = {".mxf", ".mov", ".mp4", ".mts", ".m4v", ".wav", ".avi", ".mkv"}


def streams(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index,channels", "-of", "json", str(path)],
        capture_output=True, text=True).stdout
    return json.loads(out or "{}").get("streams", [])


def read(path, audio_index, channel, channels):
    pan = [] if channels == 1 else ["-af", f"pan=mono|c0=c{channel}"]
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-t", str(SECONDS), "-i", str(path),
         "-map", f"0:a:{audio_index}", *pan, "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def db(value):
    return 20 * np.log10(max(float(value), 1e-9))


def describe(x):
    if len(x) < RATE // 2:
        return None
    frame = RATE * 30 // 1000
    frames = x[: len(x) // frame * frame].reshape(-1, frame)
    levels = np.sqrt((frames ** 2).mean(axis=1))
    rms, peak = np.sqrt((x ** 2).mean()), np.abs(x).max()
    active = levels > 10 ** (-50 / 20)
    crossings = float((np.diff(np.signbit(x)) != 0).mean() * RATE)
    loud = 20 * np.log10(np.maximum(levels[active], 1e-9)) if active.any() else np.array([0.0])
    # Regelmaat van de nuldoorgangen: tijdcode is een strak blokpatroon, spraak niet.
    crossing_at = np.flatnonzero(np.diff(np.signbit(x)))
    gaps = np.diff(crossing_at)
    if len(gaps) > 50:
        counts = np.bincount(gaps)
        order = np.argsort(counts)[::-1][:2]
        top_share = float(counts[order].sum() / len(gaps))
        top_gaps = sorted(int(v) for v in order)
    else:
        top_share, top_gaps = 0.0, []
    big = RATE // 10
    per = np.diff(np.signbit(x[: len(x) // big * big]).reshape(-1, big), axis=1).sum(axis=1)
    loud_big = np.sqrt((x[: len(x) // big * big].reshape(-1, big) ** 2).mean(axis=1)) > 10 ** (-50 / 20)
    per = per[loud_big]
    steadiness = float(per.std() / per.mean()) if len(per) > 3 and per.mean() > 0 else 9.9
    return {
        "top_share": top_share, "top_gaps": top_gaps, "steadiness": steadiness,
        "rms": db(rms), "peak": db(peak),
        "actief": float(active.mean()),              # deel van de tijd met geluid
        "crest": float(peak / max(rms, 1e-9)),       # piek ten opzichte van gemiddelde
        "spreiding": float(loud.std()),              # hoe sterk het niveau wisselt (dB)
        "nuldoorgangen": crossings,
    }


def guess(d):
    if d is None or d["rms"] < -65:
        return "stil"
    if d["actief"] > 0.97 and d["spreiding"] < 2.0 and d["crest"] < 3.0:
        return "constant signaal (tijdcode?)"
    if d["actief"] < 0.03:
        return "vrijwel stil"
    return "geluid"


def main():
    argument = sys.argv[1]
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", argument):
        # Een asset-ID: het pad opzoeken in Iconik.
        import os
        from transcribe import Iconik, find_media_file
        iconik = Iconik(os.environ["ICONIK_APP_ID"], os.environ["ICONIK_TOKEN"])
        argument = str(find_media_file(iconik, argument, None))
        print(f"Bestand: {argument}")
    target = Path(argument)
    files = [target] if target.is_file() else sorted(
        p for p in target.iterdir() if p.suffix.lower() in MEDIA and not p.name.startswith("."))
    for path in files:
        print(f"\n{path.name}")
        tracks = []
        for audio_index, stream in enumerate(streams(path)):
            for channel in range(stream.get("channels", 1)):
                x = read(path, audio_index, channel, stream.get("channels", 1))
                tracks.append((f"a{audio_index + 1}" + (f".{channel + 1}" if stream.get("channels", 1) > 1 else ""), x))
        if not tracks:
            print("  geen audio")
            continue
        base = tracks[0][1]
        for name, x in tracks:
            d = describe(x)
            if d is None:
                print(f"  {name:<5} te kort of leeg")
                continue
            n = min(len(x), len(base))
            same = float(np.corrcoef(x[:n], base[:n])[0, 1]) if n and x[:n].std() > 0 and base[:n].std() > 0 else 0.0
            print(f"  {name:<5} rms {d['rms']:6.1f} dB  piek {d['peak']:6.1f}  actief {d['actief']:4.0%}  "
                  f"crest {d['crest']:5.1f}  spreiding {d['spreiding']:4.1f}  nuldoorg {d['nuldoorgangen']:6.0f}/s  "
                  f"gelijk aan spoor 1: {same:5.2f}  -> {guess(d)}")
            print(f"        nuldoorgangen: wisseling per 100 ms {d['steadiness']:5.2f}  "
                  f"({d['top_share']:4.0%} van de afstanden is {d['top_gaps']} samples)")
        compare(tracks)


def compare(tracks):
    """Onderlinge gelijkenis van alle sporen, en per paar hoe het niveau zich verhoudt."""
    names = [name for name, _ in tracks]
    n = min(len(x) for _, x in tracks)
    if n < RATE:
        return
    data = [x[:n] for _, x in tracks]
    print("  gelijkenis    " + " ".join(f"{name:>6}" for name in names))
    for i, name in enumerate(names):
        row = []
        for j in range(len(names)):
            ok = data[i].std() > 0 and data[j].std() > 0
            row.append(float(np.corrcoef(data[i], data[j])[0, 1]) if ok else 0.0)
        print(f"  {name:<12}  " + " ".join(f"{value:6.2f}" for value in row))
    frame = RATE // 10  # 100 ms
    levels = []
    for x in data:
        frames = x[: n // frame * frame].reshape(-1, frame)
        levels.append(20 * np.log10(np.maximum(np.sqrt((frames ** 2).mean(axis=1)), 1e-6)))
    print("  niveauverschil per 100 ms (dB), alleen waar een van beide boven -45 dB zit:")
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            active = (levels[i] > -45) | (levels[j] > -45)
            if active.sum() < 20:
                continue
            diff = (levels[i] - levels[j])[active]
            q = np.percentile(diff, [5, 25, 50, 75, 95])
            print(f"    {names[i]} - {names[j]}: p5 {q[0]:6.1f}  p25 {q[1]:6.1f}  mediaan {q[2]:6.1f}  "
                  f"p75 {q[3]:6.1f}  p95 {q[4]:6.1f}   |verschil|>10 dB: {float((np.abs(diff) > 10).mean()):4.0%}")


if __name__ == "__main__":
    main()
