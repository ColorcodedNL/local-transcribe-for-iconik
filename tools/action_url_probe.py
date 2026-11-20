"""Proef voor stap 4: wat stuurt Iconik mee als de link van een Custom Action al een eigen query of pad heeft?

Nieuwe knoppen moeten zeggen uit welke Iconik-omgeving ze komen (het exemplaar, bv. iconik-ab12cd). Dat kan in
de query (?lt=...) of in het pad. Deze proef zet tijdelijk vier knoppen in Iconik die naar een kleine ontvanger
op deze Mac wijzen (http://localhost:8799), laat bij elke klik de volledige link en de headers zien en haalt de
knoppen bij het stoppen weer weg.

  LT probe (query)  op een asset en op een collectie: http://localhost:8799/probe/q?lt=iconik-test01
  LT probe (pad)    op een asset en op een collectie: http://localhost:8799/probe/pad/iconik-test01

Schrijft naar Iconik (alleen deze knoppen). Gebruikt het token van de app zonder het te verversen.

Draaien:
  uv run tools/action_url_probe.py              knoppen neerzetten, luisteren tot Ctrl+C, knoppen weghalen
  uv run tools/action_url_probe.py --cleanup    alleen knoppen die met "LT probe" beginnen weghalen
"""
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import video_probe  # noqa: E402  (ReadOnlyIconik: het token van de app, nooit verversen)

PORT = 8799
PREFIX = "LT probe"
BUTTONS = ((f"{PREFIX} (query)", f"http://localhost:{PORT}/probe/q?lt=iconik-test01"),
           (f"{PREFIX} (pad)", f"http://localhost:{PORT}/probe/pad/iconik-test01"))
CONTEXTS = ("ASSET", "COLLECTION")


def ours(client):
    """De proefknoppen die nu in Iconik staan, als (context, id, titel)."""
    found = client.get("assets/v1/custom_actions/?per_page=200").get("objects", [])
    return [(a.get("context"), a.get("id"), a.get("title")) for a in found if str(a.get("title", "")).startswith(PREFIX)]


def cleanup(client):
    for context, action_id, title in ours(client):
        try:
            client.delete(f"assets/v1/custom_actions/{context}/{action_id}/")
            print(f"Weggehaald: {title} ({context})")
        except Exception as error:   # de client meldt een HTTP-fout als Problem; hier alleen melden
            print(f"Weghalen lukte niet: {title} ({context}): {error}")


class Listener(BaseHTTPRequestHandler):
    def do_GET(self):
        print("\n--- klik ontvangen ---")
        print(f"Pad en query: {self.path}")
        for name, value in self.headers.items():
            if name.lower() != "cookie":
                print(f"  {name}: {value}")
        body = "Ontvangen. Je kunt dit tabblad sluiten.".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    client = video_probe.ReadOnlyIconik()
    if "--cleanup" in sys.argv:
        cleanup(client)
        return
    if ours(client):
        sys.exit("Er staan nog proefknoppen in Iconik. Draai eerst met --cleanup.")
    try:
        for title, url in BUTTONS:
            for context in CONTEXTS:
                client.post(f"assets/v1/custom_actions/{context}/",
                            {"context": context, "title": title, "type": "OPEN", "url": url})
                print(f"Neergezet: {title} ({context})")
        print(f"\nLuistert op http://localhost:{PORT}. Klik in Iconik op een asset en op een collectie op beide")
        print("LT probe-knoppen (rechtsklik of het menu met acties). Stoppen met Ctrl+C.")
        HTTPServer(("127.0.0.1", PORT), Listener).serve_forever()
    except KeyboardInterrupt:
        print("\nGestopt.")
    finally:
        cleanup(client)


if __name__ == "__main__":
    main()
