"""Overzicht van auto transcribe over meerdere Macs: wie deed welk asset, en is er dubbel gewerkt?

Haalt de recent gewijzigde assets met een status op, houdt die met [auto] in de details, en zet per
asset de Mac uit het detailveld naast de assets die deze Mac volgens zijn log heeft aangenomen.
Alleen lezen. Gebruikt het access token van de app en ververst het nooit zelf.

Draaien: uv run scripts/probe_cluster.py [aantal, standaard 100]
"""
import collections
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import transcribe  # noqa: E402
import worker  # noqa: E402

login = transcribe.load_login()
if not login or login["expires_at"] - time.time() < 300:
    sys.exit("Geen geldig token. Open het venster van de app even en probeer opnieuw.")
session = requests.Session()
session.headers["Authorization"] = f"Bearer {login['access_token']}"
host = transcribe.api_host()
count = int(sys.argv[1]) if len(sys.argv) > 1 else 100



class Borrowed:
    """Genoeg van transcribe.Iconik voor worker.Queue, met het geleende token."""

    def _call(self, method, path, json=None):
        response = session.request(method, f"{host}/API/{path}", json=json, timeout=60)
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
body = {"doc_types": ["assets"], "query": f"_exists_:metadata.{worker.FIELD} ",
        "sort": [{"name": "date_modified", "order": "desc"}]}
objects = iconik.post(f"search/v1/search/?per_page={count}", body).get("objects", [])

rows = []
for item in objects:
    status, info, _ = queue.read(item["id"])   # de zoekresultaten geven de metadata niet mee
    if not worker.is_auto(info):
        continue
    who = worker.busy_owner(info) or (info.split(",")[0] if "," in info else "?")
    rows.append((item["id"], status, who, (item.get("title") or "")[:50], info))

per_mac = collections.Counter(r[2] for r in rows)
print(f"{len(rows)} automatisch verwerkte assets onder de laatste {len(objects)} met een status")
for mac, n in per_mac.most_common():
    print(f"  {mac}: {n}")
print("\nStatus:", dict(collections.Counter(r[1] for r in rows)))
busy = [r for r in rows if r[1] == worker.BUSY]
for r in busy:
    print(f"  bezig: {r[2]:<12} {r[3]}")

log = Path.home() / "Library/Logs/Local Transcribe for Iconik/serve.log"
here = worker.machine_name()
taken = set(re.findall(r"Werker: asset ([0-9a-f-]{36}) aangenomen", log.read_text(errors="replace"))) if log.exists() else set()
double = [r for r in rows if r[0] in taken and r[2] != here]
print(f"\nDeze Mac ({here}) nam volgens zijn log {len(taken)} assets aan.")
if double:
    print("DUBBEL: door deze Mac aangenomen, maar in Iconik staat een andere Mac:")
    for r in double:
        print(f"  {r[0]}  {r[2]:<12} {r[3]}\n      {r[4][:160]}")
else:
    print("Geen dubbel werk gevonden: alles wat deze Mac aannam, staat in Iconik ook op naam van deze Mac.")
