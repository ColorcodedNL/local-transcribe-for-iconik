"""Proef voor auto transcribe tegen het echte Iconik.

1. Queue.candidates() met mediatype en opslag samen als filter (alleen lezen).
2. Met --clear: een status leegmaken op één asset. Het script kiest de nieuwste kandidaat, vraagt om
   bevestiging, zet de status op Fout (geen Mac pakt dat op), maakt hem weer leeg en kijkt of de
   zoekopdracht het asset daarna weer vindt. Het asset eindigt zoals het begon: zonder status. Het
   detailveld krijgt een korte testregel.

Gebruikt het access token van de app uit de sleutelhanger en ververst het nooit zelf.
Draaien: uv run scripts/probe_auto.py [--clear]
"""
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import transcribe  # noqa: E402
import worker  # noqa: E402

login = transcribe.load_login()
if not login:
    sys.exit("Niet ingelogd. Log eerst in via de app.")
left = login["expires_at"] - time.time()
if left < 300:
    sys.exit(f"Het token verloopt over {int(left)} s. Open de app even (die ververst het) en probeer opnieuw.")


class Borrowed:
    """Genoeg van transcribe.Iconik voor worker.Queue, met het geleende token."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {login['access_token']}"
        self.host = transcribe.api_host()

    def _call(self, method, path, json=None):
        response = self.session.request(method, f"{self.host}/API/{path}", json=json, timeout=60)
        if not response.ok:
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else {}

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body):
        return self._call("POST", path, body)


iconik = Borrowed()
queue = worker.Queue(iconik)
queue.setup(create=False)
storages = list((transcribe.load_config().get("mounts") or {}).keys())
print(f"Opslag die deze Mac kent: {len(storages)}")

for audio in (False, True):
    found = queue.candidates(storages, audio=audio, limit=5)
    print(f"\nKandidaten ({'video en audio' if audio else 'video'}):")
    for asset_id in found:
        asset = iconik.get(f"assets/v1/assets/{asset_id}/")
        status, info, _ = queue.read(asset_id)
        print(f"  {asset_id}  status={status!r:<6} {(asset.get('title') or '')[:55]}")

if "--clear" not in sys.argv:
    sys.exit(0)

target = queue.candidates(storages, limit=1)[0]
title = iconik.get(f"assets/v1/assets/{target}/").get("title")
answer = input(f"\nTest op '{title}' ({target}): status even op Fout en weer leeg. Doorgaan? [j/N] ")
if answer.strip().lower() not in ("j", "ja", "y", "yes"):
    sys.exit("Niets gedaan.")



def wait(want_found, label):
    start = time.time()
    while time.time() - start < 120:
        if (target in queue.candidates(storages, limit=50)) == want_found:
            print(f"{label} na {time.time() - start:.0f} s.")
            return True
        time.sleep(5)
    print(f"{label}: na 120 s nog niet.")
    return False


queue.write(target, worker.FAILED, "Test van Local Transcribe (status leegmaken), kan genegeerd worden.")
print("Status gezet:  ", queue.read(target)[0])
# Eerst wachten tot de zoekindex de status ziet, anders bewijst "weer gevonden" straks niets.
wait(False, "Uit de kandidaten verdwenen")
queue.write(target, worker.CLEAR)
status = queue.read(target)[0]
print("Na leegmaken:  ", repr(status), "(goed)" if status is None else "(NIET LEEG)")

wait(True, "Weer gevonden als kandidaat")
