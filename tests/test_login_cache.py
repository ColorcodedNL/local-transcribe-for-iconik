"""De inlog bij Iconik in de sleutelhanger, per exemplaar (stap 5d): één keer lezen en daarna uit het geheugen,
bijwerken bij verversen, opnieuw lezen als iets anders het token intussen ververste; de inlog van vóór stap 5 (account
"login", of de oude app-naam) verhuist eenmalig naar het enige exemplaar; twee Iconiks raken elkaars inlog niet.

Draaien: uv run --no-project --with pynacl --with requests python -B tests/test_login_cache.py
"""
import json
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, "app")
store, reads, deletes = {}, [], []
refuse_delete = [False]   # macOS weigert het weghalen (bijvoorbeeld na "Sta toe" in plaats van "Sta altijd toe")
_kr = types.ModuleType("keyring")


def _delete(service, user):
    deletes.append((service, user))
    if refuse_delete[0]:
        raise RuntimeError("geweigerd")
    if (service, user) not in store:
        raise type("PasswordDeleteError", (Exception,), {})("niet gevonden")
    del store[(service, user)]


_kr.get_password = lambda service, user: reads.append((service, user)) or store.get((service, user))
_kr.set_password = lambda service, user, value: store.__setitem__((service, user), value)
_kr.delete_password = _delete
sys.modules["keyring"] = _kr
for name in ("numpy", "mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
import transcribe  # noqa: E402
import keychain  # noqa: E402
import settings  # noqa: E402

transcribe.print = lambda *a, **k: None
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


SERVICE = transcribe.KEYCHAIN_SERVICE
OLD = (SERVICE, "login")                        # het item van vóór stap 5
OLDER = ("Iconik Transcribe", "login")          # de naam van vóór 0.2.0: sinds stap 6g niet meer verhuisd


def fresh_config():
    """Een eigen config.json met één Iconik, zoals na de omzetting van 0.6; de sleutelhanger en het geheugen leeg."""
    transcribe.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"   # nooit de echte voorkeuren aanraken
    transcribe.CONFIG_PATH.write_text(json.dumps({"version": 2, "general": {}, "transcription": {}, "refine": {},
                                                  "integrations": {"iconik-a1b2c3": {"type": "iconik", "client_id": "x"}}}))
    store.clear(); reads.clear(); deletes.clear(); refuse_delete[0] = False
    keychain.forget(); transcribe._has_login.clear()
    return "iconik-a1b2c3"


def login(token, expires=3600, refresh="R"):
    return {"access_token": token, "refresh_token": refresh, "expires_at": time.time() + expires}


# --- de verhuizing van het oude item ---
inst = fresh_config()
new = (SERVICE, f"{inst}:login")
store[OLD] = json.dumps(login("OUD-ITEM"))
first = transcribe.load_login()
check("verhuizen: de oude inlog werkt meteen", first and first["access_token"] == "OUD-ITEM")
check("verhuizen: staat onder de naam van het exemplaar", json.loads(store.get(new, "{}")).get("access_token") == "OUD-ITEM")
check("verhuizen: het oude item is weg", OLD not in store)
check("verhuizen: genoteerd bij het exemplaar, niet in de sleutelhanger", settings.noted(inst, transcribe.LEGACY_NOTE) == "moved"
      and "OUD-ITEM" not in transcribe.CONFIG_PATH.read_text())
check("verhuizen: het oude item één keer gelezen", reads.count(OLD) == 1, reads)
reads.clear()
transcribe.load_login(); transcribe.load_login(inst)
check("daarna uit het geheugen: geen enkele keer de sleutelhanger", reads == [], reads)

inst = fresh_config()
store[OLDER] = json.dumps(login("NOG-OUDER"))
check("de app-naam van vóór 0.2.0 wordt niet meer verhuisd en niet aangeraakt (stap 6g)", transcribe.load_login() is None
      and OLDER in store and OLDER not in reads)
del store[OLDER]

inst = fresh_config()
check("niets om te verhuizen: geen inlog", transcribe.load_login() is None and transcribe.has_login() is False)
check("  en dat is genoteerd, zodat het niet elke keer opnieuw wordt gezocht", settings.noted(inst, transcribe.LEGACY_NOTE) == "moved")
reads.clear()
keychain.forget()
transcribe.load_login()
check("  een volgende start kijkt niet meer naar het oude item", OLD not in reads and OLDER not in reads, reads)

# macOS weigert het weghalen van het oude item: de inlog is wel verhuisd; uitloggen probeert het opnieuw.
inst = fresh_config()
store[OLD] = json.dumps(login("OUD-ITEM"))
refuse_delete[0] = True
check("weghalen geweigerd: de inlog werkt toch", transcribe.load_login()["access_token"] == "OUD-ITEM" and OLD in store)
refuse_delete[0] = False
transcribe.clear_login()
check("weghalen geweigerd: uitloggen haalt ook het oude item weg", OLD not in store and (SERVICE, f"{inst}:login") not in store)
check("na uitloggen komt de oude inlog niet terug", transcribe.load_login() is None)
refuse_delete[0] = True
store[OLD] = json.dumps(login("OUD-ITEM"))   # stel dat hij er toch nog stond
keychain.forget()
check("ook niet als het oude item er nog zou staan", transcribe.load_login() is None)
refuse_delete[0] = False

# --- het geheugen, per exemplaar ---
inst = fresh_config()
key = (SERVICE, f"{inst}:login")
store[key] = json.dumps(login("A1", refresh="R1"))
first = transcribe.load_login()
transcribe.load_login(); transcribe.load_login(inst)
check("één keer lezen voor drie keer vragen", first["access_token"] == "A1" and reads.count(key) == 1, reads)
first["access_token"] = "geknoeid"
check("een kopie: knoeien verandert het geheugen niet", transcribe.load_login()["access_token"] == "A1")

transcribe.save_login(login("A2"))
check("bewaren werkt het geheugen bij zonder te lezen", transcribe.load_login()["access_token"] == "A2" and reads.count(key) == 1)
check("en staat in de sleutelhanger", json.loads(store[key])["access_token"] == "A2")

# Het token verloopt; verversen mislukt omdat een ander (het script op de opdrachtregel) al ververste.
transcribe.save_login(login("OUD", expires=-10, refresh="R-OUD"))
store[key] = json.dumps(login("NIEUW", refresh="R-NIEUW"))


def refused(fields, instance=None):
    raise transcribe.Problem("invalid_grant")


real_token_request = transcribe.token_request
transcribe.token_request = refused
client = transcribe.Iconik()
client._authorize()
check("ververst door een ander: nieuwere inlog uit de sleutelhanger", client.session.headers["Authorization"] == "Bearer NIEUW")
check("en die staat nu ook in het geheugen", transcribe.load_login()["access_token"] == "NIEUW")

# Geen nieuwere inlog: dan echt uitloggen.
transcribe.save_login(login("OUD", expires=-10, refresh="R-OUD"))
try:
    transcribe.Iconik()._authorize()
    check("verlopen zonder nieuwere inlog: uitgelogd", False)
except transcribe.Problem:
    check("verlopen zonder nieuwere inlog: uitgelogd", transcribe.load_login() is None and key not in store)

transcribe.token_request = lambda fields, instance=None: transcribe.save_login(login("VERS", refresh="R2"), instance) or transcribe.load_login(instance)
transcribe.save_login(login("OUD", expires=-10, refresh="R-OUD"))
c2 = transcribe.Iconik(); c2._authorize()
check("gewoon verversen werkt nog", c2.session.headers["Authorization"] == "Bearer VERS" and transcribe.load_login()["access_token"] == "VERS")
transcribe.token_request = real_token_request

# Twee draden tegelijk bij het opstarten (venster en achtergrond), terwijl macOS op het wachtwoord wacht.
keychain.forget()
store[key] = json.dumps(login("A1"))
reads.clear()
slow = _kr.get_password
_kr.get_password = lambda service, user: (time.sleep(0.3), slow(service, user))[1]
threads = [threading.Thread(target=transcribe.load_login) for _ in range(3)]
[t.start() for t in threads]; [t.join() for t in threads]
_kr.get_password = slow
check("drie draden tegelijk: één keer de sleutelhanger", reads.count(key) == 1, reads)

# --- twee Iconiks: elk een eigen inlog, adres en opslag ---
a = fresh_config()
b = settings.add_integration("iconik")
settings.put_many({"iconik.host": "https://b.example", "iconik.client_id": "22222222-2222-2222-2222-222222222222"}, instance=b)
store[OLD] = json.dumps(login("OUD-ITEM"))
check("twee Iconiks: het oude item wordt niet geraden", transcribe.load_login(a) is None and transcribe.load_login(b) is None
      and OLD in store)
transcribe.save_login(login("VAN-A"), a)
transcribe.save_login(login("VAN-B"), b)
check("twee Iconiks: elk een eigen item", json.loads(store[(SERVICE, f"{a}:login")])["access_token"] == "VAN-A"
      and json.loads(store[(SERVICE, f"{b}:login")])["access_token"] == "VAN-B")
check("twee Iconiks: elk een eigen adres", transcribe.api_host(a) == "https://app.iconik.io" and transcribe.api_host(b) == "https://b.example")
ca, cb = transcribe.make_client(a), transcribe.make_client(b)
check("twee Iconiks: elke verbinding hoort bij haar exemplaar", ca.instance == a and cb.instance == b)
cb._authorize()
check("twee Iconiks: de verbinding van B gebruikt de inlog van B", cb.session.headers["Authorization"] == "Bearer VAN-B")
address, state, verifier = transcribe.start_login(b)
check("twee Iconiks: inloggen bij B gaat naar het adres en de client-ID van B", address.startswith("https://b.example/oauth/authorize?")
      and "client_id=22222222-2222-2222-2222-222222222222" in address, address)
transcribe.clear_login(b)
check("twee Iconiks: uitloggen bij B laat A ingelogd", transcribe.has_login(a) and not transcribe.has_login(b)
      and (SERVICE, f"{a}:login") in store and (SERVICE, f"{b}:login") not in store)
transcribe.set_mount("s1", "/opslag-b", b)
check("twee Iconiks: de opslag hoort bij het exemplaar", settings.get("iconik.mounts", b) == {"s1": "/opslag-b"}
      and settings.get("iconik.mounts", a) == {})
try:
    transcribe.load_login()
    check("twee Iconiks: zonder te zeggen welke is een fout", False)
except settings.InstanceError:
    check("twee Iconiks: zonder te zeggen welke is een fout", True)

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
