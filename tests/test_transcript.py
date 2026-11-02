"""Het eigen transcriptmodel (app/transcript.py): naar Iconik precies wat de app altijd stuurde, terug uit
Iconik zonder verlies, en het bestandsformaat heen en terug.

Draaien: uv run --no-project --with pynacl --with requests python -B tests/test_transcript.py
"""
import json
import sys
import types

sys.path.insert(0, "app")
# Nooit de echte sleutelhanger: een nep-keyring zonder inlog (anders vraagt macOS om het wachtwoord).
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

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


def w(text, start, end, p=0.9):
    return {"word": text, "start": start, "end": end, "probability": p}


# Whisper-uitvoer met de lastige gevallen: een woord zonder spatie ervoor (hoort bij het vorige), een leeg
# woord, een eindtijd voor de begintijd, en een woord dat iets voor het segment begint.
pieces = [
    (0, [w(" Dus", 181.2, 181.5), w(" de", 181.5, 181.62), w(" woke", 181.7, 182.0, 0.8), w("-predikant", 182.0, 182.6, 0.4)]),
    (1, [w(" ", 183.0, 183.1), w(" En", 183.1, 183.3), w(" toen?", 183.3, 183.2, 0.55)]),
    (0, [w(" Precies.", 184.0, 184.9123, 0.987654)]),
    (1, [w(" ", 186.0, 186.2)]),   # alleen een spatie: in Iconik een lege regel die wegvalt
]
version_id = "versie-1"
old = [transcribe.to_iconik_segment(words, "T", version_id, n) for n, words in pieces]
built = model.from_pieces(pieces, "nl", "Spreker", engine={"name": "Local Transcribe"})
properties, new = model.to_iconik(built, "T", version_id)
check("naar Iconik: segmenten precies als tot nu toe", new == old,
      json.dumps([a for a, b in zip(new, old) if a != b], ensure_ascii=False)[:400])
check("naar Iconik: sprekerlabels als tot nu toe", properties["speaker_labels"] == {"0": "Spreker 1", "1": "Spreker 2"}
      and properties["language"] == "nl", properties)
check("woord zonder spatie aan het vorige vast", built.segments[0].words[-1].text == "woke-predikant"
      and built.segments[0].words[-1].score == 0.4, built.segments[0].words)
check("lege regel blijft leeg (valt bij het schrijven weg)", new[3]["segment_text"] == "" and not built.segments[3].words)

# Terug uit Iconik, zoals Iconik het teruggeeft (met eigen velden erbij).
from_server = [dict(s, id=f"seg-{i}", date_created="2026-11-02T10:00:00Z") for i, s in enumerate(new) if s["segment_text"]]
props_server = dict(properties, engine_info={"name": "Local Transcribe", "model": "m"}, id="T")
back = model.from_iconik(props_server, list(reversed(from_server)), source={"connector": "iconik", "asset_id": "a"})
again_props, again = model.to_iconik(back, "T", version_id)
check("terug uit Iconik en weer erheen: dezelfde segmenten", again == [s for s in new if s["segment_text"]],
      json.dumps(again, ensure_ascii=False)[:300])
check("terug uit Iconik: sprekers, taal en engine", [s.name for s in back.speakers] == ["Spreker 1", "Spreker 2"]
      and back.language == "nl" and back.engine.get("model") == "m" and back.source["asset_id"] == "a")
check("terug uit Iconik: op tijd gesorteerd", [s.start for s in back.segments] == sorted(s.start for s in back.segments))

# Iemand paste in Iconik de tekst aan maar niet de woorden: die tekst gaat voor.
edited = [dict(from_server[0], segment_text="Dus de woke predikant")]
fixed = model.from_iconik(properties, edited)
check("aangepaste tekst in Iconik blijft bewaard", fixed.segments[0].display_text() == "Dus de woke predikant"
      and model.to_iconik(fixed, "T", version_id)[1][0]["segment_text"] == "Dus de woke predikant")

# Een naam die iemand gaf, en een spreker die in de labels ontbreekt.
named = model.from_iconik({"language": "en", "speaker_labels": {"0": "Jan"}},
                          [{"time_start_milliseconds": 0, "time_end_milliseconds": 900, "segment_text": "Hi",
                            "transcription": {"speaker": 2, "words": [{"value": "Hi", "start_ms": 0, "end_ms": 400, "score": 1}]}}])
check("sprekernaam en ontbrekend label", [(s.id, s.name) for s in named.speakers] == [("1", "Jan"), ("3", "Speaker 3")]
      and named.segments[0].speaker == "3", [(s.id, s.name) for s in named.speakers])

# Sporen uit de analyse en het bestandsformaat heen en terug.
built.tracks = model.tracks_from_infos([{"n": 1, "note": "zender", "used": True, "db": -16.2, "env": [0.1]},
                                        {"n": 2, "note": "tijdcode", "used": False, "db": -9.0}])
built.source = {"connector": "iconik", "asset_id": "a1", "version_id": version_id, "title": "Clip"}
text = json.dumps(model.to_dict(built), ensure_ascii=False)
restored = model.from_dict(json.loads(text))
check("bestand heen en terug: alles gelijk", restored == built)
check("bestand: formaat en versie erin", json.loads(text)["format"] == "local-transcribe" and json.loads(text)["version"] == 1)
try:
    model.from_dict({"format": "local-transcribe", "version": 99})
    check("nieuwere versie van het formaat: nette fout", False)
except ValueError:
    check("nieuwere versie van het formaat: nette fout", True)
try:
    model.from_dict({"segments": []})
    check("ander bestand: nette fout", False)
except ValueError:
    check("ander bestand: nette fout", True)

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
