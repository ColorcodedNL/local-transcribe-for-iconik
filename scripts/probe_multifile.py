"""Proef: hoeveel assets in Iconik hebben meer dan een mediabestand, en zitten er P2/Avid/OP-Atom-achtige paden tussen?

Achtergrond: find_media_file() pakt bij meer dan een mediabestand het eerste. Bij OP-Atom (P2, Avid
MediaFiles) staat audio in losse MXF's naast de video. Deze proef telt of dat in dit archief voorkomt.

Alleen lezen. Gebruikt het access token van de app uit de sleutelhanger en ververst het nooit zelf:
een ververstoken mag maar een keer gebruikt worden, en dat doet de app.

Draaien: uv run scripts/probe_multifile.py [--max N] [--storage STORAGE_ID]
  --max N        stop na N assets (standaard: alles)
  --storage ID   alleen assets met een bestand op deze opslag
  --before DATE  alleen assets van voor deze date_created (verder gaan na een onderbreking)
"""
import argparse
import collections
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import transcribe  # noqa: E402

PER_PAGE = 100
OPATOM_PATH = re.compile(r"(CONTENTS/(VIDEO|AUDIO)|Avid MediaFiles|OMFI MediaFiles)", re.I)

parser = argparse.ArgumentParser()
parser.add_argument("--max", type=int, default=0)
parser.add_argument("--storage", default="")
parser.add_argument("--before", default="", help="alleen assets van voor deze date_created (om verder te gaan)")
args = parser.parse_args()

login = transcribe.load_login()
if not login:
    sys.exit("Niet ingelogd. Log eerst in via de app.")
host = transcribe.api_host()
session = requests.Session()
session.headers["Authorization"] = f"Bearer {login['access_token']}"


def api(method, path, body=None):
    global login
    if login["expires_at"] - time.time() < 120:
        # Misschien heeft de app het token intussen ververst: opnieuw uit de sleutelhanger lezen.
        login = transcribe.load_login() or login
        session.headers["Authorization"] = f"Bearer {login['access_token']}"
    if login["expires_at"] - time.time() < 120:
        raise SystemExit("\nHet token verloopt bijna. Open de app even (die ververst het) en draai opnieuw, "
                         "eventueel met --max om het in delen te doen.")
    for attempt in range(4):
        try:
            response = session.request(method, f"{host}/API/{path}", json=body, timeout=60)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))
            continue
        if response.status_code < 500 or attempt == 3:
            break
        time.sleep(5 * (attempt + 1))   # 502/503/504 van Iconik: even wachten en opnieuw
    if not response.ok:
        raise RuntimeError(f"{method} {path}: HTTP {response.status_code}: {response.text[:200]}")
    return response.json() if response.content else {}


def media_files(asset_id):
    files = api("GET", f"files/v1/assets/{asset_id}/files/?per_page=100").get("objects", [])
    return [f for f in files
            if f.get("type") == "FILE"
            and Path(f.get("name", "")).suffix.lower() not in transcribe.SIDECAR_EXTENSIONS]


terms = [{"name": "files.storage_id", "value": args.storage}] if args.storage else []
seen = 0
counts = collections.Counter()            # aantal mediabestanden per asset
combos = collections.Counter()            # extensies bij assets met meer dan een bestand
formats = collections.Counter()           # verschillende formats/file sets bij meer dan een bestand
opatom_single = []                        # een bestand, maar een P2/Avid-achtig pad
examples = []                             # voorbeelden van assets met meer dan een bestand
mxf_examples = []                         # assets met meer dan een bestand waarvan minstens een MXF
oldest = None                             # date_created van het oudste asset tot nu toe
# De search-API geeft maximaal 10.000 resultaten (100 pagina's van 100) per zoekopdracht. Daarom per
# tijdvak: nieuwste eerst, en na 100 pagina's verder met alles van voor het oudste asset tot dan toe.
seen_ids = set()
before = args.before
stopped = None
done = False
try:
    while not done:
        query = f'date_created:[* TO "{before}"] ' if before else ""
        page, new_in_window, window_end = 1, 0, False
        while True:
            body = {"doc_types": ["assets"], "query": query,
                    "sort": [{"name": "date_created", "order": "desc"}]}
            if terms:
                body["filter"] = {"operator": "AND", "terms": terms}
            result = api("POST", f"search/v1/search/?per_page={PER_PAGE}&page={page}", body)
            objects = result.get("objects", [])
            if not objects:
                window_end = True
                break
            for item in objects:
                asset_id = item.get("id")
                if item.get("date_created"):
                    oldest = item["date_created"]
                if not asset_id or asset_id in seen_ids:
                    continue
                seen_ids.add(asset_id)
                new_in_window += 1
                try:
                    media = media_files(asset_id)
                except RuntimeError as error:
                    print(f"\n  overgeslagen {asset_id}: {error}")
                    continue
                seen += 1
                counts[min(len(media), 5)] += 1
                paths = [f"{f.get('directory_path', '')}/{f.get('name', '')}" for f in media]
                if len(media) > 1:
                    combos[" + ".join(sorted(Path(f.get("name", "")).suffix.lower() or "(geen)" for f in media))] += 1
                    formats[f"{len({f.get('format_id') for f in media})} format(s), "
                            f"{len({f.get('file_set_id') for f in media})} file set(s)"] += 1
                    if len(examples) < 10:
                        examples.append((asset_id, (item.get("title") or "")[:50], paths[:6]))
                    if any(Path(f.get("name", "")).suffix.lower() == ".mxf" for f in media) and len(mxf_examples) < 15:
                        mxf_examples.append((asset_id, (item.get("title") or "")[:50], paths[:6]))
                elif len(media) == 1 and OPATOM_PATH.search(paths[0]):
                    opatom_single.append((asset_id, (item.get("title") or "")[:50], paths[0]))
                if seen % 50 == 0:
                    print(f"\r{seen} assets bekeken (tot {str(oldest)[:10]})...", end="", flush=True)
                if args.max and seen >= args.max:
                    done = True
                    break
            pages = int(result.get("pages") or page)
            if done or page >= pages:
                window_end = page >= pages
                break
            if page >= 100:
                break
            page += 1
        if done or window_end or new_in_window == 0 or not oldest:
            done = True
        else:
            before = oldest
except KeyboardInterrupt:
    stopped = "Afgebroken."
except (RuntimeError, SystemExit) as error:
    stopped = str(error).strip()

print(f"\r{seen} assets bekeken.          \n")
print("Mediabestanden per asset (sidecars en mappen niet meegeteld):")
for n in sorted(counts):
    label = "5 of meer" if n == 5 else str(n)
    print(f"  {label:>9}: {counts[n]}")
if combos:
    print("\nExtensies bij assets met meer dan een mediabestand:")
    for combo, n in combos.most_common(15):
        print(f"  {n:>5}  {combo}")
    print("\nFormats en file sets bij die assets (1 file set = echt samen een clip; meer = bv. origineel plus een andere versie):")
    for combo, n in formats.most_common():
        print(f"  {n:>5}  {combo}")
    print("\nVoorbeelden:")
    for asset_id, title, paths in examples:
        print(f"  {asset_id}  {title}")
        for p in paths:
            print(f"      {p}")
if mxf_examples:
    print("\nAssets met meer dan een bestand en minstens een MXF:")
    for asset_id, title, paths in mxf_examples:
        print(f"  {asset_id}  {title}")
        for p in paths:
            print(f"      {p}")
print(f"\nAssets met een bestand op een P2/Avid-achtig pad (mogelijk losse clips per spoor): {len(opatom_single)}")
for asset_id, title, path in opatom_single[:10]:
    print(f"  {asset_id}  {title}\n      {path}")
if stopped:
    print(f"\nGestopt: {stopped}")
    if oldest:
        print(f"Verder gaan: uv run scripts/probe_multifile.py --before \"{oldest}\"")
