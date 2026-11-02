"""De Iconik-connector (app/iconik.py): open weigert wat niet mag, write zet het nieuwe neer voor het oude
weggaat, en read haalt een transcript terug in het eigen model, ook over meerdere pagina's.

Draaien: uv run --no-project --with pynacl --with requests python -B tests/test_connector.py
"""
import sys
import types

sys.path.insert(0, "app")
_kr = types.ModuleType("keyring")
_kr.get_password = lambda *a: None
_kr.set_password = lambda *a: None
_kr.delete_password = lambda *a: None
sys.modules["keyring"] = _kr
for name in ("numpy", "mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
import transcribe  # noqa: E402
import transcript as model  # noqa: E402
import iconik  # noqa: E402

transcribe.print = lambda *a, **k: None
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


class FakeIconik:
    """Genoeg van Iconik voor de connector: transcripten met segmenten, in pagina's."""
    def __init__(self, transcriptions=(), fail_bulk=False, fail_reindex=False):
        self.transcriptions = [dict(t) for t in transcriptions]
        self.segments = []
        self.fail_bulk = fail_bulk
        self.fail_reindex = fail_reindex
        self.reindexed = []
        self.calls = []
        self.next = 1

    def get(self, path):
        self.calls.append(("GET", path))
        if path == "assets/v1/assets/A/":
            return {"id": "A", "title": "Interview",
                    "versions": [{"id": "V", "status": "ACTIVE"}], "versions_count": 1}
        if path.endswith("/transcriptions/properties/"):
            return {"objects": list(self.transcriptions)}
        if "/segments/?" in path:
            query = dict(p.split("=", 1) for p in path.split("?", 1)[1].split("&"))
            rows = [s for s in self.segments if s["transcription_id"] == query["transcription_id"]]
            size, page = int(query["per_page"]), int(query["page"])
            return {"objects": rows[(page - 1) * size: page * size]}
        raise AssertionError(path)

    def post(self, path, body):
        self.calls.append(("POST", path))
        if path.endswith("/transcriptions/properties/"):
            tid = f"T{self.next}"
            self.next += 1
            self.transcriptions.append(dict(body, id=tid))
            return {"id": tid}
        if path.endswith("/segments/bulk/"):
            if self.fail_bulk:
                raise transcribe.Problem("netwerk weg")
            self.segments += body["objects"]
            return {}
        if path == "assets/v1/assets/A/reindex/":
            if self.fail_reindex:
                raise transcribe.Problem("HTTP 403")
            self.reindexed.append(body["realms"])
            return {}
        raise AssertionError(path)


def fake_delete(client, asset_id, version_id, transcription_id):
    client.calls.append(("DELETE", transcription_id))
    client.transcriptions = [t for t in client.transcriptions if t["id"] != transcription_id]
    before = len(client.segments)
    client.segments = [s for s in client.segments if s["transcription_id"] != transcription_id]
    return before - len(client.segments)


transcribe.delete_transcription = fake_delete
transcribe.find_version = lambda asset: "V"
OWN = {"engine_info": {"name": transcribe.ENGINE_NAME}}
OTHER = {"engine_info": {"name": "iets-anders"}}


def doc_with(n):
    pieces = [(i % 2, [{"word": f" woord{i}", "start": i, "end": i + 0.5, "probability": 0.9}]) for i in range(n)]
    return model.from_pieces(pieces, "nl", "Spreker", source={"connector": "iconik", "asset_id": "A"})


# --- open ---
client = FakeIconik([dict(OTHER, id="X")])
try:
    iconik.IconikConnector(client).open("A", push=True)
    check("open: transcript uit andere bron blokkeert pushen", False)
except transcribe.Problem:
    check("open: transcript uit andere bron blokkeert pushen", True)
info = iconik.IconikConnector(client).open("A", push=False)
check("open: proefrun mag wel, met titel en versie", info["title"] == "Interview" and info["version_id"] == "V"
      and len(info["foreign"]) == 1)
client = FakeIconik([dict(OWN, id="OUD")])
try:
    iconik.IconikConnector(client).open("A", push=True, replace=False)
    check("open: eigen transcript zonder replace blokkeert", False)
except transcribe.Problem:
    check("open: eigen transcript zonder replace blokkeert", True)

# --- write ---
client = FakeIconik([dict(OWN, id="OUD")])
client.segments = [{"transcription_id": "OUD", "segment_text": "oud"}]
connector = iconik.IconikConnector(client)
info = connector.open("A", push=True, replace=True)
doc = doc_with(3)
tid, written = connector.write(info, doc, {"name": transcribe.ENGINE_NAME})
check("write: nieuw transcript met alle segmenten", written == 3
      and sum(s["transcription_id"] == tid for s in client.segments) == 3)
check("write: oud eigen transcript daarna weg", [t["id"] for t in client.transcriptions] == [tid])
first_delete = next(i for i, c in enumerate(client.calls) if c[0] == "DELETE")
last_bulk = max(i for i, c in enumerate(client.calls) if c[1].endswith("/segments/bulk/"))
check("write: eerst schrijven, dan weghalen", last_bulk < first_delete)
check("write: na vervangen wordt de zoekindex bijgewerkt", client.reindexed == [["TRANSCRIPTIONS", "SEGMENTS"]]
      and [c for c in client.calls if c[1].endswith("/reindex/")] == client.calls[-1:], client.reindexed)

client = FakeIconik(fail_reindex=True)
connector = iconik.IconikConnector(client)
info = connector.open("A", push=True)
tid, _ = connector.write(info, doc_with(1), {"name": transcribe.ENGINE_NAME})
check("write: zonder iets te vervangen geen herindexering", not any(c[1].endswith("/reindex/") for c in client.calls))
client.transcriptions.append(dict(OWN, id="NOG-EEN"))
tid2, _ = connector.write(info, doc_with(1), {"name": transcribe.ENGINE_NAME})
check("write: mislukte herindexering laat de opdracht niet mislukken",
      [t["id"] for t in client.transcriptions] == [tid2])

client = FakeIconik([dict(OWN, id="OUD")], fail_bulk=True)
connector = iconik.IconikConnector(client)
info = connector.open("A", push=True, replace=True)
try:
    connector.write(info, doc_with(2), {"name": transcribe.ENGINE_NAME})
    check("write: fout halverwege gooit door", False)
except transcribe.Problem:
    check("write: fout halverwege gooit door", True)
check("write: bij een fout blijft het oude staan en gaat het halve weg",
      [t["id"] for t in client.transcriptions] == ["OUD"])

client = FakeIconik()
connector = iconik.IconikConnector(client)
info = connector.open("A", push=True)
try:
    connector.write(info, doc_with(1), {"name": transcribe.ENGINE_NAME}, still_ours=lambda: False)
    check("write: overgenomen door andere Mac schrijft niets", False)
except transcribe.Problem:
    check("write: overgenomen door andere Mac schrijft niets", not client.transcriptions)

# --- read ---
transcribe.PAGE_SIZE = 4   # meerdere pagina's zonder duizenden segmenten
client = FakeIconik()
connector = iconik.IconikConnector(client)
info = connector.open("A", push=True)
check("read: geen eigen transcript geeft None", connector.read(info) is None)
original = doc_with(9)
original.speakers[1].name = "Interviewer"
tid, _ = connector.write(info, original, {"name": transcribe.ENGINE_NAME, "model": "m"})
back = connector.read(info)
check("read: alle segmenten terug over meerdere pagina's", back is not None and len(back.segments) == 9,
      back and len(back.segments))
check("read: tekst, tijden en sprekers gelijk",
      [(s.start, s.end, s.speaker, s.display_text()) for s in back.segments]
      == [(s.start, s.end, s.speaker, s.display_text()) for s in original.segments])
check("read: sprekernamen terug", [sp.name for sp in back.speakers] == ["Spreker 1", "Interviewer"])
check("read: bron en engine", back.source.get("transcription_id") == tid and back.source.get("connector") == "iconik"
      and back.engine.get("model") == "m")
client.segments.append({"transcription_id": "ANDER", "segment_text": "niet van ons",
                        "time_start_milliseconds": 0, "time_end_milliseconds": 1, "transcription": {}})
check("read: segmenten van een ander transcript komen niet mee", len(connector.read(info).segments) == 9)

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
