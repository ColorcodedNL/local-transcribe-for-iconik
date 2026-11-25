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

# --- eigen transcripten herkennen (merkteken) ---
check("kind: mlx-whisper is eigen werk", iconik.kind(OWN) == "own")
check("kind: local-transcribe (volgende engine) is eigen werk", iconik.kind({"engine_info": {"name": "local-transcribe"}}) == "own")
check("kind: correctie uit Refine", iconik.kind({"engine_info": {"name": transcribe.REFINED_ENGINE}}) == "refined")
check("kind: andere bron of zonder naam", iconik.kind(OTHER) == "foreign" and iconik.kind({}) == "foreign")
NEXT = {"engine_info": {"name": iconik.PRODUCER}}
client = FakeIconik([dict(NEXT, id="NIEUWER"), dict(OTHER, id="X")])
connector = iconik.IconikConnector(client)
info = connector.open("A", push=False)
check("open: transcript van een volgende engine telt als eigen", [t["id"] for t in info["own"]] == ["NIEUWER"]
      and [t["id"] for t in info["foreign"]] == ["X"])
tid, _ = connector.write(info, doc_with(1), {"name": transcribe.ENGINE_NAME})
check("write: vervangt ook het transcript van een volgende engine, nooit dat uit een andere bron",
      sorted(t["id"] for t in client.transcriptions) == sorted([tid, "X"]))
check("read: vindt het eigen transcript ongeacht de engine-naam", connector.read(info) is not None)

# --- via de vaste vorm (connectors.call): alleen gewone gegevens, per exemplaar (stap 5c) ---
import json  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
import connectors, settings, iconik_queue  # noqa: E402,E401
transcribe.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"   # nooit de echte voorkeuren aanraken
SOURCE = settings.add_integration("iconik")
connectors.install(iconik)
clients = {SOURCE: FakeIconik([dict(OTHER, id="X")])}
iconik.client = lambda instance: clients.get(instance)
transcribe.find_media_file = lambda client, asset_id, mount=None, ask=False, interactive=True: Path(f"/opslag/{asset_id}.mxf")
check("vorm: de Iconik-connector heeft de vaste vorm", connectors.load("iconik") is iconik
      and connectors.can(SOURCE, "queue") and connectors.can(SOURCE, "read_back"))
check("vorm: status zegt of er verbinding is", connectors.call(SOURCE, "status")["connected"] is True)
info = connectors.call(SOURCE, "open", key="A", push=False)
check("vorm: open geeft gewone gegevens met het exemplaar", info["instance"] == SOURCE and info["title"] == "Interview"
      and [t["id"] for t in info["foreign"]] == ["X"] and connectors.roundtrip(info) == info)
check("vorm: find_media geeft paden als tekst", connectors.call(SOURCE, "find_media", info=info) == ["/opslag/A.mxf"])
try:
    connectors.call(SOURCE, "open", key="A", push=True)
    check("vorm: open weigert nog steeds een andere bron (Problem, overslaan)", False)
except transcribe.Problem as problem:
    check("vorm: open weigert nog steeds een andere bron (Problem, overslaan)", problem.skip is True)
clients[SOURCE] = FakeIconik()
info = connectors.call(SOURCE, "open", key="A", push=True)
doc = json.loads(json.dumps(model.to_dict(doc_with(3))))   # zoals het over een pijp zou komen
written = connectors.call(SOURCE, "write", info=info, doc=doc, engine_info={"name": transcribe.ENGINE_NAME, "model": "m"})
check("vorm: write met een transcript als dict", written == {"id": "T1", "segments": 3}, written)
back = connectors.call(SOURCE, "read", info=info)
check("vorm: read geeft een dict terug die weer een transcript wordt", isinstance(back, dict)
      and len(model.from_dict(back).segments) == 3 and connectors.call(SOURCE, "read", info=info, which="refined") is None)
iconik_queue.lost.add("A")
try:
    connectors.call(SOURCE, "write", info=info, doc=doc, engine_info={"name": transcribe.ENGINE_NAME}, from_queue=True)
    check("vorm: opdracht uit de rij die een andere Mac overnam: niets geschreven", False)
except transcribe.Problem:
    check("vorm: opdracht uit de rij die een andere Mac overnam: niets geschreven",
          [t["id"] for t in clients[SOURCE].transcriptions] == ["T1"])
iconik_queue.lost.clear()
written = connectors.call(SOURCE, "write", info=info, doc=doc, engine_info={"name": transcribe.REFINED_ENGINE}, replace="all")
check("vorm: replace=all vervangt ook de correctie", [t["id"] for t in clients[SOURCE].transcriptions] == [written["id"]])
clients[SOURCE] = None
check("vorm: niet ingelogd", connectors.call(SOURCE, "status")["connected"] is False)
try:
    connectors.call(SOURCE, "open", key="A")
    check("vorm: niet ingelogd: open geeft een nette fout", False)
except transcribe.Problem as problem:
    check("vorm: niet ingelogd: open geeft een nette fout", "ingelogd" in str(problem) or "logged in" in str(problem), str(problem))

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
