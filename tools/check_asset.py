#!/usr/bin/env python3
"""Controle: wat staat er in Iconik, vergeleken met de laatste proefrun-payload.

Gebruik:
    uv run tools/check_asset.py <asset_id> [payload.iconik.json]
Leest alleen, schrijft niets.
"""

import json
import os
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from transcribe import ENGINE_NAME, Iconik, find_version


def main():
    asset_id = sys.argv[1]
    payload_file = sys.argv[2] if len(sys.argv) > 2 else f"{asset_id}.iconik.json"
    iconik = Iconik(os.environ["ICONIK_APP_ID"], os.environ["ICONIK_TOKEN"])
    version_id = find_version(iconik.get(f"assets/v1/assets/{asset_id}/"))

    transcripts = iconik.get(
        f"assets/v1/assets/{asset_id}/versions/{version_id}/transcriptions/properties/"
    ).get("objects", [])
    print(f"Transcripten op het asset: {len(transcripts)}")
    for item in transcripts:
        engine = (item.get("engine_info") or {}).get("name")
        print(f"  {item['id']}  engine={engine}  sprekers={item.get('speaker_labels')}")

    own = [t for t in transcripts if (t.get("engine_info") or {}).get("name") == ENGINE_NAME]
    if not own:
        sys.exit("Geen transcript van dit script gevonden.")
    transcription_id = own[-1]["id"]

    found, page = [], 1
    while True:
        data = iconik.get(
            f"assets/v1/assets/{asset_id}/segments/?segment_type=TRANSCRIPTION"
            f"&transcription_id={transcription_id}&per_page=100&page={page}")
        objects = data.get("objects", [])
        found += [o for o in objects if o.get("transcription_id") == transcription_id]
        if page == 1:
            print("Paginering volgens Iconik:",
                  {k: data.get(k) for k in ("total", "pages", "per_page") if k in data})
        if len(objects) < 100:
            break
        page += 1
    print(f"Segmenten in Iconik bij transcript {transcription_id}: {len(found)}")

    if not os.path.exists(payload_file):
        return
    expected = json.load(open(payload_file))["segments"]
    starts = {o.get("time_start_milliseconds") for o in found}
    missing = [s for s in expected if s["time_start_milliseconds"] not in starts]
    print(f"Verwacht volgens {payload_file}: {len(expected)}, ontbreekt in Iconik: {len(missing)}")
    for segment in missing[:40]:
        print(f"  {segment['time_start_milliseconds'] / 1000:7.2f}s  "
              f"S{segment['transcription']['speaker'] + 1}  {segment['segment_text']}")


if __name__ == "__main__":
    main()
