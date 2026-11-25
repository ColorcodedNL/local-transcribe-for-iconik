"""De knoppen in Iconik: Local Transcribe (asset, collectie, selectie) en Refine transcript (alleen een asset),
en de link van Refine transcript die de editor opent.

Draaien: uv run --no-project --with pynacl --with requests --with numpy --with huggingface_hub python -B tests/test_actions.py
"""
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, "app")
_kr = types.ModuleType("keyring")
_kr.get_password = lambda *a: None
_kr.set_password = lambda *a: None
_kr.delete_password = lambda *a: None
sys.modules["keyring"] = _kr
for name in ("mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
import serve  # noqa: E402

serve.transcribe.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"   # nooit de echte voorkeuren aanraken

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


class FakeIconik:
    def __init__(self, existing):
        self.existing, self.posted, self.deleted = [dict(a, id=a.get("id") or f"id{i}") for i, a in enumerate(existing)], [], []

    def get(self, path):
        return {"objects": self.existing}

    def post(self, path, body):
        made = dict(body, id=f"nieuw{len(self.posted)}")
        self.posted.append((path, body)); self.existing.append(made); return made

    def delete(self, path, body=None):
        self.deleted.append(path)
        action_id = path.rstrip("/").rsplit("/", 1)[-1]
        self.existing = [a for a in self.existing if a["id"] != action_id]


old = [{"context": c, "title": "Local Transcribe", "url": "iconiktranscribe://transcribe"} for c in ("ASSET", "COLLECTION", "BULK")]
fake = FakeIconik(old)
missing = serve.missing_actions(fake)
check("bestaande omgeving: alleen Refine transcript ontbreekt", missing == [("ASSET", "Refine transcript", "iconiktranscribe://refine")], missing)
serve.client = lambda *a: fake
serve.install_actions()
check("plaatsen: één nieuwe knop, alleen op een asset", fake.posted == [("assets/v1/custom_actions/ASSET/",
      {"context": "ASSET", "title": "Refine transcript", "type": "OPEN", "url": "iconiktranscribe://refine"})], fake.posted)
check("daarna niets meer te doen", serve.missing_actions(fake) == [] and serve.actions["state"] == "aanwezig")
empty = FakeIconik([])
check("lege omgeving: vier knoppen", len(serve.missing_actions(empty)) == 4)

aid = "aaaaaaaa-1111-2222-3333-444444444444"
check("link: zonder Iconik in de voorkeuren niets (de knop kan er dan niet zijn)",
      serve.refine_link(f"iconiktranscribe://refine?asset_ids={aid}") is None)
src = serve.items.iconik_source(create=True)
check("link: editor voor het item van dat asset in de enige Iconik",
      serve.refine_link(f"iconiktranscribe://refine?asset_ids={aid}") == f"/refine?item={src}:{aid}")
check("link: bij meer assets het eerste",
      serve.refine_link(f"iconiktranscribe://refine?asset_ids={aid},{aid[::-1]}") == f"/refine?item={src}:{aid}")
check("link: zonder asset niets", serve.refine_link("iconiktranscribe://refine") is None)
check("link: rare waarde niets", serve.refine_link("iconiktranscribe://refine?asset_ids=../../etc") is None)
check("link: transcribe-link is geen Refine", serve.refine_link(f"iconiktranscribe://transcribe?asset_ids={aid}") is None)

# --- stap 4e: de knop zegt uit welke Iconik-omgeving hij komt (system_domain_id, op elke Mac hetzelfde) ---
DOMAIN = "8c8aa6a8-1111-2222-3333-444444444444"
OTHER = "deadbeef-1111-2222-3333-444444444444"


class DomainIconik(FakeIconik):
    """Als FakeIconik, met de huidige gebruiker van een omgeving."""
    def __init__(self, existing, domain=DOMAIN):
        super().__init__(existing)
        self.domain = domain

    def get(self, path):
        if path == "users/v1/users/current/":
            return {"first_name": "Proef", "email": "proef@example.com", "system_domain_id": self.domain}
        return super().get(path)


serve.items.settings.note(src, serve.items.DOMAIN_KEY, None)
fake = DomainIconik([])
serve.client = lambda *a: fake
serve.install_actions()
urls = sorted({body["url"] for _, body in fake.posted})
check("nieuwe knoppen hebben het domein van de omgeving in de link", urls == [
      f"iconiktranscribe://refine?lt_domain={DOMAIN}", f"iconiktranscribe://transcribe?lt_domain={DOMAIN}"], urls)
check("het exemplaar onthoudt bij welke omgeving het hoort", serve.setting.noted(src, serve.items.DOMAIN_KEY) == DOMAIN)
check("daarna niets meer te doen", serve.missing_actions(fake) == [])
# Stap 5f (besloten 25 november): knoppen van vóór stap 4e worden vervangen door knoppen met het domein.
old_style = DomainIconik([{"context": c, "title": t, "url": u} for t, u, cs in serve.ACTIONS for c in cs]
                         + [{"context": "ASSET", "title": "Iets van een ander", "url": "https://elders.example/hook"}])
serve.client = lambda *a: old_style
serve.check_actions()
urls = sorted((a["context"], a["url"]) for a in old_style.existing)
check("oude knoppen vervangen: dezelfde vier, nu met het domein, en die van een ander blijft",
      urls == sorted([(c, f"{u}?lt_domain={DOMAIN}") for _, u, cs in serve.ACTIONS for c in cs] + [("ASSET", "https://elders.example/hook")]), urls)
check("oude knoppen vervangen: eerst de nieuwe, dan de oude weg (nooit zonder knop)",
      len(old_style.posted) == 4 and len(old_style.deleted) == 4 and serve.actions["state"] == "aanwezig")
check("oude knoppen vervangen: een tweede keer kijken doet niets meer",
      serve.replace_old_actions(old_style) == 0 and len(old_style.posted) == 4)
removed = serve.remove_actions()
check("knoppen weghalen (−): alleen de onze, de knop van een ander blijft",
      removed == 4 and [a["url"] for a in old_style.existing] == ["https://elders.example/hook"], old_style.existing)
serve.setting.note(src, serve.items.DOMAIN_KEY, None)
unknown = DomainIconik([{"context": c, "title": t, "url": u} for t, u, cs in serve.ACTIONS for c in cs], domain=None)
check("domein onbekend: oude knoppen blijven staan en tellen als aanwezig", serve.replace_old_actions(unknown) == 0
      and serve.missing_actions(unknown) == [] and not unknown.deleted)
serve.setting.note(src, serve.items.DOMAIN_KEY, DOMAIN)

# Links uit Iconik: Refine transcript
link = f"iconiktranscribe://refine?lt_domain={DOMAIN}&asset_ids={aid}"
check("Refine: link uit deze omgeving opent het item van deze Mac", serve.refine_link(link) == f"/refine?item={src}:{aid}")
check("Refine: link zonder domein (oude knop) valt terug op de enige Iconik",
      serve.refine_link(f"iconiktranscribe://refine?asset_ids={aid}") == f"/refine?item={src}:{aid}")
check("Refine: link uit een andere omgeving opent niets", serve.refine_link(link.replace(DOMAIN, OTHER)) is None)
check("Refine: een domein dat er geen kan zijn opent niets", serve.refine_link(link.replace(DOMAIN, "../x")) is None)

# Links uit Iconik: Local Transcribe (via queue_request, zoals de route /transcribe/<sleutel> het doorgeeft)
serve.enqueue_calls = []
real_enqueue = serve.enqueue
serve.enqueue = lambda ids, source=None: serve.enqueue_calls.append((list(ids), source))
red = []
serve.add_failed = lambda title, message: red.append(title)
serve.queue_request([([aid], [], DOMAIN)])
check("transcriberen: knop uit deze omgeving komt in de rij, bij het juiste exemplaar", serve.enqueue_calls == [([aid], src)], serve.enqueue_calls)
serve.queue_request([([aid], [])])
check("transcriberen: oude knop zonder domein ook (de enige Iconik)", serve.enqueue_calls[-1] == ([aid], src))
serve.enqueue_calls.clear()
serve.queue_request([([aid], ["col"], OTHER)])
check("transcriberen: knop uit een andere omgeving: niets in de rij, wel een rode regel", not serve.enqueue_calls
      and red in (["Knop uit een andere Iconik-omgeving"], ["Button from another Iconik environment"]), (serve.enqueue_calls, red))
serve.enqueue = real_enqueue

# Net ingelogd en het domein nog niet opgehaald, met één Iconik: de knop werkt meteen.
serve.setting.note(src, serve.items.DOMAIN_KEY, None)
check("domein nog onbekend en één Iconik: de knop werkt", serve.refine_link(link) == f"/refine?item={src}:{aid}")

# Een ander Iconik-adres: de inlog en het domein horen bij de oude omgeving.
serve.setting.note(src, serve.items.DOMAIN_KEY, DOMAIN)
serve._after_host("https://app.iconik.io", "https://ander.example")
check("ander adres: het domein van de oude omgeving is vergeten", serve.setting.noted(src, serve.items.DOMAIN_KEY) is None)

# De route zelf: /transcribe/<sleutel>?lt_domain=...&asset_ids=... geeft het domein door.
import http.client, threading  # noqa: E402,E401
from http.server import ThreadingHTTPServer  # noqa: E402
got = []
serve.queue_request = lambda requests: got.extend(requests)
serve.client = lambda *a: fake
serve.settings.update(key="k", hf_token=None, model="m", language=None, mount=None, idle_exit=False)
server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
c.putrequest("GET", f"/transcribe/k?lt_domain={DOMAIN}&asset_ids={aid}", skip_host=True)
c.putheader("Host", f"localhost:{server.server_address[1]}")
c.endheaders()
status = c.getresponse().status
check("route: de knop geeft assets en domein door", status == 303 and got == [([aid], [], DOMAIN)], (status, got))
server.shutdown()

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
