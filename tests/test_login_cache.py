"""De inlog in het geheugen (transcribe.load_login): de sleutelhanger één keer lezen, bijwerken bij verversen,
en opnieuw lezen als iets anders het token intussen ververste.

Draaien: uv run --no-project --with pynacl --with requests python -B tests/test_login_cache.py
"""
import json
import sys
import time
import types

sys.path.insert(0, "app")
store, reads = {}, []
_kr = types.ModuleType("keyring")
_kr.get_password = lambda service, user: reads.append((service, user)) or store.get((service, user))
_kr.set_password = lambda service, user, value: store.__setitem__((service, user), value)
_kr.delete_password = lambda service, user: store.pop((service, user), None)
sys.modules["keyring"] = _kr
for name in ("numpy", "mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
import transcribe  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


key = (transcribe.KEYCHAIN_SERVICE, "login")
fresh = {"access_token": "A1", "refresh_token": "R1", "expires_at": time.time() + 3600}
store[key] = json.dumps(fresh)

first = transcribe.load_login()
transcribe.load_login(); transcribe.load_login()
check("één keer lezen voor drie keer vragen", first["access_token"] == "A1" and reads.count(key) == 1, reads)
first["access_token"] = "geknoeid"
check("een kopie: knoeien verandert het geheugen niet", transcribe.load_login()["access_token"] == "A1")

transcribe.save_login(dict(fresh, access_token="A2"))
check("bewaren werkt het geheugen bij zonder te lezen", transcribe.load_login()["access_token"] == "A2" and reads.count(key) == 1)
check("en staat in de sleutelhanger", json.loads(store[key])["access_token"] == "A2")

# Het token verloopt; verversen mislukt omdat een ander (het script op de opdrachtregel) al ververste.
transcribe.save_login({"access_token": "OUD", "refresh_token": "R-OUD", "expires_at": time.time() - 10})
store[key] = json.dumps({"access_token": "NIEUW", "refresh_token": "R-NIEUW", "expires_at": time.time() + 3600})


def refused(fields):
    raise transcribe.Problem("invalid_grant")


transcribe.token_request = refused
client = transcribe.Iconik()
client._authorize()
check("ververst door een ander: nieuwere inlog uit de sleutelhanger", client.session.headers["Authorization"] == "Bearer NIEUW")
check("en die staat nu ook in het geheugen", transcribe.load_login()["access_token"] == "NIEUW")

# Geen nieuwere inlog: dan echt uitloggen.
transcribe.save_login({"access_token": "OUD", "refresh_token": "R-OUD", "expires_at": time.time() - 10})
try:
    transcribe.Iconik()._authorize()
    check("verlopen zonder nieuwere inlog: uitgelogd", False)
except transcribe.Problem:
    check("verlopen zonder nieuwere inlog: uitgelogd", transcribe.load_login() is None and key not in store)

transcribe.token_request = lambda fields: transcribe.save_login({"access_token": "VERS", "refresh_token": "R2", "expires_at": time.time() + 3600}) or transcribe.load_login()
transcribe.save_login({"access_token": "OUD", "refresh_token": "R-OUD", "expires_at": time.time() - 10})
c2 = transcribe.Iconik(); c2._authorize()
check("gewoon verversen werkt nog", c2.session.headers["Authorization"] == "Bearer VERS" and transcribe.load_login()["access_token"] == "VERS")

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
