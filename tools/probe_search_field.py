"""Zoekt uit onder welke naam het statusveld in de zoekindex staat. Alleen lezen.

Gebruik: cd ~/iconik-transcribe && uv run tools/probe_search_field.py --asset <id>
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import transcribe  # noqa: E402

FIELD = "local_transcribe_status"


def search(iconik, filt, extra=None):
    body = {"doc_types": ["assets"], "query": "", "filter": filt}
    body.update(extra or {})
    try:
        return iconik.post("search/v1/search/?per_page=20", body)
    except Exception as exc:
        return {"error": str(exc)[:200]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True)
    args = parser.parse_args()
    iconik = transcribe.make_client()

    view = next(v for v in iconik.get("metadata/v1/views/")["objects"] if v["name"] == "Local Transcribe")
    now = iconik.get(f"metadata/v1/assets/{args.asset}/assets/{args.asset}/views/{view['id']}/")
    value = now["metadata_values"][FIELD]["field_values"][0]["value"]
    print("Huidige waarde:", value)

    doc = search(iconik, {"operator": "AND", "terms": [{"name": "id", "value": args.asset}]})
    if doc.get("error"):
        print("Zoeken op id mislukt:", doc["error"])
    for obj in doc.get("objects", []):
        text = json.dumps(obj)
        print("Asset in index, sleutels:", sorted(obj.keys())[:40])
        print("Veld in het document:", FIELD in text)
        for key, val in obj.items():
            if FIELD in json.dumps(val):
                print("  gevonden onder:", key, json.dumps(val)[:300])

    for name in [FIELD, f"metadata.{FIELD}", f"metadata.{FIELD}.keyword", f"metadata.{FIELD}.value",
                 f"{FIELD}.keyword", f"metadata_values.{FIELD}"]:
        res = search(iconik, {"operator": "AND", "terms": [{"name": name, "value": value}]})
        print(f"{name:45s}", res.get("error") or f"{len(res.get('objects', []))} gevonden")
    res = search(iconik, {"operator": "AND", "terms": [{"name": f"metadata.{FIELD}", "exists": True}]})
    print(f"{'exists metadata.' + FIELD:45s}", res.get("error") or f"{len(res.get('objects', []))} gevonden")


if __name__ == "__main__":
    main()
