"""De vaste vorm van een connector (stap 5b): capabilities, gewone gegevens erin en eruit, instellingen en
geheimen per exemplaar via de gastheer. Met een nep-connector en een nep-sleutelhanger.

Draaien: uv run --no-project --with pynacl --with requests --with numpy python -B tests/test_connector_form.py
"""
import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, "app")
# Nooit de echte sleutelhanger: een nep-keyring die bijhoudt wat er gelezen en geschreven wordt.
vault, reads = {}, []
kr = types.ModuleType("keyring")


def _get(service, account):
    reads.append(account)
    return vault.get((service, account))


def _delete(service, account):
    if (service, account) not in vault:
        raise type("PasswordDeleteError", (Exception,), {})("niet gevonden")
    del vault[(service, account)]


kr.get_password = _get
kr.set_password = lambda service, account, value: vault.__setitem__((service, account), value)
kr.delete_password = _delete
sys.modules["keyring"] = kr

import base, settings, keychain, connectors  # noqa: E402,E401

base.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"   # nooit de echte voorkeuren aanraken
base.print = lambda *a, **k: None
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("OK   " if cond else "FOUT ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        fails += 1


def raises(what, error=connectors.FormError):
    try:
        what()
    except error:
        return True
    return False


# --- een nep-connector met alle capabilities ---
proef = types.ModuleType("proef_connector")
proef.NAME = "proef"
proef.TITLE = ("Proef", "Test")
proef.CAPABILITIES = ("source", "write_back", "read_back", "queue", "auto_scan")
proef.SETTINGS = [settings.Setting("proef.host", "text", "https://proef.example", ("Adres", "Address")),
                  settings.Setting("proef.auto", "bool", False, ("Automatisch", "Automatic"))]
seen = {}


def _start(host):
    seen["host"] = host


proef.start = _start
proef.status = lambda instance: {"instance": instance, "host": seen["host"].setting(instance, "proef.host")}
proef.open = lambda instance, key, push=True: {"key": key, "title": f"titel {key}", "push": push}
proef.find_media = lambda instance, info, interactive=False: [f"/opslag/{info['key']}.mxf"]
proef.write = lambda instance, info, doc, engine_info: {"id": "t1", "segments": len(doc.get("blocks", []))}
proef.read = lambda instance, info: None
proef.poll = lambda instance, who, auto=None: None
proef.report = lambda instance, job: True
proef.beat = lambda instance, now=None: True
connectors.install(proef)

check("installeren: soort is een koppeling", "proef" in settings.INTEGRATIONS)
check("installeren: instellingen aangemeld", "proef.host" in settings.SCHEMA and "proef.auto" in settings.SCHEMA)
check("installeren: start kreeg een gastheer", isinstance(seen.get("host"), connectors.Host))

a = settings.add_integration("proef")
b = settings.add_integration("proef")
settings.put("proef.host", "https://a.example", instance=a)
check("exemplaren: beide bekend", sorted(connectors.instances()) == sorted([a, b]) and connectors.instances("queue") == sorted([a, b]))
check("aanroepen: per exemplaar de eigen instelling",
      connectors.call(a, "status") == {"instance": a, "host": "https://a.example"}
      and connectors.call(b, "status")["host"] == "https://proef.example")
info = connectors.call(a, "open", key="x1")
check("aanroepen: open en find_media met gewone gegevens",
      info["title"] == "titel x1" and connectors.call(a, "find_media", info=info) == ["/opslag/x1.mxf"])
check("aanroepen: write", connectors.call(a, "write", info=info, doc={"blocks": [1, 2]}, engine_info={"name": "e"})
      == {"id": "t1", "segments": 2})
check("aanroepen: wat over een pijp terugkomt is gelijk", connectors.roundtrip(info) == info)
check("can: capability per exemplaar", connectors.can(a, "queue") and not connectors.can("proef-000000", "queue"))

# --- gewone gegevens, streng ---
check("vorm: een object als parameter geweigerd", raises(lambda: connectors.call(a, "open", key=object())))
check("vorm: een set geweigerd", raises(lambda: connectors.plain({"x": {1, 2}})))
check("vorm: een sleutel die geen tekst is geweigerd", raises(lambda: connectors.plain({1: "a"})))
check("vorm: NaN geweigerd", raises(lambda: connectors.plain([float("nan")])))
check("vorm: bytes geweigerd", raises(lambda: connectors.plain(b"x")))
check("vorm: gewone gegevens mogen", connectors.plain({"a": [1, 2.5, None, True, "x", {"b": []}]}) is not None)
proef.read = lambda instance, info: {"open": open}   # geeft iets wat geen gewoon gegeven is
check("vorm: een uitkomst die geen gewoon gegeven is geweigerd", raises(lambda: connectors.call(a, "read", info=info)))
check("vorm: een functie buiten de vaste vorm geweigerd", raises(lambda: connectors.call(a, "verwijder_alles")))
check("vorm: een onbekend exemplaar geweigerd", raises(lambda: connectors.call("proef-ffffff", "status")))

# --- een connector die zich niet aan de vorm houdt ---
half = types.ModuleType("half")
half.NAME, half.TITLE, half.CAPABILITIES, half.SETTINGS = "half", ("H", "H"), ("source",), []
half.start = half.status = lambda *a, **k: None
check("module: ontbrekende functie (open, find_media) geweigerd", raises(lambda: connectors.install(half)))
half.open = half.find_media = lambda *a, **k: None
half.CAPABILITIES = ("source", "vliegen")
check("module: onbekende capability geweigerd", raises(lambda: connectors.install(half)))
half.CAPABILITIES = ("source", "auto_scan")
check("module: auto_scan zonder wachtrij geweigerd", raises(lambda: connectors.install(half)))
half.CAPABILITIES = ("source",)
half.SETTINGS = [settings.Setting("proef.anders", "bool", False, ("x", "x"))]
check("module: instelling buiten de eigen groep geweigerd", raises(lambda: connectors.install(half)))
half.SETTINGS = []
check("module: in orde is goed", connectors.install(half) is half and "half" not in [i["type"] for i in settings.integrations()])

# --- de gastheer: instellingen ---
host = connectors.Host()
check("gastheer: instelling van de app", host.setting(a, "transcription.language") == "auto")
host.put_setting(b, "proef.auto", True)
check("gastheer: bewaart per exemplaar", settings.get("proef.auto", b) is True and settings.get("proef.auto", a) is False)
check("gastheer: geen instellingen van de app bewaren", raises(lambda: host.put_setting(a, "transcription.language", "nl")))
check("gastheer: het schema controleert", raises(lambda: host.put_setting(a, "proef.auto", "ja"), settings.SettingError))
host.note(a, "system_domain_id", "abcdef12")
check("gastheer: noteren per exemplaar", host.noted(a, "system_domain_id") == "abcdef12" and host.noted(b, "system_domain_id") is None)

# --- de gastheer: geheimen per exemplaar ---
host.put_secret(a, "login", json.dumps({"token": "A"}))
host.put_secret(b, "login", json.dumps({"token": "B"}))
check("geheimen: elk exemplaar een eigen item",
      vault.get((base.KEYCHAIN_SERVICE, f"{a}:login")) == '{"token": "A"}'
      and vault.get((base.KEYCHAIN_SERVICE, f"{b}:login")) == '{"token": "B"}')
check("geheimen: geen geheim in config.json", "token" not in base.CONFIG_PATH.read_text())
keychain.forget()
reads.clear()
check("geheimen: lezen per exemplaar", json.loads(host.secret(a, "login"))["token"] == "A"
      and json.loads(host.secret(b, "login"))["token"] == "B")
host.secret(a, "login")
host.secret(a, "login")
check("geheimen: één keer uit de sleutelhanger, daarna uit het geheugen", reads.count(f"{a}:login") == 1, reads)
check("geheimen: fresh leest opnieuw", keychain.get(a, "login", fresh=True) and reads.count(f"{a}:login") == 2)
check("geheimen: weghalen raakt alleen dat exemplaar", host.delete_secret(a, "login") is True
      and host.secret(a, "login") is None and json.loads(host.secret(b, "login"))["token"] == "B"
      and (base.KEYCHAIN_SERVICE, f"{a}:login") not in vault)
check("geheimen: weghalen wat er niet is, is goed", host.delete_secret(a, "login") is True)
check("geheimen: een rare naam geweigerd", raises(lambda: host.secret(a, "../login"), ValueError)
      and raises(lambda: host.secret("iconik", "login"), ValueError))
check("geheimen: alleen tekst", raises(lambda: host.put_secret(a, "login", {"token": 1}), ValueError))

# --- code-update: de nieuwe bestanden gaan mee ---
import re  # noqa: E402
code_files = re.search(r"^CODE_FILES = (\[.*?\])", Path("tools/make_manifest.py").read_text(), re.M).group(1)
check("code-update: keychain.py en connectors.py in CODE_FILES", '"keychain.py"' in code_files and '"connectors.py"' in code_files)

# --- start_all geeft ook een al geladen connector de gastheer van de app ---
class AppHost(connectors.Host):
    def may_take(self):
        return False


app_host = AppHost()
connectors.start_all(app_host)
check("start_all: al geladen connector krijgt de gastheer van de app", seen["host"] is app_host and connectors.host is app_host)

# --- de Iconik-soort staat in de lijst ---
check("soorten: iconik bekend", connectors.KINDS.get("iconik") == "iconik")

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
