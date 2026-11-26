"""Lokale bestanden als bron (stap 6a, app/local_files.py): de vingerafdruk als identiteit, waar het transcript komt,
de naamregel bij gelijke basisnamen, nooit iets overschrijven wat niet van de app is, kopieën, alleen neerzetten
zonder opnieuw te transcriberen, Refine op een lokaal bestand, en toevoegen via de ontvanger.

Draaien: uv run --no-project --with pynacl --with requests --with numpy python -B tests/test_local.py
Vraagt ffmpeg en ffprobe op het pad (voor Refine).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

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
import transcribe  # noqa: E402
import transcript as model  # noqa: E402

transcribe.L = lambda nl, en: nl
transcribe.print = lambda *a, **k: None
tmp = Path(tempfile.mkdtemp())
transcribe.CONFIG_PATH = tmp / "support" / "config.json"
transcribe.TRANSCRIPTS_DIR = tmp / "support" / "transcripts"
transcribe.WAVES_DIR = tmp / "support" / "waves"
transcribe.SIDECARS_FILE = tmp / "support" / "sidecars.json"
transcribe.LOCAL_INDEX = tmp / "support" / "local-files.json"

import base  # noqa: E402
import connectors  # noqa: E402
import items  # noqa: E402
import local_files  # noqa: E402
import pipeline  # noqa: E402
import platform_mac  # noqa: E402
import refine  # noqa: E402
import settings  # noqa: E402
import subtitles  # noqa: E402

refine.DRAFTS_DIR = tmp / "support" / "refine"
base.print = local_files.base.print = lambda *a, **k: None
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


def raises(call, kind=base.Problem):
    try:
        call()
    except kind as error:
        return error
    return None


check("de supportmap van de test wordt gebruikt", base.LOCAL_INDEX == tmp / "support" / "local-files.json")

# --- de vingerafdruk ---
media_dir = tmp / "opslag" / "opnames"
media_dir.mkdir(parents=True)
big = media_dir / "clip.mxf"
big.write_bytes(os.urandom(5 * 1024 * 1024))
key = local_files.fingerprint(big)
check("vingerafdruk: 32 tekens hex, een geldige sleutel", len(key) == 32 and items.valid(f"local:{key}"), key)
copy_dir = tmp / "opslag" / "kopie"
copy_dir.mkdir()
copy = copy_dir / "anders genoemd.mxf"
copy.write_bytes(big.read_bytes())
check("vingerafdruk: een kopie onder een andere naam is dezelfde", local_files.fingerprint(copy) == key)
data = bytearray(big.read_bytes())
changed = tmp / "veranderd.mxf"
data[len(data) // 2] ^= 0xFF
changed.write_bytes(bytes(data))
check("vingerafdruk: een verandering in het midden telt", local_files.fingerprint(changed) != key)
data = bytearray(big.read_bytes())
data[1024 * 1024 + 10] ^= 0xFF
changed.write_bytes(bytes(data))
check("vingerafdruk: buiten de drie stukken telt alleen de grootte (bewust)", local_files.fingerprint(changed) == key)
small = media_dir / "kort.wav"
small.write_bytes(os.urandom(1000))
small_key = local_files.fingerprint(small)
check("vingerafdruk: een klein bestand helemaal", small_key != key and len(small_key) == 32)

# --- de connectorvorm: local is ingebouwd, geen koppeling ---
connectors.start_all()
check("connector: local is geladen en houdt zich aan de vorm", "local" in connectors._loaded)
check("connector: local is geen koppeling in de voorkeuren",
      "local" not in settings.INTEGRATIONS and not settings.integrations() and connectors.kind_of("local") == "local")
check("connector: local doet geen wachtrij", not connectors.can("local", "queue") and connectors.can("local", "write_back"))
check("connector: status zegt verbonden", connectors.call("local", "status") == {"connected": True})

# --- toevoegen en terugvinden ---
item, title = local_files.remember(big)
check("onthouden: item local:<vingerafdruk> en de bestandsnaam als titel", item == f"local:{key}" and title == "clip.mxf")
check("onthouden: het pad wacht op een transcript", local_files.known(key)["pending"] == [str(big)])
check("terugvinden: het pad", local_files.media_path(key) == big)
error = raises(lambda: local_files.media_path("0" * 32))
check("terugvinden: onbekend wordt overgeslagen", error is not None and error.skip)

# --- volume en mappenstructuur ---
vols = tmp / "Volumes"
check("volume: /Volumes/Archief/a/b.mxf", platform_mac.volume_place(vols / "Archief" / "a" / "b.mxf", vols) == ("Archief", Path("a/b.mxf")))
check("volume: Archief-1 hoort bij Archief", platform_mac.volume_place(vols / "Archief-1" / "a" / "b.mxf", vols) == ("Archief", Path("a/b.mxf")))
boot, inside = platform_mac.volume_place(Path("/Users/iemand/Movies/c.mov"), vols)
check("volume: buiten /Volumes is de opstartschijf", boot and inside == Path("Users/iemand/Movies/c.mov"), (boot, inside))

# --- waar het transcript komt ---
target, state = local_files.target_for(big, key)
check("plaats: naast de media, clip.transcript.json", target == media_dir / "clip.transcript.json" and state == "none")
settings.put("files.transcript_place", "folder")
error = raises(lambda: local_files.target_for(big, key))
check("plaats: in één map zonder map geeft een melding", error is not None and "Bestanden" in str(error))
collect = tmp / "transcripten"
settings.put("files.transcript_folder", str(collect))
target, _ = local_files.target_for(big, key)
volume, inside = platform_mac.volume_place(big)
check("plaats: in één map, met volume en mappenstructuur", target == collect / volume / inside.parent / "clip.transcript.json", target)
settings.put("files.transcript_place", "next")

# --- de naamregel bij een gelijke basisnaam ---
wav = media_dir / "clip.wav"
wav.write_bytes(os.urandom(4000))
wav_key = local_files.fingerprint(wav)
ours_for_mxf = {"format": "local-transcribe", "version": 1, "source": {"fingerprint": key}, "segments": []}
(media_dir / "clip.transcript.json").write_text(json.dumps(ours_for_mxf))
target, state = local_files.target_for(wav, wav_key)
check("naam: clip.wav naast clip.mxf (die er al een heeft) krijgt clip.wav.transcript.json",
      target.name == "clip.wav.transcript.json" and state == "none", (target.name, state))
check("naam: clip.mxf houdt zijn eigen clip.transcript.json", local_files.target_for(big, key) == (media_dir / "clip.transcript.json", "mine"))
(media_dir / "clip.transcript.json").unlink()
(media_dir / "clip.mxf.transcript.json").write_text(json.dumps(ours_for_mxf))
check("naam: wat de app eerder als clip.mxf.transcript.json schreef, blijft de plek",
      local_files.target_for(big, key)[0].name == "clip.mxf.transcript.json")
(media_dir / "clip.mxf.transcript.json").unlink()

# --- nooit overschrijven wat niet van de app is ---
foreign = media_dir / "clip.transcript.json"
foreign.write_text('{"iets": "van een ander programma"}')
error = raises(lambda: connectors.call("local", "open", key=key))
check("vreemd: overgeslagen, met de reden", error is not None and error.skip and "nooit overschreven" in str(error), error)
check("vreemd: het bestand is onaangeroerd", foreign.read_text() == '{"iets": "van een ander programma"}')
info = connectors.call("local", "open", key=key, push=False)
check("vreemd: zonder schrijven (Refine kijken) mag het wel", info["foreign"] and not info["write_existing"])
foreign.write_text("geen json")
check("vreemd: onleesbaar telt als niet van de app", local_files.classify(foreign, key) == "foreign")
foreign.unlink()

# --- schrijven ---
pieces = [(0, [{"word": " Hallo", "start": 1.0, "end": 1.5, "probability": 0.9}]),
          (1, [{"word": " Ja", "start": 5.0, "end": 5.4, "probability": 0.95}])]
doc = model.from_pieces(pieces, "nl", "Spreker", engine={"name": "mlx-whisper", "model": "m", "version": "1"},
                        source={"connector": "local", "item": item, "asset_id": "", "media": str(big), "title": "clip.mxf"})
local_files.remember(copy)
local_files.remember(big)   # het laatst toegevoegde pad is het eerste waar de app naar kijkt
info = connectors.call("local", "open", key=key)
check("open: geeft media, doel en dat er niets staat", info["media"] == str(big) and info["state"] == "none"
      and not info["write_existing"] and info["own"] == [], info)
result = connectors.call("local", "write", info=info, doc=model.to_dict(doc), engine_info={"name": "mlx-whisper", "version": "1"})
written = json.loads((media_dir / "clip.transcript.json").read_text())
check("schrijven: clip.transcript.json in het eigen formaat", written["format"] == "local-transcribe" and written["version"] == 1)
check("schrijven: alleen de bestandsnaam, geen pad", written["source"] == {"connector": "local", "fingerprint": key,
      "media": "clip.mxf", "title": "clip.mxf"} and str(tmp) not in json.dumps(written), written["source"])
check("schrijven: genoteerd in sidecars.json", subtitles.unchanged(media_dir / "clip.transcript.json", base.SIDECARS_FILE))
check("schrijven: ook naast de kopie die wachtte", (copy_dir / "anders genoemd.transcript.json").is_file() and result["copies"] == 1)
check("schrijven: niets meer dat wacht", local_files.known(key)["pending"] == [])
check("schrijven: geen tijdelijke bestanden achtergebleven", not [p for p in media_dir.iterdir() if p.name.endswith(".tmp")])
local_files.remember(big)
info = connectors.call("local", "open", key=key)
check("opnieuw: het eigen transcript wordt vervangen", info["state"] == "mine" and info["own"] and not info["write_existing"])
read = connectors.call("local", "read", info=info)
check("lezen: het eigen transcript, met het pad en item van deze Mac", read and read["source"]["media"] == str(big)
      and read["source"]["item"] == item)
check("lezen: geen correctie", connectors.call("local", "read", info=info, which="refined") is None)

# --- alleen neerzetten wat deze Mac al heeft ---
items.save_transcript(model.from_dict(model.to_dict(doc)), item)
local_files.remember(big)
info = connectors.call("local", "open", key=key)
check("bestaand: opnieuw gesleept terwijl er een naast staat: opnieuw transcriberen", not info["write_existing"])
(copy_dir / "anders genoemd.transcript.json").unlink()
(media_dir / "clip.transcript.json").unlink()
local_files.remember(big)
local_files.remember(copy)
info = connectors.call("local", "open", key=key)
check("bestaand: een kopie zonder transcript, de Mac heeft het: alleen neerzetten", info["write_existing"]
      and info["media"] == str(copy))

args = argparse.Namespace(item=item, asset_id="", push=True, replace=True, model="m", mount=None, track=None)
pipeline.engines = None   # mag niet nodig zijn: niets transcriberen
result = pipeline.process_asset(args, None)
check("bestaand: pijplijn zet het neer zonder engine", result.get("written_only") and result["segments"] == 2, result)
check("bestaand: naast de media en naast de kopie", (media_dir / "clip.transcript.json").is_file()
      and (copy_dir / "anders genoemd.transcript.json").is_file())
pipeline.engines = __import__("engine")

# --- Refine op een lokaal bestand ---
clip = media_dir / "film.mov"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-map", "0:v", "-map", "1:a",
                "-c:v", "mpeg4", "-c:a", "pcm_s16le", str(clip)], check=True)
film, _ = local_files.remember(clip)
film_key = items.key(film)
film_doc = model.from_dict(model.to_dict(doc))
film_doc.source.update(item=film, media=str(clip), title="film.mov")
info = connectors.call("local", "open", key=film_key)
connectors.call("local", "write", info=info, doc=model.to_dict(film_doc), engine_info={"name": "mlx-whisper"})
items.save_transcript(film_doc, film)
view = refine.view(film)
check("Refine: opent een lokaal bestand", view["item"] == film and view["asset_id"] == "" and view["media_name"] == "film.mov", view.get("title"))
edited = json.loads(json.dumps(view["transcript"]))
edited["segments"][0]["text"] = "Hallo daar"
refine.save_draft(film, json.dumps({"transcript": edited, "edits": 1}))
refine.save_to_iconik(film)
saved = json.loads((media_dir / "film.transcript.json").read_text())
check("Refine: de correctie staat naast de media", saved["engine"]["name"] == base.REFINED_ENGINE
      and saved["source"].get("refined") and saved["segments"][0]["text"] == "Hallo daar")
check("Refine: het origineel bewaard op deze Mac", refine.originals_info()["count"] == 1)
error = raises(lambda: connectors.call("local", "open", key=film_key))
check("Refine: opnieuw transcriberen weigert de correctie", error is not None and error.skip and "Refine" in str(error))
refine.restore_original(film)
back = json.loads((media_dir / "film.transcript.json").read_text())
check("Refine: origineel teruggezet", back["engine"]["name"] == "mlx-whisper" and not back["source"].get("refined")
      and "text" not in back["segments"][0])

# --- het bestand verandert of verdwijnt ---
clip.write_bytes(b"iets anders")
error = raises(lambda: connectors.call("local", "open", key=film_key))
check("veranderd: overgeslagen met de reden", error is not None and error.skip and "veranderd" in str(error), error)
clip.unlink()
error = raises(lambda: connectors.call("local", "open", key=film_key))
check("weg: overgeslagen met de reden", error is not None and error.skip and "niet (meer) te vinden" in str(error), error)
check("vergeten: uit local-files.json", local_files.forget(film_key) and not local_files.known(film_key))

# --- kan niet schrijven (stap 6d) ---
import errno  # noqa: E402

check("reden: alleen-lezen", local_files.reason_of(OSError(errno.EROFS, "x")) == "readonly")
check("reden: geen rechten", local_files.reason_of(OSError(errno.EACCES, "x")) == "denied"
      and local_files.reason_of(OSError(errno.EPERM, "x")) == "denied")
check("reden: anders de tekst van het systeem", local_files.reason_of(OSError(errno.ENOSPC, "Geen ruimte")) == "Geen ruimte")
check("schrijftest: een gewone map kan, zonder iets achter te laten", local_files.can_write(media_dir) is None
      and not [p for p in media_dir.iterdir() if p.name.startswith(".lt-schrijftest")])
card = tmp / "Volumes" / "KAART_01" / "DCIM" / "100"
check("bereik: een volume onder /Volumes",
      local_files.scope_of(Path("/Volumes/KAART_01/DCIM/100/clip.mxf")) == "/Volumes/KAART_01")
check("bereik: op de opstartschijf de map zelf, niet de hele schijf", local_files.scope_of(media_dir / "clip.mxf") == str(media_dir))
settings.put("files.watched", {str(tmp / "opslag"): {"subfolders": True}})
check("bereik: een gevolgde map gaat voor", local_files.scope_of(media_dir / "clip.mxf") == str(tmp / "opslag"))
settings.put("files.watched", {})
other_place = tmp / "elders"
settings.put("files.elsewhere", {str(tmp / "opslag"): str(local_files.place_root(tmp / "opslag", other_place))})
check("andere plek: met de naam van het bereik en dezelfde mapjes", local_files.directory_for(media_dir / "clip.mxf")
      == other_place / "opslag" / "opnames", local_files.directory_for(media_dir / "clip.mxf"))
check("andere plek: de map erboven gekozen geeft een eigen map ernaast, niet het bereik zelf",
      local_files.place_root(tmp / "opslag", tmp) == tmp / "opslag transcripten")
check("andere plek: een map in het bereik gekozen ook",
      local_files.place_root(tmp / "opslag", tmp / "opslag" / "x") == tmp / "opslag" / "x" / "opslag transcripten")
settings.put("files.elsewhere", {})
real_can_write = local_files.can_write
local_files.can_write = lambda directory: "readonly"
error = raises(lambda: connectors.call("local", "open", key=key))
check("vooraf: open weigert met place, niet als fout", error is not None and getattr(error, "place", False)
      and "alleen-lezen" in str(error), error)
check("vooraf: kijken mag wel (Refine)", connectors.call("local", "open", key=key, push=False)["media"])
local_files.can_write = real_can_write

# Tijdens het werk: schrijven mislukt; het transcript blijft op deze Mac, zodat een andere plek het alleen nog wegschrijft.
for name in ("clip.transcript.json", "clip.mxf.transcript.json"):
    (media_dir / name).unlink(missing_ok=True)
items.path(base.TRANSCRIPTS_DIR, item, ".json").unlink()
local_files.remember(big)
real_write_one = local_files._write_one


def refuse(target, data):
    raise OSError(errno.EROFS, "Read-only file system")


local_files._write_one = refuse
info = connectors.call("local", "open", key=key)
error = raises(lambda: connectors.call("local", "write", info=info, doc=model.to_dict(doc), engine_info={"name": "mlx-whisper"}))
check("tijdens het werk: schrijven mislukt met place", error is not None and getattr(error, "place", False), error)
args = argparse.Namespace(item=item, asset_id="", push=True, replace=True, model="m", mount=None, track=None, no_speakers=True)
prepared = {"asset": {"title": "clip.mxf"}, "version_id": "", "own": [], "info": info, "media_path": str(big), "tracks": [],
            "peaks": {}, "samples": None, "microphones": None, "model_path": "m"}
saved = []
real_save = pipeline.save_transcript
pipeline.save_transcript = lambda d, i: saved.append(i) or real_save(d, i)
try:
    pipeline.connectors.call(items.source(item), "write", info=info, doc=model.to_dict(doc), engine_info={"name": "x"})
except base.Problem as problem:
    if getattr(problem, "place", False):   # zoals de pijplijn het doet
        pipeline.save_transcript(model.from_dict(model.to_dict(doc)), item)
pipeline.save_transcript = real_save
local_files._write_one = real_write_one
check("tijdens het werk: het transcript staat op deze Mac", items.path(base.TRANSCRIPTS_DIR, item, ".json").is_file() and saved == [item])
check("tijdens het werk: met een plek wordt het alleen weggeschreven", connectors.call("local", "open", key=key)["write_existing"])
check("de pijplijn vangt place af bij schrijven", "getattr(problem, \"place\", False)" in Path("app/pipeline.py").read_text())

# --- toevoegen via de ontvanger ---
import serve  # noqa: E402

serve.jobs_path = lambda: tmp / "support" / "jobs.json"
serve.print = lambda *a, **k: None
before = serve.work.qsize()
other = media_dir / "ander.mov"
other.write_bytes(os.urandom(3000))
note = media_dir / "notities.txt"
note.write_text("x")
result = serve.add_local([str(other), str(note), "relatief.mov", str(tmp / "bestaat-niet.mov"), str(other)])
check("toevoegen: alleen media, absoluut en bestaand, één keer", result == {"added": 1, "skipped": 3}, result)
third = tmp / "derde kopie.mov"
third.write_bytes(other.read_bytes())
result = serve.add_local([str(third)])
check("toevoegen: een kopie van wat al wacht wordt geen tweede opdracht", result == {"added": 0, "skipped": 0}, result)
other_key = local_files.fingerprint(other)
check("toevoegen: de kopie wacht wel op zijn transcript", str(third) in local_files.known(other_key)["pending"])
job = serve.jobs.find(serve.jobs.next_number() - 1)
check("toevoegen: de opdracht heeft het item en de bestandsnaam", job.item == f"local:{other_key}" and job.title == "ander.mov"
      and job.asset_id == "" and serve.work.qsize() == before + 1)

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
