"""De routetabel van de ontvanger: elke route staat er één keer in, met wie er bij mag.

Vastgelegd bij stap 4c (van if-ketens naar een tabel): dezelfde routes als op 4fabc1b, met dezelfde bescherming.
Een nieuwe route komt hier bij, zodat bescherming een bewuste keuze is.

Draaien: uv run --no-project --with pynacl --with requests --with numpy python -B tests/test_routes.py
"""
import http.client
import sys
import tempfile
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, "app")
kr = types.ModuleType("keyring")
kr.get_password = lambda *a: None
kr.set_password = kr.delete_password = lambda *a: None
sys.modules["keyring"] = kr
for name in ("mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)

import transcribe, serve  # noqa: E402,E401

transcribe.L = lambda nl, en: nl
tmp = Path(tempfile.mkdtemp())
transcribe.CONFIG_PATH = tmp / "config.json"
serve.jobs_path = lambda: tmp / "jobs.json"
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


# --- 1. de tabel zelf
# pad: (methodes, sessie nodig, beschermd, soort)
W, P = ("GET",), ("POST",)
EXPECTED = {
    "/": (W, True, False, "window"), "/state": (W, True, False, "window"),
    "/settings-window": (W, True, False, "window"), "/settings-data": (W, True, False, "window"),
    "/window/open": (W, False, False, "window"),
    "/window/state": (W, False, False, "app"), "/window/show": (("GET", "POST"), False, True, "app"),
    "/settings": (P, True, True, "window"), "/settings/reset": (P, True, True, "window"),
    "/window/title": (P, True, True, "window"), "/window/settings": (P, True, True, "window"),
    "/mount/change": (P, True, True, "window"), "/mount/remove": (P, True, True, "window"),
    "/update/install": (P, True, True, "window"), "/update/open": (P, True, True, "window"),
    "/worker": (P, True, True, "window"), "/login-item": (P, True, True, "window"),
    "/asset/open": (P, True, True, "window"), "/actions/install": (P, True, True, "window"),
    "/log/clear": (P, True, True, "window"), "/jobs/remove": (P, True, True, "window"), "/local/add": (P, True, True, "window"),
    "/integrations/remove": (P, True, True, "window"), "/integrations/add": (P, True, True, "window"), "/clear": (P, True, True, "window"),
    "/logout": (P, True, True, "window"),
    "/login": (W, True, False, "window"), "/oauth/callback": (W, False, False, "link"),
    "/transcribe": (W, False, False, "link"),
    "/refine": (W, True, False, "window"), "/refine/originals": (W, True, False, "window"),
    "/refine/originals/clear": (P, True, True, "window"), "/refine/originals/remove": (P, True, True, "window"),
    "/refine/view": (W, True, False, "window"), "/refine/chunk": (W, True, False, "window"),
    "/refine/waves": (W, True, False, "window"), "/refine/draft": (P, True, True, "window"),
    "/refine/discard": (P, True, True, "window"), "/refine/save": (P, True, True, "window"),
    **{f"/refine/subtitles/{a}": (P, True, True, "window")
       for a in ("preview", "make", "import", "draft", "write", "write-as", "fit", "discard")},
}
have = {p: (r.methods, r.session, r.protected, r.kind) for p, r in serve.ROUTES.items()}
check("de tabel heeft precies de verwachte routes", set(have) == set(EXPECTED),
      (sorted(set(have) - set(EXPECTED)), sorted(set(EXPECTED) - set(have))))
wrong = {p: (have[p], EXPECTED[p]) for p in set(have) & set(EXPECTED) if have[p] != EXPECTED[p]}
check("elke route heeft de verwachte methodes, sessie, bescherming en soort", not wrong, wrong)
check("één route met iets na het pad: /transcribe/<sleutel>, open, van buiten",
      {p: (r.session, r.kind) for p, r in serve.PREFIXES.items()} == {"/transcribe/": (False, "link")})

OLD_PROTECTED = {"/clear", "/mount/change", "/mount/remove", "/actions/install", "/update/open", "/update/install",
                 "/worker", "/login-item", "/settings", "/logout", "/window/show", "/asset/open", "/window/settings",
                 "/window/title", "/integrations/remove", "/integrations/add", "/jobs/remove", "/log/clear"}
check("alles wat op 4fabc1b beschermd was, is het nog", OLD_PROTECTED <= serve.protected_paths(),
      OLD_PROTECTED - serve.protected_paths())
check("elke route die POST aanneemt is beschermd", all(r.protected for r in serve.ROUTES.values() if "POST" in r.methods),
      [p for p, r in serve.ROUTES.items() if "POST" in r.methods and not r.protected])
check("een beschermde route neemt geen GET aan (behalve /window/show voor oudere versies)",
      [p for p, r in serve.ROUTES.items() if r.protected and "GET" in r.methods] == ["/window/show"])
check("niets onder de API-voorvoegsel zolang die API er niet is",
      not [p for p in list(serve.ROUTES) + list(serve.PREFIXES) if p.startswith(serve.API_PREFIX)])
for bad, kind in (("/api/v1/x", "window"), ("/iets", "api"), ("/", "window")):
    try:
        serve.route(bad, kind=kind)(lambda h, r: None)
        refused = False
    except ValueError:
        refused = True
    check(f"weigert een route {bad} als {kind}", refused)

# --- 2. de regels in de praktijk
OK = lambda *a, **k: {"ok": True}  # noqa: E731
for name in ("install_update", "install_actions", "change_mount", "set_login_item", "clear_finished", "queue_request"):
    setattr(serve, name, lambda *a, **k: None)
for name in ("remove_jobs", "remove_integration", "apply_settings", "reset_settings", "app_state"):
    setattr(serve, name, OK)
serve.client = lambda *a: None
serve.system.open_url = lambda *a: None
serve.remote.resume = lambda: None
transcribe.clear_login = lambda *a: None
R = serve.refine
for name in ("save_to_iconik", "preview_subtitles", "make_subtitles", "import_subtitles", "write_subtitles", "fit_subtitles"):
    setattr(R, name, OK)
R.save_draft = R.save_subtitles = lambda *a: "nu"
for name in ("clear_originals", "remove_original", "discard_draft", "discard_subtitles"):
    setattr(R, name, lambda *a, **k: None)
serve.settings.update(key="k", hf_token=None, model="m", language=None, mount=None, idle_exit=False)
server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()


def ask(method, path, session=True, header=True, body=b"{}"):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", f"localhost:{PORT}")
    if session:
        c.putheader("Cookie", f"{serve.SESSION_COOKIE}={serve.SESSION}")
    if header:
        c.putheader("X-Local-Transcribe", "1")
    if method == "POST":
        c.putheader("Content-Length", str(len(body)))
    c.endheaders()
    if method == "POST" and body:
        c.send(body)
    r = c.getresponse()
    r.read()
    return r.status


def with_asset(path):
    return path + "?asset=abcdef12" if path.startswith("/refine/") else path


protected = sorted(p for p, r in serve.ROUTES.items() if r.protected and p != "/window/show")
answers = {p: (ask("POST", with_asset(p)), ask("POST", with_asset(p), session=False), ask("POST", with_asset(p), header=False),
               ask("GET", with_asset(p)), ask("GET", with_asset(p), header=False)) for p in protected}
check("beschermd: POST met sessie en header werkt", all(a[0] == 200 for a in answers.values()),
      {p: a[0] for p, a in answers.items() if a[0] != 200})
check("beschermd: zonder sessie 403", all(a[1] == 403 for a in answers.values()))
check("beschermd: zonder header 403", all(a[2] == 403 for a in answers.values()))
check("beschermd: GET met header 405, zonder header 403", all(a[3] == 405 and a[4] == 403 for a in answers.values()),
      {p: a[3:] for p, a in answers.items() if a[3:] != (405, 403)})
check("/window/show: zonder sessie met header, ook als GET; zonder header niet",
      ask("POST", "/window/show", session=False) == 200 and ask("GET", "/window/show", session=False) == 200
      and ask("GET", "/window/show", session=False, header=False) == 403)
check("open routes nemen geen POST aan zonder sessie (403, niet 404)",
      all(ask("POST", p, session=False) == 403 for p in ("/transcribe/k", "/oauth/callback", "/window/open", "/window/state")))
check("venster-routes zonder sessie: 403", all(ask("GET", p, session=False) == 403 for p in ("/", "/state", "/refine", "/login")))
check("onbekend pad: 403 zonder sessie, 404 met", ask("GET", "/onbekend", session=False) == 403 and ask("GET", "/onbekend") == 404
      and ask("POST", "/onbekend") == 404)
check("de knop in Iconik met de sleutel werkt zonder sessie", ask("GET", "/transcribe/k?asset_ids=abcdef12", session=False, header=False) == 303)
c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
c.putrequest("GET", "/state", skip_host=True)
c.putheader("Host", f"evil.example:{PORT}")
c.putheader("Cookie", f"{serve.SESSION_COOKIE}={serve.SESSION}")
c.endheaders()
check("een andere Host wordt geweigerd, ook met een geldige sessie", c.getresponse().status == 403)

server.shutdown()
print("\nALLES OK" if not fails else f"\n{fails} FOUT")
sys.exit(1 if fails else 0)
