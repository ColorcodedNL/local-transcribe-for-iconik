"""updater.py: handtekening, pakketcontrole, installeren en zoeken. Draaien vanuit de projectmap:

    uv run --with pynacl --with requests python tests/test_updater.py
"""
import base64, hashlib, importlib.util, io, json, sys, tempfile, threading, zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from nacl.signing import SigningKey
sys.path.insert(0, "app")
import updater, app_shell

KEY = SigningKey.generate(); PUB = base64.b64encode(bytes(KEY.verify_key)).decode()
OTHER = SigningKey.generate()
fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))

BASE = {"app_main.py": b"# a", "transcribe.py": b'APP_VERSION = "x"', "serve.py": b"# s", "ui.html": b"<html>", "updater.py": b"# u"}
def package(version, files=None, shell=1, tamper=None, members=None, manifest_version=None):
    files = dict(BASE if files is None else files)
    manifest = {"version": manifest_version or version, "requires_shell": shell,
                "files": {n: tamper.get(n) if tamper and n in tamper else hashlib.sha256(b).hexdigest() for n, b in files.items()}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for n, b in (members if members is not None else files).items():
            z.writestr(n, b)
    return buf.getvalue()
def sign(data, key=KEY): return base64.b64encode(key.sign(data).signature)
def run(version, data, key=KEY, running="0.2.0", shell=1, expected=None):
    code = Path(tempfile.mkdtemp()) / "code"
    store = {"https://x/z": data, "https://x/s": sign(data, key)}
    info = {"version": expected or version, "zip_url": "https://x/z", "sig_url": "https://x/s"}
    try:
        got = updater.install(info, running, shell, PUB, code, get=lambda u, l: store[u])
        return got, None, code
    except updater.UpdateError as e:
        return None, e, code

got, err, code = run("0.2.1", package("0.2.1"))
check("goede update", got == "0.2.1" and (code/"0.2.1"/"app_main.py").read_bytes() == b"# a" and json.loads((code/"current.json").read_text())["version"] == "0.2.1", err)
check("  manifest staat erbij", (code/"0.2.1"/"manifest.json").is_file())
check("  geen resten van installeren", not list(code.glob(".*")), list(code.glob(".*")))

for name, kw, key in [
    ("handtekening van andere sleutel", {}, OTHER),
]:
    got, err, code = run("0.2.1", package("0.2.1"), key=key)
    check(name, got is None and err and "handtekening" in err.nl and not (code/"current.json").exists())

data = package("0.2.1")
store = {"z": data[:-1] + bytes([data[-1] ^ 1]), "s": sign(data)}
try: updater.install({"version": "0.2.1", "zip_url": "z", "sig_url": "s"}, "0.2.0", 1, PUB, Path(tempfile.mkdtemp())/"c", get=lambda u, l: store[u]); ok = False
except updater.UpdateError as e: ok = "handtekening" in e.nl
check("zip aangepast na ondertekenen", ok)

cases = [
 ("hash in manifest klopt niet (wel ondertekend)", package("0.2.1", tamper={"serve.py": "0"*64}), "niet in orde"),
 ("vraagt nieuwere shell", package("0.2.1", shell=2), "nieuwe installatie"),
 ("extra bestand dat niet in het manifest staat", package("0.2.1", members={**BASE, "evil.py": b"x"}), "niet in orde"),
 ("bestandsnaam met ../", package("0.2.1", files={**BASE, "../evil.py": b"x"}), "niet in orde"),
 ("bestandsnaam met pad", package("0.2.1", files={**BASE, "sub/evil.py": b"x"}), "niet in orde"),
 ("main.py in een update", package("0.2.1", files={**BASE, "main.py": b"x"}), "niet in orde"),
 ("app_shell.py in een update", package("0.2.1", files={**BASE, "app_shell.py": b"x"}), "niet in orde"),
 ("verplicht bestand ontbreekt", package("0.2.1", files={k: v for k, v in BASE.items() if k != "serve.py"}), "niet in orde"),
 ("manifest zegt andere versie", package("0.2.1", manifest_version="9.9.9"), "niet in orde"),
 ("geen zip", b"dit is geen zip", "niet in orde"),
]
for name, data, word in cases:
    got, err, code = run("0.2.1", data)
    check(name, got is None and err and word in err.nl and not (code/"current.json").exists() and not (code/"0.2.1").exists(), err and err.nl)

got, err, code = run("0.2.0", package("0.2.0"))
check("niet nieuwer dan de draaiende versie", got is None and err and "niet nieuwer" in err.nl)
got, err, code = run("0.2.1", package("0.2.1"), expected="0.2.2")
check("versie wijkt af van de release", got is None and err)

# meerdere versies en opruimen, en bad.json
code = Path(tempfile.mkdtemp()) / "code"
for v in ("0.2.1", "0.2.2", "0.2.3"):
    d = package(v); store = {"z": d, "s": sign(d)}
    if v == "0.2.2": (code).mkdir(parents=True, exist_ok=True); (code/"bad.json").write_text(json.dumps(["0.2.2", "0.2.9"]))
    updater.install({"version": v, "zip_url": "z", "sig_url": "s"}, "0.2.0", 1, PUB, code, get=lambda u, l: store[u])
check("opruimen: huidige en vorige blijven", sorted(p.name for p in code.iterdir() if p.is_dir()) == ["0.2.2", "0.2.3"], sorted(p.name for p in code.iterdir()))
check("bad.json: opnieuw geïnstalleerde versie eraf, andere blijft", json.loads((code/"bad.json").read_text()) == ["0.2.9"], (code/"bad.json").read_text())

# samen met de boot
spec = importlib.util.spec_from_file_location("boot", "app/main.py"); boot = importlib.util.module_from_spec(spec); spec.loader.exec_module(boot)
here = Path(tempfile.mkdtemp()); (here/"manifest.json").write_text(json.dumps({"version": "0.2.0"}))
chosen = boot.choose_code(here, code, 1, app_shell.version_tuple)
check("boot kiest de net geïnstalleerde code", chosen and chosen[1] == "0.2.3" and chosen[0] == code/"0.2.3", chosen)

# find_update
R = "ColorcodedNL/local-transcribe-for-iconik"; P = f"https://github.com/{R}/releases/download/v0.3.0/"
def rel(tag="v0.3.0", html=f"https://github.com/{R}/releases/tag/v0.3.0", assets=None):
    return {"tag_name": tag, "html_url": html, "assets": [{"name": n, "browser_download_url": u} for n, u in (assets if assets is not None else [
        ("code-0.3.0-shell1.zip", P + "code-0.3.0-shell1.zip"), ("code-0.3.0-shell1.zip.sig", P + "code-0.3.0-shell1.zip.sig"), ("x.dmg", P + "x.dmg")])]}
f = lambda r, running="0.2.0", shell=1, bad=(): updater.find_update(r, R, running, shell, bad)
check("find: nieuwere release met code-zip", (f(rel()) or {}).get("mode") == "code" and f(rel())["zip_url"].endswith(".zip"))
check("find: zelfde versie", f(rel(), running="0.3.0") is None)
check("find: oudere versie", f(rel(), running="0.4.0") is None)
check("find: zip vraagt nieuwere shell: dmg", (f(rel(), shell=0) or {}).get("mode") == "dmg")
check("find: handtekening ontbreekt: dmg", (f(rel(assets=[("code-0.3.0-shell1.zip", P + "code-0.3.0-shell1.zip")])) or {}).get("mode") == "dmg")
check("find: zip van een ander adres: dmg", (f(rel(assets=[("code-0.3.0-shell1.zip", "https://evil.example/z"), ("code-0.3.0-shell1.zip.sig", "https://evil.example/s")])) or {}).get("mode") == "dmg")
check("find: zip van andere versie: dmg", (f(rel(assets=[("code-0.2.5-shell1.zip", P + "a"), ("code-0.2.5-shell1.zip.sig", P + "b")])) or {}).get("mode") == "dmg")
check("find: versie op de lijst met mislukte: dmg", (f(rel(), bad={"0.3.0"}) or {}).get("mode") == "dmg")
check("find: release van andere repo", f(rel(html="https://github.com/other/repo/releases/tag/v0.3.0")) is None)
check("find: tag is geen versie", f(rel(tag="nightly")) is None)
check("find: geen assets", (f(rel(assets=[])) or {}).get("mode") == "dmg")

# fetch met maximum
class H(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.send_header("Content-Length", "2048"); self.end_headers(); self.wfile.write(b"a" * 2048)
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/x"
try: updater.fetch(url, 4096); ok = False
except updater.UpdateError: ok = True
check("fetch: http naar localhost standaard geweigerd", ok)
updater.INSECURE_HOSTS.add("127.0.0.1")
check("fetch: binnen het maximum", len(updater.fetch(url, 4096)) == 2048)
try: updater.fetch(url, 1024); ok = False
except updater.UpdateError: ok = True
check("fetch: groter dan het maximum", ok)
try: updater.fetch("http://evil.example/x", 1024); ok = False
except updater.UpdateError: ok = True
check("fetch: gewone http naar andere host geweigerd", ok)
for spoof in ("http://127.0.0.1.evil.example/x", "https://x@127.0.0.1.evil.example/x"):
    try: updater.fetch(spoof.replace("https", "http"), 1024); ok = False
    except updater.UpdateError: ok = True
    check("fetch: naam die met 127.0.0.1 begint geweigerd", ok)
print("ALLES OK" if not fails else f"{fails} FOUT")
raise SystemExit(1 if fails else 0)
