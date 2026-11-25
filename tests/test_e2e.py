"""End-to-end: de echte ontvanger (serve.py) en het echte venster (ui.html) in een browser, tegen een
nep-Iconik op deze machine. Klikt wat een gebruiker klikt: inloggen, uitloggen, een andere client-ID,
een klik vanuit Iconik terwijl je uitgelogd bent, de schakelaars. Elke 4xx of 5xx van de ontvanger
en elke JavaScript-fout is een fout. Zo was de 403 op inloggen (0.3.1 tot en met 0.3.4) meteen gevonden.

Eenmalig:  uv run --no-project --with playwright playwright install chromium
Draaien:   uv run --no-project --with pynacl --with requests --with numpy --with playwright python -B tests/test_e2e.py

Met LT_CHROMIUM=/pad/naar/chrome gebruikt hij die browser in plaats van die van Playwright.
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import traceback
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

# Alles wat de app schrijft (voorkeuren, LaunchAgent, logboek) in een lege map, nooit in je eigen thuismap.
# Playwright zoekt zijn browser onder HOME. Leg de echte plek vast voordat HOME hieronder verandert.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "ms-playwright"))
TMP = Path(tempfile.mkdtemp(prefix="lt-e2e-"))
os.environ["HOME"] = str(TMP)
FAKE_RES = TMP / "Local Transcribe for Iconik.app" / "Contents" / "Resources"
FAKE_RES.mkdir(parents=True)
os.environ["LOCALTRANSCRIBE_RES"] = str(FAKE_RES)   # dan toont het venster ook "opstarten bij inloggen"

sys.path.insert(0, "app")
keychain = {}
kr = types.ModuleType("keyring")
kr.get_password = lambda s, u: keychain.get((s, u))
kr.set_password = lambda s, u, p: keychain.__setitem__((s, u), p)
kr.delete_password = lambda s, u: keychain.pop((s, u))
sys.modules["keyring"] = kr
for name in ("mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)

import transcribe, serve  # noqa: E402,E401
import settings as app_settings  # noqa: E402  (`settings` is hieronder het blok Voorkeuren op de pagina)
from playwright.sync_api import sync_playwright  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


def text_keys(html):
    """De sleutels van TEXT.nl en TEXT.en in een venster, met dubbele. Een dubbele sleutel overschrijft stil de
    eerste (zo stond "Log in bij Iconik" ooit bij Opstarten bij inloggen)."""
    import re
    block = html[html.index("const TEXT = {"):]
    block = block[:block.index("\n};") + 3]
    found = {}
    for lang in ("nl", "en"):
        part = block[block.index(f"\n  {lang}: {{"):]
        part = part[:part.index("\n  },") if "\n  }," in part else part.index("\n};")]
        part = re.sub(r'"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`|\'(?:[^\'\\]|\\.)*\'', '""', part)   # geen tekst binnen strings
        while True:   # kleinere lijstjes binnen de tabel (taalkeuzes, redenen) tellen niet mee
            flat = re.sub(r"\{[^{}]*\}", "0", part)
            if flat == part:
                break
            part = flat
        keys = re.findall(r"(?:^|,|\{)\s*([A-Za-z_]\w*)\s*:", part, re.M)[1:]   # de eerste is "nl" of "en" zelf
        found[lang] = sorted({k for k in keys if keys.count(k) > 1})
    return found


for page_file in ("ui.html", "settings.html", "refine.html"):
    dupes = text_keys((Path("app") / page_file).read_text())
    check(f"{page_file}: geen tekstsleutel twee keer", not dupes["nl"] and not dupes["en"], dupes)


# --- Nep-Iconik: OAuth met PKCE, token, gebruiker, knoppen, titels
ICON_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
CLIENT_IDS = {"11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"}
DOMAIN = "dddddddd-1111-2222-3333-444444444444"   # system_domain_id van de nep-omgeving, zoals Iconik hem bij de gebruiker geeft
iconik = {"codes": {}, "tokens": set(), "pkce_ok": 0, "authorize": 0, "bad": []}


class FakeIconik(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body=None, headers=()):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return self.headers.get("Authorization", "").removeprefix("Bearer ") in iconik["tokens"]

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/oauth/authorize":
            iconik["authorize"] += 1
            if q.get("client_id") not in CLIENT_IDS or q.get("redirect_uri") != transcribe.REDIRECT_URI \
                    or q.get("code_challenge_method") != "S256":
                iconik["bad"].append(q)
                return self.send(400, {"error": "invalid_request"})
            code = f"code-{len(iconik['codes'])}"
            iconik["codes"][code] = (q["client_id"], q["code_challenge"])
            target = q["redirect_uri"] + "?" + urlencode({"code": code, "state": q["state"]})
            return self.send(302, headers=[("Location", target)])
        if url.path == "/evil":
            # Een gewone webpagina die probeert de ontvanger te bedienen, zonder de header van het venster.
            page = (f"<img src='http://localhost:{APP_PORT}/logout'>"
                    f"<script>fetch('http://localhost:{APP_PORT}/clear', {{mode: 'no-cors'}}).finally(() => "
                    f"document.title = 'klaar')</script>")
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if url.path in ("/", "/static/apple-touch-icon.png"):
            # De startpagina met een icoon, zoals Iconik die heeft; de app toont dat icoon bij Integraties.
            body, kind = ((b'<html><head><link rel="apple-touch-icon" href="/static/apple-touch-icon.png" /></head></html>', "text/html")
                          if url.path == "/" else (ICON_PNG, "image/png"))
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if not url.path.startswith("/API/"):
            return self.send(404, {})
        if not self.authorized():
            return self.send(401, {"error": "unauthorized"})
        path = url.path.removeprefix("/API/")
        if path == "users/v1/users/current/":
            return self.send(200, {"first_name": "Test", "last_name": "Gebruiker", "email": "test@example.com",
                                   "system_domain_id": DOMAIN})
        if path.startswith("assets/v1/custom_actions/"):
            return self.send(200, {"objects": [{"context": c, "url": url} for _, url, contexts in serve.ACTIONS for c in contexts]})
        if path.startswith("assets/v1/assets/"):
            return self.send(200, {"title": "Titel " + path.split("/")[3]})
        return self.send(200, {"objects": []})

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        if url.path == "/API/auth/v1/oauth/token/":
            if form.get("grant_type") == "authorization_code":
                client_id, challenge = iconik["codes"].pop(form.get("code"), (None, None))
                verifier = form.get("code_verifier", "")
                expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if client_id is None or client_id != form.get("client_id") or expected != challenge:
                    return self.send(400, {"error": "invalid_grant"})
                iconik["pkce_ok"] += 1
            token = f"token-{len(iconik['tokens'])}"
            iconik["tokens"].add(token)
            return self.send(200, {"access_token": token, "refresh_token": "r-" + token, "expires_in": 3600})
        return self.send(404, {})


fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeIconik)
FAKE = f"http://127.0.0.1:{fake.server_address[1]}"
threading.Thread(target=fake.serve_forever, daemon=True).start()

# --- De echte ontvanger, op een vrije poort, met de nep-Iconik als host
app = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
APP_PORT = app.server_address[1]
BASE = f"http://localhost:{APP_PORT}"
transcribe.REDIRECT_URI = f"{BASE}/oauth/callback"
transcribe.save_config({"host": FAKE, "client_id": sorted(CLIENT_IDS)[0], "app_language": "nl"})
serve.settings.update(key="sleutel", hf_token=None, model="m", language=None, mount=None, idle_exit=False)
threading.Thread(target=app.serve_forever, daemon=True).start()   # zonder werkrij: opdrachten blijven wachten


def logged_in():
    return bool(transcribe.load_login())


errors, console = [], []
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.environ.get("LT_CHROMIUM") or None)
    try:
        page = browser.new_page()
        page.on("response", lambda r: errors.append(f"{r.status} {r.url}")
                if r.url.startswith(BASE) and r.status >= 400 else None)
        page.on("pageerror", lambda e: console.append(str(e)))
        opened = []   # wat het opdrachtenvenster in Instellingen wil openen
        serve.settings_hook = opened.append

        # 1. Eerste start, nog niet ingelogd: lege lijst met de knop naar Integraties, rechtsboven de status.
        # Zonder sessie komt een browser er niet in; het venster meldt zich eerst en krijgt de cookie.
        outsider = browser.new_page()
        refused = outsider.goto(BASE + "/").status
        outsider.close()
        check("een andere browser op deze Mac krijgt de app niet te zien", refused == 403, refused)
        page.goto(f"{BASE}/window/open?t={serve.SESSION}")
        page.wait_for_url(BASE + "/", timeout=5000)
        connect = page.get_by_role("button", name="Verbinden met Iconik…")
        connect.wait_for(timeout=5000)
        check("uitgelogd: lege lijst met Verbinden met Iconik, status rechtsboven", page.locator("#who").inner_text() == "Niet ingelogd")
        check("geen zijbalk en geen Voorkeuren meer in het venster", page.locator("aside, #settings, .nav").count() == 0)
        check("venstertitel zonder 'for Iconik'", page.title() == "Local Transcribe", page.title())
        connect.click()
        page.locator("#status").click()
        page.locator("#gear").click()
        page.wait_for_timeout(300)
        check("de knop en de status openen Integraties, het tandwiel de laatste tab", opened == ["integrations", "integrations", None], opened)

        # 2. Inloggen vanuit Instellingen › Integraties
        page.goto(BASE + "/settings-window?tab=integrations")
        page.locator("#login").click()
        page.locator("#logout").wait_for(timeout=10000)
        check("inloggen vanuit Instellingen: ingelogd", logged_in())
        check("Iconik kreeg een geldige PKCE-uitwisseling", iconik["pkce_ok"] == 1 and not iconik["bad"], iconik)
        page.goto(BASE + "/")
        page.get_by_text("Ingelogd bij Iconik").wait_for(timeout=5000)
        check("het opdrachtenvenster toont de verbinding", page.locator("#dot.on").count() == 1)

        # 3. Uitloggen
        page.goto(BASE + "/settings-window?tab=integrations")
        page.locator("#logout").click()
        page.locator("#login").wait_for(timeout=5000)
        check("uitloggen: inlog weg uit de sleutelhanger", not logged_in())

        # 4. Andere client-ID: eerst een ongeldige, dan een geldige (in hoofdletters)
        cid = page.locator("#f-iconik-client_id")
        cid.fill("geen-id")
        cid.press("Enter")
        page.get_by_text("Dat lijkt geen geldige client-ID.").wait_for(timeout=5000)
        check("ongeldige client-ID: melding, niets bewaard", app_settings.stored("iconik.client_id") == sorted(CLIENT_IDS)[0])
        other = sorted(CLIENT_IDS)[1]
        cid = page.locator("#f-iconik-client_id")
        cid.fill(other.upper())
        cid.press("Enter")
        page.wait_for_timeout(500)
        check("geldige client-ID: in kleine letters bewaard", app_settings.stored("iconik.client_id") == other)
        page.locator("#login").click()
        page.locator("#logout").wait_for(timeout=10000)
        check("ingelogd met de nieuwe client-ID", logged_in())

        # 5. Klik vanuit Iconik terwijl je uitgelogd bent: eerst inloggen, daarna staan de opdrachten in de rij
        page.locator("#logout").click()
        page.locator("#login").wait_for(timeout=5000)
        page.goto(f"{BASE}/transcribe/sleutel?asset_ids=A1,A2")
        page.wait_for_url(BASE + "/", timeout=10000)
        page.get_by_text("Titel A1").wait_for(timeout=10000)
        page.get_by_text("Titel A2").wait_for(timeout=10000)
        check("klik uit Iconik zonder inlog: inloggen, dan beide assets in de rij",
              logged_in() and {j.asset_id for j in serve.jobs} == {"A1", "A2"}, [j.asset_id for j in serve.jobs])
        status = page.request.get(f"{BASE}/transcribe/fout?asset_ids=A3", max_redirects=0).status
        errors[:] = [e for e in errors if "/transcribe/fout" not in e]   # deze 403 is de bedoeling
        check("verkeerde sleutel geweigerd", status == 403 and "A3" not in {j.asset_id for j in serve.jobs}, status)

        # 6. Op afstand en automatisch in Instellingen, en wat het opdrachtenvenster daarvan laat zien
        main = browser.new_page()   # het opdrachtenvenster naast Instellingen; meldt zich aan zoals het echte venster
        main.on("response", lambda r: errors.append(f"{r.status} {r.url}") if r.url.startswith(BASE) and r.status >= 400 else None)
        main.on("pageerror", lambda e: console.append(str(e)))
        main.goto(f"{BASE}/window/open?t={serve.SESSION}")
        main.wait_for_url(BASE + "/", timeout=5000)
        main.locator("#strip").get_by_text("Uit", exact=True).wait_for(timeout=5000)
        page.goto(BASE + "/settings-window?tab=integrations")
        page.locator("#f-iconik-worker_enabled").check()
        name = page.locator("#f-iconik-machine_name")
        name.wait_for(timeout=5000)
        check("opdrachten op afstand aan", app_settings.stored("iconik.worker_enabled") is True)
        # De werker kijkt nu echt bij de nep-Iconik; wat hij daar vindt maakt niet uit, wel dat "Uit" weg is.
        main.locator("#strip").get_by_text("Uit", exact=True).wait_for(state="detached", timeout=5000)
        check("de statusstrook toont dat op afstand aan staat", True)

        # Eigen naam voor deze Mac in Iconik: niet tijdens een opdracht (A1 en A2 wachten nog), daarna wel
        name.fill("Montage 3")
        name.press("Enter")
        page.get_by_text("Wacht tot de lopende opdracht klaar is.").wait_for(timeout=5000)
        check("naam van de Mac: geweigerd terwijl er opdrachten wachten", not app_settings.stored("iconik.machine_name"))
        real_busy, serve.busy = serve.busy, lambda: False
        name = page.locator("#f-iconik-machine_name")
        name.fill("Montage 3")
        name.press("Enter")
        page.get_by_text("Naam: Montage 3").wait_for(timeout=5000)
        check("naam van de Mac: bewaard en getoond", app_settings.stored("iconik.machine_name") == "Montage 3")
        name = page.locator("#f-iconik-machine_name")
        name.fill("")
        name.press("Enter")
        page.wait_for_timeout(500)
        check("naam van de Mac: leeg maken geeft de computernaam terug", app_settings.stored("iconik.machine_name") == ""
              and serve.remote.machine_name() == serve.remote.computer_name())
        serve.busy = real_busy

        # Automatisch transcriberen: schakelaar, ook audio, en waarop het wacht (Instellingen en statusstrook)
        page.locator("#f-iconik-auto_transcribe").check()
        page.locator("#f-iconik-auto_audio").wait_for(timeout=5000)
        check("automatisch transcriberen aan", app_settings.stored("iconik.auto_transcribe") is True)
        serve.remote.state["auto"] = {"ready": False, "reason": "gpu", "values": {"gpu": 63.0}}
        page.get_by_text("GPU 63% bezet").wait_for(timeout=10000)
        main.locator("#strip").get_by_text("GPU 63% bezet").wait_for(timeout=5000)
        check("Instellingen en de statusstrook zeggen waarop automatisch wacht", True)
        serve.remote.state["auto"] = {"ready": True, "reason": None, "values": {}}
        main.locator("#strip").get_by_text("Klaar: pakt een asset").wait_for(timeout=5000)
        check("en wanneer het klaar is", True)
        page.locator("#f-iconik-auto_audio").check()
        page.wait_for_timeout(500)
        check("ook audio aan", app_settings.stored("iconik.auto_audio") is True)
        page.locator("#f-iconik-auto_transcribe").uncheck()
        page.wait_for_timeout(500)
        check("automatisch transcriberen uit, audio-keuze blijft bewaard",
              app_settings.stored("iconik.auto_transcribe") is False and app_settings.stored("iconik.auto_audio") is True)
        check("Ook audio verdwijnt als automatisch uit staat", page.locator("#f-iconik-auto_audio").count() == 0)
        serve.remote.state["auto"] = None
        serve.jobs[0].set(auto=True)
        main.locator(".tag", has_text="Auto").first.wait_for(timeout=5000)
        check("automatisch opgepakte job heeft het label Auto", main.locator(".tag").count() == 1)
        serve.jobs[0].set(auto=False)
        page.locator("#f-iconik-worker_enabled").uncheck()
        page.wait_for_timeout(500)
        check("opdrachten op afstand uit", app_settings.stored("iconik.worker_enabled") is False)
        main.locator("#strip").get_by_text("Uit", exact=True).wait_for(timeout=5000)
        check("de statusstrook zegt dat op afstand uit staat", True)

        # De opslag in de strook, met automatisch uit: wat de app echt ziet, niet alleen dat er een map is ingesteld.
        folder = Path(tempfile.mkdtemp()) / "ProefISG"
        serve.storage_names["isg1"] = "Proefopslag"
        app_settings.update("iconik.mounts", lambda mounts: mounts.update(isg1=str(folder)))
        serve.seen.update(offline=None, looked=[])
        main.locator("#strip").get_by_text("Kijkt of de opslag bereikbaar is…").wait_for(timeout=5000)
        check("strook: eerst kijken, niet meteen 'gekoppeld'", main.locator("#strip").get_by_text("gekoppeld").count() == 0)
        serve.look_storage()
        main.locator("#strip").get_by_text("Proefopslag niet bereikbaar").wait_for(timeout=5000)
        check("strook: een map die er niet is heet niet bereikbaar, ook met automatisch uit", True)
        folder.mkdir()
        serve.look_storage()
        main.locator("#strip").get_by_text("Proefopslag gekoppeld").wait_for(timeout=5000)
        check("strook: en gekoppeld zodra hij er is", True)
        app_settings.update("iconik.mounts", lambda mounts: mounts.pop("isg1", None))
        main.close()
        check("client-ID bleef bij al die wijzigingen staan", app_settings.stored("iconik.client_id") == other)

        # 7. Een andere website probeert uit te loggen en de lijst te wissen
        evil = browser.new_page()
        evil.goto(FAKE + "/evil")
        evil.wait_for_function("document.title === 'klaar'", timeout=5000)
        evil.wait_for_timeout(500)
        check("andere website kan niet uitloggen", logged_in())
        check("andere website kan de lijst niet wissen", len(serve.jobs) == 2, len(serve.jobs))
        evil.close()

        # 8. Losse opdrachten verwijderen: selecteren en ✕, ⌫ (niet bij een wachtende), rechtsklik en "ook van deze Mac"
        with serve.jobs_lock:
            for job in serve.jobs:
                job.set(status="klaar")
            a1 = next(j for j in serve.jobs if j.asset_id == "A1")
            a2 = next(j for j in serve.jobs if j.asset_id == "A2")
            twin = serve.jobs.append(serve.job_list.Job(**dict(a1.view(), n=serve.jobs.next_number())))  # A1 nog een keer
            waiting = serve.jobs.append(serve.job_list.Job(**dict(a2.view(), n=serve.jobs.next_number(), asset_id="A3",
                                                                  item=a2.item.replace(":A2", ":A3"),
                                                                  title="Titel A3", status="wachtend")))
        path = serve.items.path
        kept = [path(serve.refine.appbase.TRANSCRIPTS_DIR, a1.item, ".json"), path(serve.refine.appbase.WAVES_DIR, a1.item, ".waves"),
                path(serve.refine.DRAFTS_DIR, a1.item, ".draft.json"), path(serve.refine.appbase.TRANSCRIPTS_DIR, a2.item, ".json")]
        for f in kept:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("{}")
        page.goto(BASE + "/")
        page.locator(f"#job-{waiting.n}").click(position={"x": 4, "y": 4})
        page.wait_for_timeout(300)
        check("wachtende opdracht: geselecteerd, maar zonder ✕", page.locator(f"#job-{waiting.n}.sel").count() == 1
              and page.locator(f"#job-{waiting.n} button.x").count() == 0)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(500)
        check("⌫ haalt een wachtende opdracht niet weg", any(j.n == waiting.n for j in serve.jobs))
        page.locator(f"#job-{a2.n}").click(position={"x": 4, "y": 4})
        page.locator(f"#job-{a2.n} button.x").click()
        page.locator(f"#job-{a2.n}").wait_for(state="detached", timeout=5000)
        check("✕ haalt één regel uit de lijst, de bestanden blijven", not any(j.n == a2.n for j in serve.jobs) and kept[3].exists())
        page.locator(f"#job-{twin.n}").click(button="right", position={"x": 4, "y": 4})
        menu = page.locator("#menu")
        check("rechtsklik: menu met de vier keuzes", menu.get_by_role("menuitem").count() == 4)
        check("rechtsklik: uitleg bij 'ook van deze Mac'", "mediabestand blijft altijd staan"
              in (menu.get_by_role("menuitem", name="Verwijder ook van deze Mac…").get_attribute("title") or ""))
        menu.get_by_role("menuitem", name="Verwijder ook van deze Mac…").click()
        dialog = page.get_by_role("dialog")
        check("bevestiging zegt dat het mediabestand blijft", dialog.get_by_text("Blijft altijd staan: het mediabestand zelf").count() == 1)
        serve.clear_log_hook = lambda: True
        dialog.get_by_role("button", name="Log wissen").click()
        dialog.get_by_text("Log gewist.").wait_for(timeout=5000)
        dialog.get_by_role("button", name="Verwijder").click()
        page.locator(f"#job-{twin.n}").wait_for(state="detached", timeout=5000)
        check("ook van deze Mac: beide regels van A1 weg, de wachtende blijft",
              [j.asset_id for j in serve.jobs] == ["A3"], [j.asset_id for j in serve.jobs])
        check("ook van deze Mac: de bestanden van A1 weg, die van A2 niet", not any(f.exists() for f in kept[:3]) and kept[3].exists())
        serve.clear_log_hook = None
        for job in serve.jobs:
            job.set(status="klaar")

        # Lijst opruimen vanuit het venster
        page.locator("#clear").click()
        page.get_by_text("Nog geen opdrachten").wait_for(timeout=5000)
        check("lijst opruimen vanuit het venster", not serve.jobs)

        # 9. Het venster Instellingen: eigen pagina, vier tabs, de titel volgt de tab, de laatste tab blijft.
        titles, opened = [], []
        serve.title_hook, serve.settings_hook = titles.append, opened.append
        outsider = browser.new_page()
        refused = outsider.goto(BASE + "/settings-window").status
        outsider.close()
        check("Instellingen: zonder sessie geen toegang", refused == 403, refused)
        page.goto(BASE + "/settings-window?tab=general")   # eerdere stappen lieten Integraties als laatste tab achter
        page.wait_for_function("document.title === 'Algemeen'", timeout=5000)
        check("Instellingen: vier tabs", page.get_by_role("tab").count() == 4, page.get_by_role("tab").count())
        page.get_by_role("tab", name="Bestanden").click()
        page.wait_for_function("document.title === 'Bestanden'", timeout=5000)
        page.wait_for_timeout(300)
        check("Instellingen: titel van het venster volgt de tab", titles[-1:] == ["Bestanden"], titles)
        page.reload()
        page.wait_for_function("document.title === 'Bestanden'", timeout=5000)
        check("Instellingen: de laatste tab blijft na opnieuw openen",
              page.get_by_role("tab", name="Bestanden").get_attribute("aria-selected") == "true")
        page.goto(BASE + "/settings-window?tab=integrations")
        page.wait_for_function("document.title === 'Integraties'", timeout=5000)
        check("Instellingen: opent op de gevraagde tab", page.locator('[data-panel="integrations"]').is_visible())
        check("Instellingen: ?tab= daarna uit het adres, opnieuw laden blijft op de huidige tab",
              page.url == BASE + "/settings-window", page.url)
        hdr = {"X-Local-Transcribe": "1"}
        check("Instellingen openen: geen GET", page.request.get(BASE + "/window/settings", headers=hdr).status == 405)
        check("Instellingen openen: niet zonder header", page.request.post(BASE + "/window/settings").status == 403)
        page.request.post(BASE + "/window/settings?tab=files", headers=hdr)
        page.request.post(BASE + "/window/settings?tab=elders", headers=hdr)
        check("Instellingen openen: de app krijgt de tab, een onbekende tab wordt geen tab", opened == ["files", None], opened)
        page.request.post(BASE + "/window/title", headers=hdr, data=json.dumps({"title": "  Twee   woorden "}))
        check("titel: spaties opgeschoond", titles[-1:] == ["Twee woorden"], titles)
        big = page.request.post(BASE + "/window/title", headers=hdr, data=json.dumps({"title": "x" * 2000})).status
        check("titel: te groot verzoek geweigerd", big == 413, big)
        errors[:] = [e for e in errors if "/window/settings" not in e and "/window/title" not in e
                     and not e.startswith("403 " + BASE + "/settings-window")]   # deze weigeringen zijn de bedoeling
        serve.title_hook = serve.settings_hook = None

        # 10. Instellingen, Algemeen en Transcriptie: getekend uit het schema, elke wijziging meteen bewaard.
        page.goto(BASE + "/settings-window?tab=general")
        app_lang = page.locator("#f-general-app_language")
        app_lang.wait_for(timeout=5000)
        check("Algemeen: taal van de app als keuzelijst", app_lang.input_value() == "nl", app_lang.input_value())
        app_lang.select_option("en")
        page.wait_for_function("document.title === 'General'", timeout=5000)
        check("Algemeen: andere taal bewaard, het venster komt terug in die taal", app_settings.stored("general.app_language") == "en")
        page.locator("#f-general-app_language").select_option("nl")
        page.wait_for_function("document.title === 'Algemeen'", timeout=5000)
        login_row = page.locator(".row", has=page.locator("#loginOn"))
        check("Algemeen: de schakelaar heet Opstarten bij inloggen (niet de knop van Iconik)",
              "Opstarten bij inloggen" in login_row.inner_text() and "Iconik" not in login_row.inner_text(), login_row.inner_text())
        page.locator("#loginOn").check()
        page.wait_for_timeout(500)
        check("Algemeen: opstarten bij inloggen aan", serve.login_item_on())
        page.locator("#loginOn").uncheck()
        page.wait_for_timeout(500)
        check("Algemeen: opstarten bij inloggen uit", not serve.login_item_on())

        page.get_by_role("tab", name="Transcriptie").click()
        spoken = page.locator("#f-transcription-language")
        spoken.wait_for(timeout=5000)
        check("Transcriptie: niet geteste talen staan erbij",
              "niet getest" in page.locator('#f-transcription-language option[value="de"]').inner_text())
        spoken.select_option("nl")
        page.wait_for_timeout(500)
        check("Transcriptie: gesproken taal bewaard", app_settings.stored("transcription.language") == "nl")
        page.get_by_text("Aanbevolen grenzen").wait_for(timeout=5000)
        page.get_by_role("button", name="Aanpassen…").click()
        free = page.locator("#f-transcription-auto_min_free_gb")
        free.fill("16")
        free.press("Enter")
        page.get_by_text("Eigen grenzen").wait_for(timeout=5000)
        check("Transcriptie: eigen grens bewaard", app_settings.stored("transcription.auto_min_free_gb") == 16)
        free = page.locator("#f-transcription-auto_min_free_gb")
        free.fill("-3")
        free.press("Enter")
        page.locator(".err").first.wait_for(timeout=5000)
        check("Transcriptie: ongeldige grens geweigerd met melding", app_settings.stored("transcription.auto_min_free_gb") == 16)
        page.get_by_role("button", name="Terug naar aanbevolen").click()
        page.get_by_text("Aanbevolen grenzen").wait_for(timeout=5000)
        check("Transcriptie: terug naar aanbevolen haalt de waarde uit het bestand",
              app_settings.stored("transcription.auto_min_free_gb") is None)
        check("Transcriptie: Geavanceerd is dicht", page.locator("#f-transcription-mlx_cache_gb").count() == 0)
        page.get_by_role("button", name="Geavanceerd").click()
        cache = page.locator("#f-transcription-mlx_cache_gb")
        cache.fill("0")
        cache.press("Enter")
        page.wait_for_timeout(500)
        check("Geavanceerd: buffer 0 mag (geen buffer)", app_settings.stored("transcription.mlx_cache_gb") == 0)

        # De originelen van Refine: één weghalen uit de lijst, daarna de rest leegmaken.
        source = serve.items.iconik_source(create=True)
        secret = serve.items.path(serve.refine.DRAFTS_DIR, f"{source}:A9", ".original.json")
        remaining = serve.items.path(serve.refine.DRAFTS_DIR, f"{source}:B7", ".original.json")
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text(json.dumps({"transcript": {"source": {"connector": "iconik", "title": "Vertrouwelijk interview"}}}))
        remaining.write_text(json.dumps({"transcript": {"source": {"connector": "iconik", "title": "Ander interview"}}}))
        page.reload()
        page.get_by_text("Originelen van 2 gecorrigeerde transcripten").wait_for(timeout=5000)
        page.get_by_role("button", name="Toon lijst…").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Vertrouwelijk interview").check()
        dialog.get_by_role("button", name="Verwijder geselecteerde").click()
        dialog.get_by_text("Vertrouwelijk interview").wait_for(state="detached", timeout=5000)
        check("originelen: één weghalen uit de lijst", not secret.exists() and remaining.exists())
        dialog.get_by_role("button", name="Klaar").click()
        page.get_by_role("button", name="Leegmaken").click()
        page.get_by_role("button", name="Zeker? Klik nogmaals").click()
        page.get_by_text("Geen originelen van gecorrigeerde transcripten").wait_for(timeout=5000)
        check("originelen: leegmaken werkt (de route ontbrak tot nu)", not remaining.exists())

        # 11. Instellingen, Integraties: Iconik in de lijst, op afstand en automatisch, uit- en inloggen, − en +.
        # De opdrachten uit stap 5 staan nog in de rij (hier draait geen werker); met iets in de rij weigert de app
        # een nieuwe naam en het verwijderen van Iconik. Eerst leeg, verderop wordt die weigering zelf getest.
        while not serve.work.empty():
            serve.work.get_nowait()
        page.goto(BASE + "/settings-window?tab=integrations")
        page.locator("#logout").wait_for(timeout=5000)
        check("Integraties: één Iconik in de lijst, ingelogd", page.locator(".item").count() == 1 and logged_in())
        check("Integraties: + kan altijd (stap 5f: meer Iconiks)", page.locator("#addIntegration").is_enabled()
              and page.locator("#untested").count() == 0)
        icon = page.locator(".item img")
        icon.wait_for(timeout=10000)   # het icoon wordt op de achtergrond opgezocht; de tab ververst elke 3 seconden
        check("Integraties: het icoon van de Iconik-omgeving zelf, van haar eigen host, opgehaald door de app",
              icon.get_attribute("src") == "data:image/png;base64," + base64.b64encode(ICON_PNG).decode()
              and icon.evaluate("img => img.complete && img.naturalWidth > 0"), (icon.get_attribute("src") or "")[:60])
        page.locator("#f-iconik-worker_enabled").check()
        name = page.locator("#f-iconik-machine_name")
        name.wait_for(timeout=5000)
        check("Integraties: opdrachten op afstand aan", app_settings.stored("iconik.worker_enabled") is True)
        name.fill("Testmac")
        name.press("Enter")
        page.wait_for_timeout(500)
        check("Integraties: naam van deze Mac bewaard", app_settings.stored("iconik.machine_name") == "Testmac")
        page.locator("#f-iconik-auto_transcribe").check()
        page.locator("#f-iconik-auto_audio").wait_for(timeout=5000)
        check("Integraties: automatisch transcriberen aan", app_settings.stored("iconik.auto_transcribe") is True)
        page.locator("#f-iconik-auto_transcribe").uncheck()
        page.wait_for_timeout(500)
        page.locator("#f-iconik-worker_enabled").uncheck()
        page.wait_for_timeout(500)
        check("Integraties: automatisch en op afstand weer uit",
              app_settings.stored("iconik.auto_transcribe") is False and app_settings.stored("iconik.worker_enabled") is False)
        page.locator("#logout").click()
        page.locator("#login").wait_for(timeout=5000)
        check("Integraties: uitloggen", not logged_in())
        page.locator("#login").click()
        page.locator("#logout").wait_for(timeout=10000)
        check("Integraties: inloggen vanuit Instellingen komt terug in Instellingen",
              logged_in() and page.url == BASE + "/settings-window", page.url)
        page.locator("#removeIntegration").click()
        page.get_by_role("button", name="Annuleer").click()
        check("Integraties: − en dan Annuleer laat alles staan", logged_in() and app_settings.integrations())
        serve.work.put(("bezig", "test"))   # alsof er een opdracht loopt
        page.locator("#removeIntegration").click()
        page.locator("#removeConfirm").click()
        page.get_by_text("Wacht tot de lopende opdracht klaar is.").wait_for(timeout=5000)
        check("Integraties: − tijdens een opdracht geweigerd met melding", logged_in() and app_settings.integrations())
        serve.work.get_nowait()
        page.locator("#removeConfirm").click()
        page.get_by_text("Kies + om Iconik toe te voegen.").wait_for(timeout=5000)
        check("Integraties: − verwijdert de koppeling en logt uit",
              not logged_in() and not app_settings.integrations() and app_settings.stored("iconik.client_id") is None)
        page.locator("#addIntegration").click()
        check("Integraties: + vraagt een naam, de eerste heet Iconik, zonder melding over meer omgevingen",
              page.locator("#addName").input_value() == "Iconik" and page.locator("#addUntested").count() == 0)
        page.locator("#addConfirm").click()
        cid = page.locator("#f-iconik-client_id")
        cid.wait_for(timeout=5000)
        app_settings.raw_put("iconik.host", FAKE)   # het schema wil https; de nep-Iconik draait op http
        cid.fill(other)
        cid.press("Enter")
        page.locator("#login:not([disabled])").wait_for(timeout=5000)
        page.locator("#login").click()
        page.locator("#logout").wait_for(timeout=10000)
        check("Integraties: + en een client-ID, daarna ingelogd", logged_in() and app_settings.stored("iconik.client_id") == other)

        # 12. Twee Iconiks (stap 5e): elk een regel in de lijst met een eigen naam; een tweede koppeling naar dezelfde
        # omgeving zegt dat en heeft geen schakelaars voor de rij; een opdracht toont bij welke Iconik hij hoort.
        first = app_settings.instances("iconik")[0]
        second = app_settings.add_integration("iconik")
        app_settings.raw_put("iconik.host", FAKE, second)
        app_settings.put("iconik.client_id", sorted(CLIENT_IDS)[0], second)
        iconik["tokens"].add("tweede-token")
        transcribe.save_login({"access_token": "tweede-token", "refresh_token": "r2", "expires_at": time.time() + 3600}, second)
        page.goto(BASE + "/settings-window?tab=integrations")
        page.locator(".item >> nth=1").wait_for(timeout=5000)
        names = [t.split("\n")[0] for t in page.locator(".item .nm").all_inner_texts()]
        check("twee Iconiks: twee regels, Iconik en Iconik (2)", names == ["Iconik", "Iconik (2)"], names)
        check("twee Iconiks: de eerste is gekozen", page.locator(f'.item[data-instance="{first}"]').get_attribute("aria-current") == "true")
        page.locator(f'.item[data-instance="{second}"]').click()
        page.wait_for_function("document.querySelector('.iname').textContent === 'Iconik (2)'", timeout=5000)
        check("twee Iconiks: de tweede gekozen, het naamveld toont de automatische naam",
              page.locator("#f-iconik-name").get_attribute("placeholder") == "Iconik (2)")
        page.locator("#f-iconik-name").fill("Archief")
        page.locator("#f-iconik-name").press("Enter")
        page.wait_for_function("document.querySelector('.iname').textContent === 'Archief'", timeout=5000)
        check("twee Iconiks: eigen naam bewaard bij de tweede, niet bij de eerste",
              app_settings.stored("iconik.name", second) == "Archief" and app_settings.stored("iconik.name", first) is None)
        page.locator("#sameEnv").wait_for(timeout=10000)   # het domein wordt op de achtergrond opgehaald
        check("twee Iconiks: dezelfde omgeving als de eerste, met de naam van de eerste erin",
              "Iconik" in page.locator("#sameEnv").inner_text() and page.locator("#f-iconik-worker_enabled").count() == 0
              and page.locator("#f-iconik-iconik_language").count() == 0 and page.locator("#installActions").count() == 0)
        page.locator(f'.item[data-instance="{first}"]').click()
        page.locator("#f-iconik-worker_enabled").wait_for(timeout=5000)
        check("twee Iconiks: de eerste heeft de schakelaars voor de rij, zonder melding", page.locator("#sameEnv").count() == 0
              and page.locator(".iname").inner_text() == "Iconik")
        with serve.jobs_lock:
            serve.jobs.append(serve.job_list.Job(n=serve.jobs.next_number(), item=f"{second}:abcdef12", asset_id="abcdef12",
                                                 title="Uit het archief", status="klaar"))
            serve.jobs.append(serve.job_list.Job(n=serve.jobs.next_number(), item=f"{first}:abcdef13", asset_id="abcdef13",
                                                 title="Uit de eerste", status="klaar"))
        page.goto(BASE + "/")
        page.locator(".tag.source").first.wait_for(timeout=5000)
        tags = {page.locator(".job", has_text=t).locator(".tag.source").inner_text() for t in ("Uit het archief", "Uit de eerste")}
        check("twee Iconiks: elke opdracht toont bij welke Iconik hij hoort", tags == {"Archief", "Iconik"}, tags)
        check("twee Iconiks: rechtsboven: ingelogd bij beide", page.locator("#who").inner_text() == "Ingelogd bij 2 Iconiks",
              page.locator("#who").inner_text())

        # 13. + en − (stap 5f): een derde Iconik met de vraag om een naam en de melding dat meerdere omgevingen nog niet
        # echt getest zijn; − met "ook van deze Mac"; de knoppen uit Iconik halen alleen waar dat kan.
        page.goto(BASE + "/settings-window?tab=integrations")
        page.locator("#untested").wait_for(timeout=5000)
        check("+ en −: onder de lijst de melding dat meerdere omgevingen nog niet getest zijn",
              "nog niet getest" in page.locator("#untested").inner_text())
        page.locator("#addIntegration").click()
        check("+: de naam staat klaar als Iconik (3), met de melding", page.locator("#addName").input_value() == "Iconik (3)"
              and page.locator("#addUntested").count() == 1)
        page.locator("#addCancel").click()
        check("+ en Annuleer: niets toegevoegd", len(app_settings.instances("iconik")) == 2)
        page.locator("#addIntegration").click()
        page.locator("#addName").fill("Klant X")
        page.locator("#addName").press("Enter")
        page.wait_for_function("document.querySelector('.iname') && document.querySelector('.iname').textContent === 'Klant X'", timeout=5000)
        third = app_settings.instances("iconik")[-1]
        check("+: de nieuwe Iconik is gekozen, met zijn naam bewaard", len(app_settings.instances("iconik")) == 3
              and app_settings.stored("iconik.name", third) == "Klant X" and page.locator("#f-iconik-client_id").count() == 1)
        kept = transcribe.TRANSCRIPTS_DIR / third / "abcdef14.json"
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_text('{"format": "local-transcribe"}')
        other_kept = transcribe.TRANSCRIPTS_DIR / first / "abcdef15.json"
        other_kept.parent.mkdir(parents=True, exist_ok=True)
        other_kept.write_text('{"format": "local-transcribe"}')
        with serve.jobs_lock:
            serve.jobs.append(serve.job_list.Job(n=serve.jobs.next_number(), item=f"{third}:abcdef14", asset_id="abcdef14",
                                                 title="Van klant X", status="klaar"))
        page.locator("#removeIntegration").click()
        check("−: de titel noemt de Iconik, 'ook van deze Mac' staat uit, knoppen weghalen kan hier niet (niet ingelogd)",
              page.locator(".sheet h2").inner_text() == "Klant X verwijderen?" and not page.locator("#removeForget").is_checked()
              and page.locator("#removeButtons").count() == 0)
        page.locator("#removeForget").check()
        page.locator("#removeConfirm").click()
        page.wait_for_function("!document.querySelector('.sheet')", timeout=5000)
        with serve.jobs_lock:
            rows = [job.item for job in serve.jobs]
        check("− met 'ook van deze Mac': de Iconik, zijn bestanden en zijn regels weg; de andere Iconiks houden de hunne",
              third not in app_settings.instances("iconik") and not kept.exists() and not kept.parent.exists()
              and other_kept.exists() and not any(r.startswith(third) for r in rows), (app_settings.instances("iconik"), rows))
        page.locator(f'.item[data-instance="{first}"]').click()
        page.wait_for_function("document.querySelector('.iname').textContent === 'Iconik'", timeout=5000)
        page.locator("#removeIntegration").click()
        check("−: knoppen weghalen niet bij een Iconik waarvan een andere koppeling dezelfde omgeving gebruikt",
              page.locator("#removeButtons").count() == 0)
        page.locator("#removeCancel").click()
        page.locator(f'.item[data-instance="{second}"]').click()
        page.wait_for_function("document.querySelector('.iname').textContent === 'Archief'", timeout=5000)
        page.locator("#removeIntegration").click()
        page.locator("#removeConfirm").click()
        page.wait_for_function("document.querySelectorAll('.item').length === 1", timeout=5000)
        page.locator("#removeIntegration").click()
        check("−: weer één Iconik, ingelogd: nu kan 'De knoppen uit Iconik halen' wel (standaard uit)",
              page.locator("#removeButtons").count() == 1 and not page.locator("#removeButtons").is_checked()
              and page.locator("#untested").count() == 0)
        page.locator("#removeCancel").click()

        browser.close()
    except Exception as error:
        # Een stap liep vast: zeg welke, en welke verzoeken de ontvanger weigerde.
        here = [f.lineno for f in traceback.extract_tb(error.__traceback__) if f.filename.endswith("test_e2e.py")]
        check("browsertest liep vast", False, f"regel {here[-1] if here else '?'}: "
              f"{type(error).__name__}: {str(error).splitlines()[0]}; "
              f"fouten van de ontvanger: {errors or 'geen'}; pagina: {page.url}")
        browser.close()

check("geen 4xx of 5xx van de ontvanger voor het venster", not errors, errors)
check("geen JavaScript-fouten", not console, console)
app.shutdown()
fake.shutdown()
print("ALLES OK" if not fails else f"{fails} FOUT(EN)")
sys.exit(1 if fails else 0)
