"""Proef voor stap 4e: waaraan herkent de app een Iconik-omgeving, op elke Mac hetzelfde?

Het exemplaar-ID in config.json (iconik-<6 hex>) maakt elke Mac zelf; een knop in Iconik wordt door één Mac
neergezet maar door iedereen gebruikt. Daarom zoeken we een ID van de omgeving zelf. Alleen lezen: toont welke
velden de huidige gebruiker heeft en de velden die naar een domein of account wijzen (ID's ingekort).

Draaien (de app even open, zodat het token vers is):
  uv run tools/domain_probe.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import video_probe  # noqa: E402  (ReadOnlyIconik: het token van de app, nooit verversen)


def short(value):
    text = str(value)
    return text[:8] + "…" if len(text) > 12 else text


def main():
    client = video_probe.ReadOnlyIconik()
    user = client.get("users/v1/users/current/")
    print("Velden van de huidige gebruiker:", ", ".join(sorted(user)))
    for key in sorted(user):
        if any(word in key.lower() for word in ("domain", "account", "system", "tenant", "organization")):
            print(f"  {key}: {short(user[key])}")
    for path in ("users/v1/users/current/system_domain/", "settings/v1/system/current/", "settings/v1/system/"):
        try:
            data = client.get(path)
            keys = sorted(data) if isinstance(data, dict) else type(data).__name__
            print(f"{path}: {keys}")
        except Exception as error:
            print(f"{path}: {type(error).__name__}: {str(error)[:80]}")


if __name__ == "__main__":
    main()
