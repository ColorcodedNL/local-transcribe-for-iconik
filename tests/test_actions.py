"""De knoppen in Iconik: Local Transcribe (asset, collectie, selectie) en Refine transcript (alleen een asset),
en de link van Refine transcript die de editor opent.

Draaien: uv run --no-project --with pynacl --with requests --with numpy --with huggingface_hub python -B tests/test_actions.py
"""
import sys
import types

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

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


class FakeIconik:
    def __init__(self, existing):
        self.existing, self.posted = list(existing), []

    def get(self, path):
        return {"objects": self.existing}

    def post(self, path, body):
        self.posted.append((path, body)); self.existing.append(body); return {}


old = [{"context": c, "title": "Local Transcribe", "url": "iconiktranscribe://transcribe"} for c in ("ASSET", "COLLECTION", "BULK")]
fake = FakeIconik(old)
missing = serve.missing_actions(fake)
check("bestaande omgeving: alleen Refine transcript ontbreekt", missing == [("ASSET", "Refine transcript", "iconiktranscribe://refine")], missing)
serve.client = lambda: fake
serve.install_actions()
check("plaatsen: één nieuwe knop, alleen op een asset", fake.posted == [("assets/v1/custom_actions/ASSET/",
      {"context": "ASSET", "title": "Refine transcript", "type": "OPEN", "url": "iconiktranscribe://refine"})], fake.posted)
check("daarna niets meer te doen", serve.missing_actions(fake) == [] and serve.actions["state"] == "aanwezig")
empty = FakeIconik([])
check("lege omgeving: vier knoppen", len(serve.missing_actions(empty)) == 4)

aid = "2df8d8e0-a790-11f0-9edb-4a3452efd6a5"
check("link: editor voor het asset", serve.refine_link(f"iconiktranscribe://refine?asset_ids={aid}") == f"/refine?asset={aid}")
check("link: bij meer assets het eerste", serve.refine_link(f"iconiktranscribe://refine?asset_ids={aid},{aid[::-1]}") == f"/refine?asset={aid}")
check("link: zonder asset niets", serve.refine_link("iconiktranscribe://refine") is None)
check("link: rare waarde niets", serve.refine_link("iconiktranscribe://refine?asset_ids=../../etc") is None)
check("link: transcribe-link is geen Refine", serve.refine_link(f"iconiktranscribe://transcribe?asset_ids={aid}") is None)

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
