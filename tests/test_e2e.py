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
from playwright.sync_api import sync_playwright  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


# --- Nep-Iconik: OAuth met PKCE, token, gebruiker, knoppen, titels
CLIENT_IDS = {"11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"}
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
        if not url.path.startswith("/API/"):
            return self.send(404, {})
        if not self.authorized():
            return self.send(401, {"error": "unauthorized"})
        path = url.path.removeprefix("/API/")
        if path == "users/v1/users/current/":
            return self.send(200, {"first_name": "Test", "last_name": "Gebruiker", "email": "test@example.com"})
        if path.startswith("assets/v1/custom_actions/"):
            return self.send(200, {"objects": [{"context": c, "url": serve.ACTION_URL} for c in serve.ACTION_CONTEXTS]})
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
        settings = page.locator('.nav[data-view="settings"]')
        account = page.locator("#account")

        # 1. Eerste start, nog niet ingelogd: het venster opent op Instellingen
        page.goto(BASE + "/")
        account.get_by_text("Niet ingelogd.").wait_for(timeout=5000)
        check("uitgelogd: Instellingen open met 'Niet ingelogd'", True)

        # 2. Inloggen via de link
        account.get_by_role("link", name="Log in bij Iconik").click()
        page.wait_for_url(BASE + "/", timeout=10000)
        settings.click()
        account.get_by_text("Ingelogd als Test Gebruiker (test@example.com).").wait_for(timeout=5000)
        check("inloggen via de link: ingelogd als Test Gebruiker", logged_in())
        check("Iconik kreeg een geldige PKCE-uitwisseling", iconik["pkce_ok"] == 1 and not iconik["bad"], iconik)

        # 3. Uitloggen
        account.get_by_role("link", name="Log uit").click()
        account.get_by_text("Niet ingelogd.").wait_for(timeout=5000)
        check("uitloggen: inlog weg uit de sleutelhanger", not logged_in())

        # 4. Andere client-ID: eerst een ongeldige, dan een geldige
        account.get_by_text("Andere Client ID gebruiken").click()
        account.locator('input[name="client_id"]').fill("geen-id")
        account.get_by_role("button", name="Opslaan en inloggen").click()
        page.get_by_text("Dat lijkt geen geldige client-ID.").wait_for(timeout=5000)
        check("ongeldige client-ID: melding, niets bewaard", transcribe.load_config()["client_id"] == sorted(CLIENT_IDS)[0])
        other = sorted(CLIENT_IDS)[1]
        account.locator('input[name="client_id"]').fill(other.upper())
        account.get_by_role("button", name="Opslaan en inloggen").click()
        page.wait_for_url(BASE + "/", timeout=10000)
        settings.click()
        account.get_by_text("Ingelogd als").wait_for(timeout=5000)
        check("geldige client-ID: bewaard en ingelogd", transcribe.load_config()["client_id"] == other and logged_in())

        # 5. Klik vanuit Iconik terwijl je uitgelogd bent: eerst inloggen, daarna staan de opdrachten in de rij
        account.get_by_role("link", name="Log uit").click()
        account.get_by_text("Niet ingelogd.").wait_for(timeout=5000)
        page.goto(f"{BASE}/transcribe/sleutel?asset_ids=A1,A2")
        page.wait_for_url(BASE + "/", timeout=10000)
        page.get_by_text("Titel A1").wait_for(timeout=10000)
        page.get_by_text("Titel A2").wait_for(timeout=10000)
        check("klik uit Iconik zonder inlog: inloggen, dan beide assets in de rij",
              logged_in() and {j["asset_id"] for j in serve.jobs} == {"A1", "A2"}, [j["asset_id"] for j in serve.jobs])
        status = page.request.get(f"{BASE}/transcribe/fout?asset_ids=A3", max_redirects=0).status
        errors[:] = [e for e in errors if "/transcribe/fout" not in e]   # deze 403 is de bedoeling
        check("verkeerde sleutel geweigerd", status == 403 and "A3" not in {j["asset_id"] for j in serve.jobs}, status)

        # 6. Schakelaars en keuzes in Instellingen
        settings.click()
        page.locator("#workerOn").check()
        page.wait_for_timeout(500)
        check("opdrachten op afstand aan", transcribe.load_config().get("worker_enabled") is True)

        # Auto transcribe: schakelaar en vinkje onder opdrachten op afstand, status van de meter
        page.locator("#autoOn").check()
        page.wait_for_timeout(500)
        check("automatisch transcriberen aan", transcribe.load_config().get("auto_transcribe") is True)
        serve.remote.state["auto"] = {"ready": False, "reason": "gpu", "values": {"gpu": 63.0}}
        page.get_by_text("GPU 63% bezet").wait_for(timeout=5000)
        check("het venster zegt waarop auto wacht", True)
        gap = page.locator(".autostate").evaluate("e => parseFloat(getComputedStyle(e).marginBottom)")
        check("ruimte onder de statusregel, zodat het volgende onderdeel los staat", gap > 10, gap)
        serve.remote.state["auto"] = {"ready": True, "reason": None, "values": {}}
        page.get_by_text("Klaar: pakt een asset").wait_for(timeout=5000)
        check("en wanneer hij klaar is", True)
        page.locator("#autoAudio").check()
        page.wait_for_timeout(500)
        check("ook audio aan", transcribe.load_config().get("auto_audio") is True)
        page.locator("#autoOn").uncheck()
        page.wait_for_timeout(500)
        check("automatisch transcriberen uit, audio-keuze blijft bewaard",
              transcribe.load_config().get("auto_transcribe") is False and transcribe.load_config().get("auto_audio") is True)
        check("vinkje voor audio verdwijnt als auto uit staat", page.locator("#autoAudio").count() == 0)
        serve.remote.state["auto"] = None
        serve.jobs[0]["auto"] = True
        page.locator('.nav[data-view="jobs"]').click()
        page.locator(".tag", has_text="Auto").first.wait_for(timeout=5000)
        check("automatisch opgepakte job heeft het label Auto", page.locator(".tag").count() == 1)
        serve.jobs[0]["auto"] = False
        settings.click()

        page.locator("#workerOn").uncheck()
        page.wait_for_timeout(500)
        check("opdrachten op afstand uit", transcribe.load_config().get("worker_enabled") is False)
        page.locator("#loginOn").check()
        page.wait_for_timeout(500)
        check("opstarten bij inloggen aan: LaunchAgent staat er", serve.LOGIN_PLIST.exists())
        page.locator("#loginOn").uncheck()
        page.wait_for_timeout(500)
        check("opstarten bij inloggen uit: LaunchAgent weg", not serve.LOGIN_PLIST.exists())
        page.locator("#lang").select_option("en")
        account.get_by_role("link", name="Log out").wait_for(timeout=5000)
        check("taal van de app naar Engels", transcribe.load_config().get("app_language") == "en")
        page.locator("#lang").select_option("nl")
        account.get_by_role("link", name="Log uit").wait_for(timeout=5000)
        page.locator("#spoken").select_option("en")
        page.wait_for_timeout(500)
        check("gesproken taal bewaard", transcribe.load_config().get("language") == "en")
        check("client-ID bleef bij al die wijzigingen staan", transcribe.load_config().get("client_id") == other)

        # 7. Een andere website probeert uit te loggen en de lijst te wissen
        evil = browser.new_page()
        evil.goto(FAKE + "/evil")
        evil.wait_for_function("document.title === 'klaar'", timeout=5000)
        evil.wait_for_timeout(500)
        check("andere website kan niet uitloggen", logged_in())
        check("andere website kan de lijst niet wissen", len(serve.jobs) == 2, len(serve.jobs))
        evil.close()

        # 8. Lijst opruimen vanuit het venster
        for job in serve.jobs:
            job["status"] = "klaar"
        page.locator('.nav[data-view="jobs"]').click()
        page.locator("#clear").click()
        page.get_by_text("Nog geen opdrachten").wait_for(timeout=5000)
        check("lijst opruimen vanuit het venster", not serve.jobs)

        browser.close()
    except Exception as error:
        # Een stap liep vast: zeg welke, en welke verzoeken de ontvanger weigerde.
        check("browsertest liep vast", False, f"{type(error).__name__}: {str(error).splitlines()[0]}; "
              f"fouten van de ontvanger: {errors or 'geen'}; pagina: {page.url}")
        browser.close()

check("geen 4xx of 5xx van de ontvanger voor het venster", not errors, errors)
check("geen JavaScript-fouten", not console, console)
app.shutdown()
fake.shutdown()
print("ALLES OK" if not fails else f"{fails} FOUT(EN)")
sys.exit(1 if fails else 0)
