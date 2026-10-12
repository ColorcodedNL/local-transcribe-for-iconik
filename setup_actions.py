#!/usr/bin/env python3
"""Maakt de Custom Actions in Iconik aan (of werkt ze bij) voor de lokale ontvanger.

Gebruik:
    source ~/.iconik-env
    uv run setup_actions.py            # laat zien wat er zou gebeuren
    uv run setup_actions.py --apply    # voert het uit
    uv run setup_actions.py --apply --remove "Transcribeer (test)"

Maakt per context (een asset, een collectie, een selectie) een actie "Local Transcribe"
die http://localhost:<poort>/transcribe/<sleutel> opent in de browser.
"""

import argparse
import os
import sys

from transcribe import Iconik, Problem

TITLE = "Local Transcribe"
OLD_TITLES = ("Transcribeer",)  # worden hernoemd in plaats van dubbel aangemaakt
CONTEXTS = {"ASSET": "een asset", "COLLECTION": "een collectie", "BULK": "een selectie"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Echt uitvoeren.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--app", action="store_true",
                        help="Laat de acties de app openen (iconiktranscribe://) in plaats van localhost.")
    parser.add_argument("--remove", action="append", default=[],
                        help="Titel van een actie die weg moet (mag vaker).")
    args = parser.parse_args()

    key = os.environ.get("LISTENER_KEY")
    if not key:
        sys.exit("LISTENER_KEY ontbreekt. Draai eerst: source ~/.iconik-env")
    iconik = Iconik(os.environ["ICONIK_APP_ID"], os.environ["ICONIK_TOKEN"])
    if args.app:
        # De app "Local Transcribe for Iconik" vangt dit linktype op en start de ontvanger zelf.
        url = shown = "iconiktranscribe://transcribe"
    else:
        url = f"http://localhost:{args.port}/transcribe/{key}"
        shown = f"http://localhost:{args.port}/transcribe/<sleutel>"

    existing = iconik.get("assets/v1/custom_actions/?per_page=200").get("objects", [])
    print(f"Bestaande acties: {len(existing)}")
    for action in existing:
        print(f"  {action['context']:<11} {action['type']:<5} {action['title']}")

    print("\nPlan:" if not args.apply else "\nUitvoeren:")
    for context, meaning in CONTEXTS.items():
        match = [a for a in existing if a["context"] == context and a["title"] in (TITLE, *OLD_TITLES)]
        body = {"context": context, "title": TITLE, "type": "OPEN", "url": url}
        if match:
            renamed = f" (was {match[0]['title']})" if match[0]["title"] != TITLE else ""
            print(f"  bijwerken   {TITLE} op {meaning}{renamed} -> {shown}")
            if args.apply:
                iconik._call("PUT", f"assets/v1/custom_actions/{context}/{match[0]['id']}/", json=body)
        else:
            print(f"  aanmaken    {TITLE} op {meaning} -> {shown}")
            if args.apply:
                iconik.post(f"assets/v1/custom_actions/{context}/", body)

    for action in existing:
        if action["title"] in args.remove:
            print(f"  verwijderen {action['title']} ({action['context']})")
            if args.apply:
                iconik.delete(f"assets/v1/custom_actions/{action['context']}/{action['id']}/")

    print("\nKlaar." if args.apply else "\nNiets gewijzigd. Voeg --apply toe om het uit te voeren.")


if __name__ == "__main__":
    try:
        main()
    except Problem as problem:
        sys.exit(f"FOUT: {problem}")
