"""Zet assets in de rij voor een werker-Mac (status "aangevraagd"), zonder de Iconik-weergave.

Gebruik: cd ~/iconik-transcribe && uv run tools/request_transcript.py <asset_id> [<asset_id> ...]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import transcribe  # noqa: E402
import worker  # noqa: E402


def main():
    ids = sys.argv[1:]
    if not ids:
        print(__doc__)
        return
    iconik = transcribe.make_client()
    if iconik is None:
        print("Niet ingelogd. Log eerst in via de app.")
        return
    queue = worker.Queue(iconik)
    queue.setup()
    for asset_id in ids:
        queue.write(asset_id, worker.REQUESTED, f"{worker.stamp()}: aangevraagd met request_transcript.py")
        print("aangevraagd:", asset_id)


if __name__ == "__main__":
    main()
