"""Proef voor auto transcribe: welke Iconik-zoekopdrachten vinden de kandidaten?

Alleen lezen. Gebruikt het access token van de app uit de sleutelhanger en ververst
het nooit zelf: een ververstoken mag maar een keer gebruikt worden, en dat doet de app.

Draaien: uv run scripts/probe_search.py [--lengths]
"""
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import transcribe  # noqa: E402

FIELD = "metadata.local_transcribe_status"

login = transcribe.load_login()
if not login:
    sys.exit("Niet ingelogd. Log eerst in via de app.")
left = login["expires_at"] - time.time()
if left < 300:
    sys.exit(f"Het token verloopt over {int(left)} s. Open de app even (die ververst het) en probeer opnieuw.")

host = transcribe.api_host()
session = requests.Session()
session.headers["Authorization"] = f"Bearer {login['access_token']}"


def api(method, path, body=None):
    response = session.request(method, f"{host}/API/{path}", json=body, timeout=60)
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
    return response.json() if response.content else {}


def search(query, terms=None, per_page=3):
    body = {"doc_types": ["assets"], "query": query}
    if terms:
        body["filter"] = {"operator": "AND", "terms": terms}
    return api("POST", f"search/v1/search/?per_page={per_page}", body)


def show(label, query, terms=None):
    try:
        result = search(query, terms)
    except RuntimeError as error:
        print(f"{label:<44} FOUT {error}")
        return None, []
    total = result.get("total")
    print(f"{label:<44} {total!s:>7}   query={query!r}" + (f" filter={terms}" if terms else ""))
    return total, [o.get("id") for o in result.get("objects", []) if o.get("id")]


print(f"Iconik: {host}\n")

if "--lengths" in sys.argv:
    # Video-kandidaten rond een paar lengtes, om meetjobs uit te kiezen.
    for minutes in (5, 30, 60, 90):
        low, high = int((minutes - 3) * 60000), int((minutes + 3) * 60000)
        query = (f"NOT _exists_:{FIELD} AND NOT _exists_:transcription_text "
                 f"AND duration_milliseconds:[{low} TO {high}] ")
        try:
            result = api("POST", "search/v1/search/?per_page=5",
                         {"doc_types": ["assets"], "query": query,
                          "filter": {"operator": "AND", "terms": [{"name": "media_type", "value": "video"}]}})
        except RuntimeError as error:
            print(f"Rond {minutes} min: FOUT {error}")
            continue
        print(f"Rond {minutes} min ({result.get('total')} kandidaten):")
        for o in result.get("objects", []):
            seconds = (o.get("duration_milliseconds") or 0) / 1000
            print(f"  {o.get('id')}  {int(seconds // 60):>3}:{int(seconds % 60):02d}  {(o.get('title') or '')[:60]}")
    sys.exit(0)

counts = {}
counts["all"], _ = show("Alle assets", "")
counts["A"], _ = show("A  status niet gezet", f"NOT _exists_:{FIELD} ")
counts["B"], _ = show("B  status wel gezet", f"_exists_:{FIELD} ")
counts["C"], _ = show("C  met transcript", "_exists_:transcription_text ")
counts["D"], _ = show("D  zonder transcript", "NOT _exists_:transcription_text ")
counts["E"], candidates = show("E  geen status EN geen transcript",
                               f"NOT _exists_:{FIELD} AND NOT _exists_:transcription_text ")
counts["F"], _ = show("F  wel transcript, geen status",
                      f"_exists_:transcription_text AND NOT _exists_:{FIELD} ")

print("\nControle van de tellingen (moet kloppen als de zoektaal doet wat we denken):")
c = counts
if None not in (c["all"], c["A"], c["B"]):
    print(f"  A + B = alle:   {c['A']} + {c['B']} = {c['A'] + c['B']}  (alle: {c['all']})")
if None not in (c["all"], c["C"], c["D"]):
    print(f"  C + D = alle:   {c['C']} + {c['D']} = {c['C'] + c['D']}  (alle: {c['all']})")
if None not in (c["A"], c["E"], c["F"]):
    print(f"  E + F = A:      {c['E']} + {c['F']} = {c['E'] + c['F']}  (A: {c['A']})")

mounts = transcribe.load_config().get("mounts") or {}
print(f"\nOpslag die deze Mac kent: {len(mounts)}")
for storage_id in mounts:
    show(f"G  op opslag {storage_id[:8]}", f"files.storage_id:{storage_id} ")
    show(f"H  E + opslag {storage_id[:8]} (query)",
         f"NOT _exists_:{FIELD} AND NOT _exists_:transcription_text AND files.storage_id:{storage_id} ")
    show(f"I  E + opslag {storage_id[:8]} (filter)",
         f"NOT _exists_:{FIELD} AND NOT _exists_:transcription_text ",
         [{"name": "files.storage_id", "value": storage_id}])

print("\nSteekproef uit E, rechtstreeks nagevraagd per asset:")
for asset_id in candidates:
    try:
        asset = api("GET", f"assets/v1/assets/{asset_id}/")
        versions = [v for v in asset.get("versions", []) if v.get("status") == "ACTIVE"]
        found = "geen actieve versie"
        if versions:
            objects = api("GET", f"assets/v1/assets/{asset_id}/versions/{versions[-1]['id']}"
                                 "/transcriptions/properties/").get("objects", [])
            found = f"{len(objects)} transcript(en)"
        print(f"  {asset_id}  {found:<22} {asset.get('type', '?'):<8} {(asset.get('title') or '')[:50]}")
    except RuntimeError as error:
        print(f"  {asset_id}  FOUT {error}")


# Deel 2: mediatype en sortering.
CANDIDATES = f"NOT _exists_:{FIELD} AND NOT _exists_:transcription_text "

print("\nVelden van een zoekresultaat (om te zien hoe het mediatype heet):")
try:
    first = (search(CANDIDATES, per_page=1).get("objects") or [{}])[0]
    print("  " + ", ".join(sorted(first)))
    for key in ("media_type", "type", "object_type", "is_online", "status", "date_created"):
        if key in first:
            print(f"  {key} = {first[key]!r}")
    files = first.get("files") or []
    if files:
        print("  files[0]: " + ", ".join(f"{k}={v!r}" for k, v in sorted(files[0].items())
                                       if k in ("type", "file_set_id", "format_id", "storage_id", "status")))
except RuntimeError as error:
    print(f"  FOUT {error}")

print("\nKandidaten (E) per mediatype, in de query en als filter:")
for kind in ("video", "audio", "image", "document", "other"):
    show(f"E + media_type:{kind} (query)", CANDIDATES + f"AND media_type:{kind} ")
    show(f"E + media_type {kind} (filter)", CANDIDATES, [{"name": "media_type", "value": kind}])

print("\nSortering (kandidaten, eerste drie):")
for order in ("desc", "asc"):
    body = {"doc_types": ["assets"], "query": CANDIDATES,
            "sort": [{"name": "date_created", "order": order}]}
    try:
        result = api("POST", "search/v1/search/?per_page=3", body)
        print(f"  date_created {order}:")
        for o in result.get("objects", []):
            print(f"    {str(o.get('date_created'))[:19]}  {(o.get('title') or '')[:50]}")
    except RuntimeError as error:
        print(f"  date_created {order}: FOUT {error}")
