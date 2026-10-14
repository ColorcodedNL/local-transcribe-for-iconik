"""Alleen-lezen test: mag de inlog van de app metadatavelden lezen en zoeken?

Gebruik: cd ~/iconik-transcribe && uv run tools/probe_queue.py
Schrijft niets naar Iconik.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import transcribe  # noqa: E402


def attempt(label, call):
    try:
        data = call()
    except Exception as exc:  # Problem uit transcribe.fail, of een netwerkfout
        print(f"{label}: MISLUKT\n    {str(exc)[:300]}")
        return None
    print(f"{label}: OK")
    return data


def main():
    iconik = transcribe.make_client()
    if iconik is None:
        print("Niet ingelogd. Log eerst in via de app.")
        return

    fields = attempt("Metadatavelden lezen (GET metadata/v1/fields/)",
                     lambda: iconik.get("metadata/v1/fields/"))
    if fields is not None:
        objects = fields.get("objects", [])
        print(f"    {len(objects)} velden, bijvoorbeeld: {[f.get('name') for f in objects[:8]]}")

    found = attempt("Zoeken (POST search/v1/search/)",
                    lambda: iconik.post("search/v1/search/",
                                        {"doc_types": ["assets"], "query": "*", "per_page": 1}))
    if found is not None:
        print(f"    totaal gevonden: {found.get('total')}")


if __name__ == "__main__":
    main()
