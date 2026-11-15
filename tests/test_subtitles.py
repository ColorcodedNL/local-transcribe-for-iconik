"""Ondertitels uit het transcriptmodel (app/subtitles.py): verdelen volgens de regels, inpassen (samenvoegen
en uitloop), regelbreuken, waarschuwingen in twee niveaus, de laag, en de bestanden .srt en .vtt.

Draaien: uv run --no-project python -B tests/test_subtitles.py
"""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "app")
import subtitles  # noqa: E402
import transcript as model  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


def segment(speaker, start, text, per_word=300, gap=0):
    """Een blok met woorden van elk per_word ms, direct achter elkaar (of met gap ms ertussen)."""
    words, at = [], start
    for part in text.split():
        words.append(model.Word(part, at, at + per_word, 0.9))
        at += per_word + gap
    return model.Segment(start, words[-1].end if words else start, speaker, words)


def doc(*segments):
    return model.Transcript(language="nl", speakers=[model.Speaker("1", "Spreker 1"), model.Speaker("2", "Anna <gast>")],
                            segments=list(segments))


def texts_of(cues):
    return [" ".join(c["lines"]) for c in cues]


LONG = ("Dus toen we daar aankwamen, bleek dat de hele ploeg al vertrokken was naar het andere stadion. "
        "Niemand had ons iets verteld! En dat was eigenlijk het begin van een heel lang en vreemd seizoen, "
        "waarin bijna alles anders liep dan we vooraf hadden bedacht.")
STREAM = subtitles.clean_rules()
PLAIN = {"linger": 0, "merge": 0}      # alleen verdelen, zonder inpassen

# --- regels ---
check("voorkeuze is streaming, 42 tekens en twee regels", STREAM["max_chars"] == 42 and STREAM["max_lines"] == 2)
check("inpassen staat standaard aan: uitloop tot 2 s, samenvoegen tot een pauze van 0,5 s",
      STREAM["linger"] == 1 and STREAM["linger_ms"] == 2000 and STREAM["merge"] == 1 and STREAM["merge_gap_ms"] == 500)
check("voorkeuze herkend, ook met ander inpassen",
      subtitles.preset_of(STREAM) == "streaming" and subtitles.preset_of(subtitles.clean_rules(dict(subtitles.PRESETS["broadcast"], linger=0))) == "broadcast")
own = subtitles.clean_rules({"max_chars": "30", "max_lines": 9, "min_ms": "x", "gap_ms": -5, "cps": 12.4, "linger": 7, "merge_gap_ms": 99999, "vreemd": 1})
check("eigen waarden: getallen overgenomen en binnen de grenzen",
      own["max_chars"] == 30 and own["max_lines"] == 3 and own["min_ms"] == 1000 and own["gap_ms"] == 0 and own["cps"] == 12
      and own["linger"] == 1 and own["merge_gap_ms"] == 2000, str(own))
check("eigen waarden heten custom", subtitles.preset_of(own) == "custom")
check("langste duur blijft boven de kortste", subtitles.clean_rules({"min_ms": 3000, "max_ms": 2000})["max_ms"] >= 3500)

# --- verdelen ---
cues = subtitles.cues(doc(segment("1", 10000, LONG)), PLAIN)
texts = texts_of(cues)
check("geen woord kwijt of dubbel", " ".join(texts) == LONG, " | ".join(texts))
check("nooit meer dan twee regels van 42 tekens",
      all(len(c["lines"]) <= 2 and all(len(line) <= 42 for line in c["lines"]) for c in cues), str([c["lines"] for c in cues]))
check("nooit langer dan 7 seconden", all(c["end"] - c["start"] <= 7000 for c in cues), str([(c["start"], c["end"]) for c in cues]))
check("elke zin eindigt een ondertitel", any(t.endswith("stadion.") for t in texts) and any(t.endswith("verteld!") for t in texts), " | ".join(texts))
ends = [t.split()[-1] for t in texts[:-1]]
check("ondertitels eindigen op een leesteken waar dat kan", sum(bool(re.search(r"[.,!?]$", e)) for e in ends) >= len(ends) - 1, str(ends))
check("tijden lopen op, met ruimte ertussen", all(a["end"] + 80 <= b["start"] for a, b in zip(cues, cues[1:])), str([(c["start"], c["end"]) for c in cues]))
check("begin is het begin van het eerste woord", cues[0]["start"] == 10000)
narrow = subtitles.cues(doc(segment("1", 0, LONG)), dict(PLAIN, max_chars=37))
check("37 tekens: smallere regels", all(len(line) <= 37 for c in narrow for line in c["lines"]) and len(narrow) >= len(cues))
single = subtitles.cues(doc(segment("1", 0, LONG)), dict(PLAIN, max_lines=1))
check("een regel: elke ondertitel een regel", all(len(c["lines"]) == 1 for c in single) and len(single) > len(cues))

# --- regelbreuk ---
lines = subtitles.layout("Dit is een zin die net niet op een regel past, dus breken.".split(), STREAM)
check("twee regels, ongeveer even lang", len(lines) == 2 and abs(len(lines[0]) - len(lines[1])) <= 16, str(lines))
lines = subtitles.layout("Toen kwam hij binnen met de grote koffer van zijn vader".split(), STREAM)
check("regel eindigt niet op een lidwoord of voorzetsel", lines[0].split()[-1] not in ("de", "van", "met"), str(lines))
check("korte tekst blijft op een regel", subtitles.layout("Ja, precies.".split(), STREAM) == ["Ja, precies."])
long_word = subtitles.cues(doc(segment("1", 0, "Kijk hottentottententententoonstellingsterreinbeheerder daar")), PLAIN)
check("een woord langer dan een regel valt niet weg", "hottentottententententoonstellingsterreinbeheerder" in " ".join(texts_of(long_word)))

# --- inpassen: korte zinnen samen ---
talk = doc(segment("1", 0, "Ja, zeker."), segment("1", 900, "Je weet dat het heel hectisch wordt."))
check("samenvoegen: een korte zin bij de volgende van dezelfde spreker, zonder pauze ertussen",
      texts_of(subtitles.cues(talk)) == ["Ja, zeker. Je weet dat het heel hectisch wordt."], texts_of(subtitles.cues(talk)))
check("samenvoegen uit: elke zin apart", len(subtitles.cues(talk, {"merge": 0})) == 2)
apart = doc(segment("1", 0, "Ja, zeker."), segment("1", 2000, "Je weet dat het heel hectisch wordt."))
check("samenvoegen: niet als er een pauze tussen zit", len(subtitles.cues(apart)) == 2, texts_of(subtitles.cues(apart)))
check("samenvoegen: wel met een ruimere pauze ingesteld", len(subtitles.cues(apart, {"merge_gap_ms": 2000})) == 1)
other = doc(segment("1", 0, "Ja, zeker."), segment("2", 900, "Je weet dat het heel hectisch wordt."))
check("samenvoegen: nooit twee sprekers in een ondertitel", [c["speaker"] for c in subtitles.cues(other)] == ["1", "2"])
both_long = doc(segment("1", 0, "Dat was voor ons een verschrikkelijke dag."), segment("1", 2500, "En dat wordt het morgen weer waarschijnlijk."))
check("samenvoegen: twee lange zinnen blijven apart", len(subtitles.cues(both_long, {"merge_gap_ms": 2000})) == 2)
chain = doc(segment("1", 0, "Ja. Dat klopt. En daarna zijn we met de bus teruggegaan."))
check("samenvoegen: korte zinnen in een blok komen bij de zin erna", texts_of(subtitles.cues(chain)) == ["Ja. Dat klopt. En daarna zijn we met de bus teruggegaan."],
      texts_of(subtitles.cues(chain)))

# --- inpassen: uitloop ---
quick = doc(segment("1", 0, "Moet je wat vinden van zo'n parcours?", per_word=200), segment("2", 6000, "Ja."))
tight = subtitles.cues(quick, PLAIN)
loose = subtitles.cues(quick, {"merge": 0})
speed = lambda c: sum(len(line) for line in c["lines"]) * 1000 / (c["end"] - c["start"])   # noqa: E731
check("zonder uitloop stopt de ondertitel op het laatste woord en is hij te snel", tight[0]["end"] == 1400 and speed(tight[0]) > 17, tight[0]["end"])
check("uitloop: blijft staan tot hij te lezen is", loose[0]["end"] > 1400 and speed(loose[0]) <= 17.01, (loose[0]["end"], speed(loose[0])))
check("uitloop: niet langer dan nodig", loose[0]["end"] < 3400, loose[0]["end"])
capped = subtitles.cues(quick, {"merge": 0, "linger_ms": 500})
check("uitloop: hooguit het ingestelde maximum na het laatste woord", capped[0]["end"] == 1900, capped[0]["end"])
crowded = doc(segment("1", 0, "Moet je wat vinden van zo'n parcours?", per_word=200), segment("2", 1700, "Ja."))
check("uitloop: nooit over de volgende heen", subtitles.cues(crowded, {"merge": 0})[0]["end"] == 1700 - 80)
calm = doc(segment("1", 0, "Dit gaat rustig genoeg.", per_word=600), segment("2", 9000, "Ja."))
check("uitloop: een ondertitel die te lezen is blijft zoals hij is", subtitles.cues(calm, {"merge": 0})[0]["end"] == 2400)

# --- tijden ---
short = subtitles.cues(doc(segment("1", 1000, "Ja."), segment("2", 5000, "Nee.")), PLAIN)
check("korte ondertitel gerekt tot een seconde", short[0]["end"] - short[0]["start"] == 1000 and short[1]["end"] - short[1]["start"] == 1000,
      str([(c["start"], c["end"]) for c in short]))
tight = subtitles.cues(doc(segment("1", 1000, "Ja."), segment("2", 1500, "Nee.")), PLAIN)
check("rekken stopt voor de volgende", tight[0]["end"] == 1500 - 80, str([(c["start"], c["end"]) for c in tight]))
overlap = subtitles.cues(doc(segment("1", 0, "Dat denk ik eigenlijk ook wel"), segment("2", 1200, "Ja ja")), PLAIN)
check("door elkaar praten: de eerste stopt voor de tweede", overlap[0]["end"] == 1200 - 80 and overlap[1]["speaker"] == "2",
      str([(c["start"], c["end"]) for c in overlap]))
paused = subtitles.cues(doc(segment("1", 0, "Even wachten", gap=2000)), PLAIN)
check("een lange stilte in een blok begint een nieuwe ondertitel", len(paused) == 2)
no_words = model.Segment(2000, 6000, "1", [], "Een blok zonder woorden uit een andere bron")
plain = subtitles.cues(doc(no_words), PLAIN)
check("blok zonder woorden: tekst over de duur verdeeld", plain and plain[0]["start"] == 2000 and plain[-1]["end"] <= 6000
      and " ".join(texts_of(plain)) == no_words.text)
check("leeg transcript geeft niets", subtitles.cues(doc()) == [] and subtitles.to_srt([]) == "")

# --- waarschuwingen in twee niveaus ---
def cue(text, start, end):
    return {"start": start, "end": end, "lines": subtitles.layout(text.split(), STREAM), "speaker": "1"}


check("goed: geen waarschuwing", subtitles.warnings(cue("Dit gaat rustig genoeg.", 0, 3000), None, STREAM) == ([], 0))
check("sneller dan de leessnelheid: licht (oranje)", subtitles.warnings(cue("Geestink is uit de Tour.", 0, 1300), None, STREAM) == (["fast"], 1))
check("anderhalf keer de leessnelheid: zwaar (rood)", subtitles.warnings(cue("Moet je wat vinden van zo'n parcours?", 0, 1400), None, STREAM) == (["unreadable"], 2))
check("te kort en te lang: licht", subtitles.warnings(cue("Ja.", 0, 500), None, STREAM)[1] == 1 and "long" in subtitles.warnings(cue("Ja.", 0, 9000), None, STREAM)[0])
wide = {"start": 0, "end": 5000, "lines": ["Kijk", "hottentottententententoonstellingsterreinbeheerder", "daar"], "speaker": "1"}
check("past niet binnen de regels: zwaar", subtitles.warnings(wide, None, STREAM) == (["wide"], 2))
check("loopt over de volgende heen: zwaar", subtitles.warnings(cue("Dit gaat rustig genoeg.", 0, 3000), cue("Ja.", 2500, 4000), STREAM) == (["overlap"], 2))
fast_doc = doc(segment("1", 0, LONG, per_word=120))
counted = subtitles.report(subtitles.cues(fast_doc, PLAIN))
check("telling: goed, licht en zwaar tellen op tot het totaal", counted["good"] + counted["fast"] + counted["bad"] == counted["cues"] and counted["bad"] > 0, counted)
check("snelle spraak: de tekst wordt nooit ingekort", " ".join(texts_of(subtitles.cues(fast_doc))) == LONG)
seen = subtitles.preview(quick)
check("voorvertoning: de telling, en hoeveel het er zonder uitloop zouden zijn",
      seen["cues"] == 2 and seen["fast"] + seen["bad"] < seen["without_linger"], seen)

# --- de ondertitels als eigen laag ---
source = doc(segment("1", 0, LONG), segment("2", 40000, "Ja ja"))
lay = subtitles.layer(source)
derived = subtitles.cues(source)
check("laag: elk blok een ondertitel, met de woorden en de spreker",
      [(s.start, s.end, s.speaker, s.display_text()) for s in lay.segments] == [(c["start"], c["end"], c["speaker"], " ".join(c["lines"])) for c in derived]
      and all(s.words for s in lay.segments))
check("laag: woordtijden binnen de ondertitel", all(s.start <= w.start <= w.end <= s.end for s in lay.segments for w in s.words))
check("laag: sprekers en taal van het transcript", lay.language == "nl" and [s.name for s in lay.speakers] == ["Spreker 1", "Anna <gast>"])
check("laag: heen en terug door het bestandsformaat", model.to_dict(model.from_dict(model.to_dict(lay))) == model.to_dict(lay))
lay.segments[0].words = lay.segments[0].words[:2]            # met de hand ingekort
lay.segments[1].end = lay.segments[1].start + 400            # en een rand versleept
edited = subtitles.cues_of_layer(lay)
check("laag: een bewerkte ondertitel gaat eruit zoals hij staat, zonder rekken of opnieuw verdelen",
      edited[0]["lines"] == ["Dus toen"] and edited[1]["end"] - edited[1]["start"] == 400 and len(edited) == len(derived))

# --- een laag opnieuw inpassen ---
base = subtitles.layer(quick, PLAIN)                       # zonder uitloop gemaakt
fitted, touched = subtitles.fit_layer(base, [False, False], {"merge": 0})
check("inpassen: uitloop alsnog toegepast", fitted.segments[0].end > base.segments[0].end and touched == [False, False])
again, _ = subtitles.fit_layer(fitted, [False, False], PLAIN)
check("inpassen: uitloop uit haalt hem er weer af", again.segments[0].end == base.segments[0].end, again.segments[0].end)
hand = subtitles.layer(quick, PLAIN)
hand.segments[0].end = 1500
kept, touched = subtitles.fit_layer(hand, [True, False], {"merge": 0})
check("inpassen: wat met de hand is aangepast blijft precies zo", kept.segments[0].end == 1500 and touched == [True, False])
pair = subtitles.layer(talk, PLAIN)
joined, touched = subtitles.fit_layer(pair, [False, False])
check("inpassen: voegt korte zinnen alsnog samen", len(pair.segments) == 2 and len(joined.segments) == 1 and touched == [False])
stay, touched = subtitles.fit_layer(pair, [True, False])
check("inpassen: voegt niets samen met een ondertitel die met de hand is aangepast", len(stay.segments) == 2 and touched == [True, False])

# --- bestanden ---
d = subtitles.layer(doc(segment("1", 3661500, "Tom & Jerry <live>"), segment("2", 3665000, "Zeker weten.")), PLAIN)
srt, counted = subtitles.render(d, "srt")
check("srt: nummer, tijden met komma, tekst, lege regel",
      srt == "1\n01:01:01,500 --> 01:01:02,700\nTom & Jerry <live>\n\n2\n01:01:05,000 --> 01:01:06,000\nZeker weten.\n", repr(srt))
check("srt: geen sprekernaam", "Spreker" not in srt and "Anna" not in srt)
check("telling komt mee", counted["cues"] == 2)
vtt, _ = subtitles.render(d, "vtt")
check("vtt: kop, tijden met punt, spreker als <v>, tekens ontweken",
      vtt == "WEBVTT\n\n01:01:01.500 --> 01:01:02.700\n<v Spreker 1>Tom &amp; Jerry &lt;live&gt;\n\n"
             "01:01:05.000 --> 01:01:06.000\n<v Anna  gast>Zeker weten.\n", repr(vtt))
try:
    subtitles.render(d, "docx")
    check("onbekend formaat geweigerd", False)
except ValueError:
    check("onbekend formaat geweigerd", True)

# --- een bestaand bestand inlezen ---
SRT = ("\ufeff1\r\n00:00:01,000 --> 00:00:02,500\r\n<i>Dus toen we daar</i>\r\naankwamen,\r\n\r\n"
       "2\r\n00:00:03,000 --> 00:00:04,200\r\n{\\an8}Niemand had ons iets verteld!\r\n\r\n\r\nrommel zonder tijden\r\n\r\n"
       "3\r\n00:01:05,5 --> 00:01:06,000\r\n\r\n")
read = subtitles.parse(SRT.encode("utf-8"))
check("inlezen .srt: tijden, regels samen, opmaak en nummers weg, rommel en lege ondertitels overgeslagen",
      read == [{"start": 1000, "end": 2500, "text": "Dus toen we daar aankwamen,", "voice": ""},
               {"start": 3000, "end": 4200, "text": "Niemand had ons iets verteld!", "voice": ""}], read)
VTT = ("WEBVTT\n\nNOTE dit is een opmerking\n\nSTYLE\n::cue { color: red }\n\nintro\n00:01.000 --> 00:02.500 line:10% align:start\n"
       "<v Anna &lt;gast&gt;>Tom &amp; Jerry\n\n01:00:00.000 --> 01:00:01.000\n<c.geel>Zeker</c> weten.\n")
read = subtitles.parse(VTT)
check("inlezen .vtt: zonder uren, met plaatsing, NOTE en STYLE overgeslagen, spreker uit <v>, tekens terug",
      read == [{"start": 1000, "end": 2500, "text": "Tom & Jerry", "voice": "Anna <gast>"},
               {"start": 3600000, "end": 3601000, "text": "Zeker weten.", "voice": ""}], read)
check("inlezen: andere tekencodering en onzin geven geen fout", subtitles.parse("1\n00:00:01,000 --> 00:00:02,000\ncafé\n".encode("cp1252"))[0]["text"] == "café"
      and subtitles.parse(b"\x00\xff geen ondertitels") == [] and subtitles.parse("") == [])
check("heen en terug: wat de app schrijft leest hij precies zo weer in",
      [(c["start"], c["end"], c["text"]) for c in subtitles.parse(srt)] == [(3661500, 3662700, "Tom & Jerry"), (3665000, 3666000, "Zeker weten.")]
      and [c["voice"] for c in subtitles.parse(vtt)] == ["Spreker 1", "Anna  gast"], subtitles.parse(srt))

spoken = doc(segment("1", 1000, "Dus toen we daar aankwamen, bleek dat de hele ploeg al vertrokken was."), segment("2", 9000, "Echt waar?"))
loaded = subtitles.import_layer([{"start": 1000, "end": 2600, "text": "Dus toen we daar aankwamen,", "voice": ""},
                                 {"start": 2700, "end": 5000, "text": "bleek de ploeg al weg te zijn.", "voice": ""},
                                 {"start": 9000, "end": 10000, "text": "Echt waar?", "voice": ""},
                                 {"start": 20000, "end": 21000, "text": "Iets heel anders.", "voice": "anna <GAST>"}], spoken)
check("inladen: elke ondertitel een blok, met zijn eigen tijden en tekst",
      [(s.start, s.end, s.display_text()) for s in loaded.segments] == [(1000, 2600, "Dus toen we daar aankwamen,"), (2700, 5000, "bleek de ploeg al weg te zijn."),
                                                                         (9000, 10000, "Echt waar?"), (20000, 21000, "Iets heel anders.")])
check("inladen: de spreker van het blok dat het meest overlapt, of de naam uit <v>", [s.speaker for s in loaded.segments] == ["1", "1", "2", "2"],
      [s.speaker for s in loaded.segments])
check("inladen: woordtiming uit het transcript waar de tekst overeenkomt",
      [(w.text, w.start, w.end) for w in loaded.segments[0].words] == [("Dus", 1000, 1300), ("toen", 1300, 1600), ("we", 1600, 1900), ("daar", 1900, 2200), ("aankwamen,", 2200, 2500)],
      [(w.text, w.start, w.end) for w in loaded.segments[0].words])
changed_words = loaded.segments[1].words
check("inladen: aangepaste tekst houdt de tijd van de woorden die kloppen, de rest ertussen verdeeld",
      [w.text for w in changed_words] == "bleek de ploeg al weg te zijn.".split() and changed_words[0].start == 2700 and changed_words[0].score == 0.9
      and changed_words[-1].score == 1.0, [(w.text, w.start, w.end, w.score) for w in changed_words])
check("inladen: woorden op volgorde en binnen de ondertitel",
      all(s.start <= w.start <= w.end <= s.end for s in loaded.segments for w in s.words)
      and all(a.start <= b.start for s in loaded.segments for a, b in zip(s.words, s.words[1:])))
check("inladen: zonder tekst in het transcript de woorden gelijkmatig over de duur",
      [(w.start, w.end) for w in loaded.segments[3].words] == [(20000, 20333), (20333, 20666), (20666, 21000)], [(w.start, w.end) for w in loaded.segments[3].words])
check("inladen: de laag gaat er weer uit zoals hij binnenkwam",
      [(c["start"], c["end"], " ".join(c["lines"])) for c in subtitles.cues_of_layer(loaded)] == [(s.start, s.end, s.display_text()) for s in loaded.segments])

# --- bestanden schrijven ---
tmp = Path(tempfile.mkdtemp())
clip, registry = tmp / "opnames" / "clip 01.mxf", tmp / "support" / "sidecars.json"
clip.parent.mkdir()
clip.write_bytes(b"media")
first = subtitles.write_files(d, clip, "both", None, registry)
check("schrijven: .srt en .vtt met de naam van het mediabestand",
      [(r["name"], r["state"], r["cues"]) for r in first] == [("clip 01.srt", "written", 2), ("clip 01.vtt", "written", 2)]
      and (clip.parent / "clip 01.srt").read_text(encoding="utf-8") == srt and (clip.parent / "clip 01.vtt").read_text(encoding="utf-8") == vtt, first)
check("schrijven: geen tijdelijke bestanden achtergelaten", sorted(f.name for f in clip.parent.iterdir()) == ["clip 01.mxf", "clip 01.srt", "clip 01.vtt"])
newer = subtitles.layer(doc(segment("1", 0, "Een nieuwe versie van de ondertitels.")))
again = subtitles.write_files(newer, clip, "srt", None, registry)
check("schrijven: een eigen bestand wordt vervangen door de nieuwe versie",
      again[0]["state"] == "written" and "nieuwe versie" in (clip.parent / "clip 01.srt").read_text(encoding="utf-8"), again)
(clip.parent / "clip 01.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nMet de hand verbeterd\n", encoding="utf-8")
changed = subtitles.write_files(d, clip, "srt", None, registry)
check("schrijven: een bestand dat daarna is gewijzigd blijft staan",
      (changed[0]["state"], changed[0]["detail"]) == ("kept", "exists") and "Met de hand" in (clip.parent / "clip 01.srt").read_text(encoding="utf-8"), changed)
forced = subtitles.write_files(d, clip, "srt", None, registry, confirmed=("srt",))
check("schrijven: wie het pad zelf koos in het bewaarvenster, overschrijft wel", forced[0]["state"] == "written" and (clip.parent / "clip 01.srt").read_text(encoding="utf-8") == srt)
other = tmp / "opnames" / "ander.mov"
other.write_bytes(b"media")
other.with_suffix(".vtt").write_text("WEBVTT\n\nvan iemand anders\n", encoding="utf-8")
foreign = subtitles.write_files(d, other, "both", None, registry, confirmed=("srt",))
check("schrijven: een bestand van iemand anders blijft staan, het andere formaat komt er wel",
      [r["state"] for r in foreign] == ["written", "kept"] and "iemand anders" in other.with_suffix(".vtt").read_text(encoding="utf-8"), foreign)
chosen = subtitles.write_files(d, tmp / "elders" / "naam.final.srt", "both", None, registry)
check("schrijven: een map die niet bestaat geeft een melding, geen fout", [r["state"] for r in chosen] == ["failed", "failed"] and chosen[0]["detail"], chosen)
(tmp / "elders").mkdir()
chosen = subtitles.write_files(d, tmp / "elders" / "naam.final.srt", "both", None, registry)
check("schrijven: een gekozen pad houdt zijn naam, de extensie volgt het formaat",
      [r["name"] for r in chosen] == ["naam.final.srt", "naam.final.vtt"] and all(r["state"] == "written" for r in chosen), chosen)
check("schrijven: een onbekende keuze schrijft niets", subtitles.write_files(d, clip, "docx", None, registry) == [])
empty = subtitles.write_files(subtitles.layer(doc()), tmp / "opnames" / "leeg.mov", "srt", None, registry)
check("schrijven: een lege laag schrijft niets", (empty[0]["state"], empty[0]["detail"]) == ("kept", "empty") and not (tmp / "opnames" / "leeg.srt").exists())

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
