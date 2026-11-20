"""Items (stap 4): een eigen identiteit per item per bron, de map per bron en de eenmalige migratie van de losse
bestanden van vóór stap 4.

Draaien: uv run --no-project --with pynacl --with requests --with numpy python -B tests/test_items.py
"""
import hashlib
import json
import sys
import tempfile
import types
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

import transcribe, serve, refine, items, settings  # noqa: E402,E401

transcribe.L = lambda nl, en: nl
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


def fresh():
    """Een lege supportmap met eigen voorkeuren; geeft de map."""
    tmp = Path(tempfile.mkdtemp())
    transcribe.CONFIG_PATH = tmp / "config.json"
    transcribe.TRANSCRIPTS_DIR = tmp / "transcripts"
    transcribe.WAVES_DIR = tmp / "waves"
    transcribe.SIDECARS_FILE = tmp / "sidecars.json"
    refine.DRAFTS_DIR = tmp / "refine"
    serve.jobs_path = lambda: tmp / "jobs.json"
    return tmp


def roots():
    return {"transcripts": transcribe.TRANSCRIPTS_DIR, "waves": transcribe.WAVES_DIR, "refine": refine.DRAFTS_DIR}


ASSET = "0edde392-a79f-11f0-bbd0-aa870b39fb9a"

# --- 1. het formaat
check("een item uit Iconik", items.valid(f"iconik-10ce9b:{ASSET}") and items.split(f"iconik-10ce9b:{ASSET}") == ("iconik-10ce9b", ASSET))
check("een lokaal item", items.valid("local:clip-1") and items.asset_id("local:clip-1") == "" and not items.is_iconik("local:clip-1"))
check("asset_id en sleutel", items.asset_id(f"iconik-10ce9b:{ASSET}") == ASSET and items.key("local:clip-1") == "clip-1")
for bad in ("", ASSET, "iconik:abc", "iconik-10CE9B:abc", "iconik-10ce9:abc", "nextcloud-a1b2c3:x", "local:", "local:a/b",
            "local:../x", f"iconik-10ce9b:{ASSET}:x", "local:a b", None, 12, "local:" + "x" * 65):
    check(f"geen item: {bad!r}", not items.valid(bad))
try:
    items.make("local", "a/b")
    check("make weigert een rare sleutel", False)
except ValueError:
    check("make weigert een rare sleutel", True)

# --- 2. de enige Iconik
tmp = fresh()
check("zonder Iconik: from_request met een kaal asset geeft niets en maakt niets aan",
      items.from_request(asset=ASSET) is None and settings.integrations() == [])
item = items.iconik(ASSET)
source = items.iconik_source()
check("iconik() maakt het exemplaar aan als het er nog niet is", source and item == f"{source}:{ASSET}"
      and settings.integrations() == [{"id": source, "type": "iconik"}], settings.integrations())
check("een tweede keer hetzelfde exemplaar", items.iconik("abc") == f"{source}:abc" and len(settings.integrations()) == 1)
check("met een eigen bron", items.iconik("abc", source="iconik-aaaaaa") == "iconik-aaaaaa:abc")
check("een asset-ID dat er geen kan zijn: None", items.iconik("../x") is None and items.iconik("") is None)
check("from_request: een item", items.from_request(item=f"{source}:abc") == f"{source}:abc")
check("from_request: een ongeldig item is niets, ook met een asset erbij", items.from_request(item="rommel", asset="abc") is None)
check("from_request: een kaal asset (oude link) wordt de enige Iconik", items.from_request(asset="abc") == f"{source}:abc")
check("bestaande voorkeuren blijven staan", json.loads(transcribe.CONFIG_PATH.read_text())["version"] == 2)

# --- 3. paden per bron
p = items.path(transcribe.TRANSCRIPTS_DIR, item, ".json")
check("pad: <map>/<bron>/<asset><achtervoegsel>", p == transcribe.TRANSCRIPTS_DIR / source / f"{ASSET}.json", p)
check("pad terug naar het item", items.from_path(transcribe.TRANSCRIPTS_DIR, p, ".json") == item)
check("lokaal en Iconik met dezelfde sleutel botsen niet",
      items.path(refine.DRAFTS_DIR, "local:abc", ".draft.json") != items.path(refine.DRAFTS_DIR, f"{source}:abc", ".draft.json"))
for f in (items.path(refine.DRAFTS_DIR, f"{source}:a", ".original.json"), items.path(refine.DRAFTS_DIR, "local:b", ".original.json")):
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{}")
(refine.DRAFTS_DIR / "losse.original.json").write_text("{}")
(refine.DRAFTS_DIR / "rommel").mkdir()
(refine.DRAFTS_DIR / "rommel" / "x.original.json").write_text("{}")
check("files: alleen wat in een map per bron staat", [i for i, _ in items.files(refine.DRAFTS_DIR, ".original.json")]
      == [f"{source}:a", "local:b"], items.files(refine.DRAFTS_DIR, ".original.json"))

# --- 4. de migratie, op een map met dezelfde opbouw als een echte van 0.6
tmp = fresh()
transcribe.save_config({"version": 2, "general": {}, "transcription": {}, "refine": {},
                        "integrations": {"iconik-10ce9b": {"type": "iconik", "client_id": "x"}}})
assets = [f"{i:08x}-a79f-11f0-bbd0-aa870b39fb9a" for i in range(49)]
layout = [("transcripts", a, ".json") for a in assets] + [("waves", a, ".waves") for a in assets[:19]] \
    + [("waves", a, ".media.json") for a in assets[:7]] + [("refine", a, ".draft.json") for a in assets[:2]] \
    + [("refine", a, ".original.json") for a in assets[2:5]] + [("refine", a, ".subtitles.json") for a in assets[:2]]
before = {}
for kind, a, suffix in layout:
    f = roots()[kind] / f"{a}{suffix}"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"asset": a, "suffix": suffix}))
    before[(kind, a, suffix)] = hashlib.sha256(f.read_bytes()).hexdigest()
odd = [transcribe.TRANSCRIPTS_DIR / f"{assets[0]}.json.tmp", transcribe.WAVES_DIR / "notitie.txt",
       refine.DRAFTS_DIR / "raar naam.draft.json"]
for f in odd:
    f.write_text("laten staan")
logged = []
moved, kept = items.migrate(roots(), log=logged.append)
check("migratie: alle 82 bestanden verplaatst", (moved, kept) == (82, 0), (moved, kept))
check("migratie: elk bestand staat in de map van de Iconik, met dezelfde inhoud",
      all(hashlib.sha256(items.path(roots()[k], f"iconik-10ce9b:{a}", s).read_bytes()).hexdigest() == h
          for (k, a, s), h in before.items()))
check("migratie: niets meer los in de mappen", not items.legacy_files(roots()))
check("migratie: .tmp, een notitie en een naam die geen ID kan zijn blijven staan", all(f.exists() for f in odd))
check("migratie: geen tweede Iconik aangemaakt", [i["id"] for i in settings.integrations()] == ["iconik-10ce9b"])
check("migratie: één regel in het log, zonder namen of ID's", len(logged) == 1 and "82 verplaatst" in logged[0]
      and not any(a in logged[0] for a in assets), logged)
check("migratie: een tweede keer doet niets", items.migrate(roots(), log=logged.append) == (0, 0) and len(logged) == 1)

# Refine vindt alles terug op de nieuwe plek.
first = f"iconik-10ce9b:{assets[0]}"
check("na de migratie: concept en ondertitels in de labels van de lijst", refine.marks(first)["draft"] and refine.marks(first)["subtitles"])
check("na de migratie: de drie originelen in Instellingen, met hun item",
      sorted(o["item"] for o in refine.originals_list()) == sorted(f"iconik-10ce9b:{a}" for a in assets[2:5]))

# Al een bestand op de nieuwe plek (bijvoorbeeld een halve eerdere poging): beide blijven staan.
clash_old = transcribe.TRANSCRIPTS_DIR / f"{assets[1]}.json"
clash_old.write_text("oud")
moved, kept = items.migrate(roots(), log=logged.append)
check("botsing: niets overschreven, beide blijven", (moved, kept) == (0, 1) and clash_old.read_text() == "oud"
      and items.path(transcribe.TRANSCRIPTS_DIR, f"iconik-10ce9b:{assets[1]}", ".json").read_text() != "oud")

# Geen Iconik in de voorkeuren (de koppeling was weggehaald) maar wel oude bestanden: die horen bij een Iconik.
tmp = fresh()
transcribe.TRANSCRIPTS_DIR.mkdir(parents=True)
(transcribe.TRANSCRIPTS_DIR / "abc.json").write_text("{}")
items.migrate(roots(), log=logged.append)
made = items.iconik_source()
check("zonder Iconik: er komt er een, en de bestanden gaan daarheen", made and (transcribe.TRANSCRIPTS_DIR / made / "abc.json").is_file())

# Een lege of nieuwe supportmap: niets te doen, geen Iconik aangemaakt.
tmp = fresh()
check("lege map: niets te doen en geen Iconik", items.migrate(roots(), log=logged.append) == (0, 0) and settings.integrations() == [])

# --- 5. bij de start: eerst de bestanden, dan jobs.json van 0.6, en ze horen bij elkaar
tmp = fresh()
transcribe.save_config({"version": 2, "integrations": {"iconik-10ce9b": {"type": "iconik"}}})
transcribe.TRANSCRIPTS_DIR.mkdir(parents=True)
(transcribe.TRANSCRIPTS_DIR / f"{ASSET}.json").write_text("{}")
serve.jobs_path().write_text(json.dumps({"saved": "2026-11-16T12:00:00", "cleared": 0, "jobs": [
    {"n": 1, "asset_id": ASSET, "title": "Clip", "status": "klaar", "added": "2026-11-16T10:00:00"}]}))
serve.jobs.clear()
serve.migrate_items()
serve.load_jobs()
job = serve.jobs[0]
check("start: de opdracht en zijn kopie horen bij hetzelfde item", job.item == f"iconik-10ce9b:{ASSET}"
      and items.path(transcribe.TRANSCRIPTS_DIR, job.item, ".json").is_file(), job.item)
serve.save_jobs()
check("start: jobs.json daarna versie 2", json.loads(serve.jobs_path().read_text())["version"] == 2)
check("start: een migratie die faalt houdt de start niet tegen",
      (lambda: (setattr(items, "migrate", lambda *a, **k: 1 / 0), serve.migrate_items(), True)[-1])())

print("\nALLES OK" if not fails else f"\n{fails} FOUT")
sys.exit(1 if fails else 0)
