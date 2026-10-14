"""Schrijfproef voor de rij in Iconik: een statusveld, een view, een waarde, zoeken.

Zonder --apply gebeurt er niets, het laat alleen zien wat er ontbreekt.
Met --apply --asset <id>: maakt het veld en de view aan als ze ontbreken,
zet de status op een testasset, leest hem terug, zoekt hem en zet hem op "klaar".

Gebruik: cd ~/iconik-transcribe && uv run tools/probe_queue_write.py --asset <id> [--apply]
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import transcribe  # noqa: E402

FIELD = "local_transcribe_status"
LABEL = "Transcriptie"
VIEW = "Local Transcribe"
OPTIONS = ["aangevraagd", "bezig", "klaar", "fout"]


def step(label, call):
    try:
        out = call()
    except Exception as exc:
        print(f"  MISLUKT: {label}\n    {str(exc)[:400]}")
        sys.exit(1)
    print(f"  ok: {label}")
    return out


def set_status(iconik, asset, view_id, value):
    return iconik._call("PUT", f"metadata/v1/assets/{asset}/assets/{asset}/views/{view_id}/",
                        json={"metadata_values": {FIELD: {"field_values": [{"value": value}], "mode": "overwrite"}}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    iconik = transcribe.make_client()
    if iconik is None:
        print("Niet ingelogd. Log eerst in via de app.")
        return

    fields = iconik.get("metadata/v1/fields/").get("objects", [])
    views = iconik.get("metadata/v1/views/").get("objects", [])
    field_exists = any(f.get("name") == FIELD for f in fields)
    view = next((v for v in views if v.get("name") == VIEW), None)
    print(f"Veld '{FIELD}': {'bestaat al' if field_exists else 'ontbreekt'}")
    print(f"View '{VIEW}': {'bestaat al' if view else 'ontbreekt'}")
    if not args.apply:
        print("Proefrun. Met --apply wordt het aangemaakt en op asset", args.asset, "getest.")
        return

    if not field_exists:
        step("veld aanmaken", lambda: iconik.post("metadata/v1/fields/", {
            "name": FIELD, "label": LABEL, "field_type": "drop_down",
            "options": [{"label": o, "value": o} for o in OPTIONS]}))
    if not view:
        view = step("view aanmaken", lambda: iconik.post("metadata/v1/views/", {
            "name": VIEW, "write_access_for_everyone": True,
            "view_fields": [{"name": FIELD, "label": LABEL}]}))
    view_id = view["id"]

    step("status 'aangevraagd' zetten op de testasset", lambda: set_status(iconik, args.asset, view_id, "aangevraagd"))
    back = step("waarde teruglezen", lambda: iconik.get(f"metadata/v1/assets/{args.asset}/assets/{args.asset}/views/{view_id}/"))
    print("    gelezen:", back.get("metadata_values", {}).get(FIELD))

    query = {"doc_types": ["assets"], "query": "",
             "filter": {"operator": "AND", "terms": [{"name": FIELD, "value": "aangevraagd"}]}}
    found = []
    for attempt in range(6):
        result = step(f"zoeken op status (poging {attempt + 1})",
                      lambda: iconik.post("search/v1/search/?per_page=20", query))
        found = [o.get("id") for o in result.get("objects", [])]
        print(f"    gevonden: {len(found)}, testasset erbij: {args.asset in found}")
        if args.asset in found:
            break
        time.sleep(5)

    step("status terugzetten op 'klaar'", lambda: set_status(iconik, args.asset, view_id, "klaar"))


if __name__ == "__main__":
    main()
