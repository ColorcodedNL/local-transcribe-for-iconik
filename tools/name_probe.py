"""Proef voor stap 5e-2: geeft Iconik een naam voor de omgeving, om een koppeling in de app automatisch te noemen?

Alleen lezen. Toont van de systeeminstellingen en de huidige gebruiker de velden die op een naam lijken, met hun
waarde (de naam van de eigen omgeving, geen geheimen), en van de rest alleen de namen van de velden.

Draaien (de app even open, zodat het token vers is):
  uv run tools/name_probe.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import video_probe  # noqa: E402  (ReadOnlyIconik: het token van de app, nooit verversen)

WORDS = ("name", "title", "company", "organization", "organisation", "brand", "label", "display", "domain", "url")


def show(path, data):
    if not isinstance(data, dict):
        print(f"{path}: {type(data).__name__}")
        return
    print(f"{path}: velden {', '.join(sorted(data))}")
    for key in sorted(data):
        value = data[key]
        if any(secret in key.lower() for secret in ("token", "secret", "password", "key")):
            continue
        if any(word in key.lower() for word in WORDS) and (value is None or isinstance(value, (str, int, float, bool))):
            print(f"  {key}: {str(value)[:80]}")


def main():
    client = video_probe.ReadOnlyIconik()
    for path in ("settings/v1/system/current/", "users/v1/users/current/"):
        try:
            data = client.get(path)
            show(path, data)
        except Exception as error:
            print(f"{path}: {type(error).__name__}: {str(error)[:80]}")
            continue
        # De omgevingen van de gebruiker: per stuk de velden, en wat op een naam lijkt.
        for i, domain in enumerate(data.get("system_domains") or [] if isinstance(data, dict) else []):
            show(f"{path} system_domains[{i}]", domain if isinstance(domain, dict) else {"waarde": domain})


if __name__ == "__main__":
    main()
