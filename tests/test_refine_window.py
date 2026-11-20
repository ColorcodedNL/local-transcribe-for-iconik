"""Refine in een echte browser, tegen de echte ontvanger: ondertitels bewust maken (regels, inpassen, waar het
bestand komt, de telling als voorvertoning), de twee lagen, waarschuwingen in twee niveaus, bewerken, bewaren,
inpassen, opnieuw maken, op een andere plek bewaren en verwijderen.

Zonder origineel, dus met alleen tekst: de speler (MPEG-4 via Media Source Extensions) is van WebKit en werkt
niet in Chromium. Wat dit dus niet toont: beeld, geluid, bewaren naast de media en het echte bewaarvenster.

Draaien: uv run --no-project --with pynacl --with requests --with numpy --with playwright python -B tests/test_refine_window.py
Eenmalig: uv run --no-project --with playwright playwright install chromium (of LT_CHROMIUM=/pad/naar/chrome).
"""
import json
import os
import sys
import tempfile
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

# Playwright zoekt zijn browser onder HOME. Leg de echte plek vast voordat HOME hieronder verandert.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "ms-playwright"))
TMP = Path(tempfile.mkdtemp(prefix="lt-refine-"))
os.environ["HOME"] = str(TMP)   # voorkeuren, concepten en transcripten in een lege map, nooit in je eigen thuismap
sys.path.insert(0, "app")
kr = types.ModuleType("keyring")
kr.get_password = lambda *a: None
kr.set_password = lambda *a: None
kr.delete_password = lambda *a: None
sys.modules["keyring"] = kr
for name in ("mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
import transcribe, serve, refine, subtitles  # noqa: E402,E401
import transcript as model  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

transcribe.print = lambda *a, **k: None
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


media = TMP / "weg" / "interview.mov"   # bestaat niet: Refine opent met alleen tekst
# (spreker, begin, seconden per woord, tekst): gewone spraak, snelle spraak, een kort antwoord en een woord
# dat op geen enkele regel past.
TEXTS = [
    (0, 1.0, 0.30, "Dus toen we daar aankwamen, bleek dat de hele ploeg al vertrokken was naar het andere stadion. Niemand had ons iets verteld!"),
    (1, 9.0, 0.26, "Echt waar? Dat is toch niet te geloven."),
    (0, 12.5, 0.16, "Nee, en dat was eigenlijk het begin van een heel lang en vreemd seizoen, waarin bijna alles anders liep dan we vooraf hadden bedacht en gehoopt."),
    (1, 18.5, 0.3, "Ja."),
    (0, 20.0, 0.34, "En daarna zijn we met de bus teruggegaan naar het hotel waar iedereen al zat te wachten op het eten dat koud was geworden."),
    (1, 30.0, 0.3, "Hottentottententententoonstellingsterreinbeheerder zei hij toen nog."),
]
pieces = []
for speaker, start, per, text in TEXTS:
    words, at = [], start
    for part in text.split():
        words.append({"word": " " + part, "start": at, "end": at + per * 0.9, "probability": 0.9})
        at += per
    pieces.append((speaker, words))
doc = model.from_pieces(pieces, "nl", "Spreker",
                        tracks=[model.Track(1, "eigen microfoon", True, -12.0), model.Track(2, "eigen microfoon", True, -14.0)],
                        source={"connector": "iconik", "asset_id": "A1", "title": "Interview na de wedstrijd", "media": str(media)})
transcribe.save_config({"app_language": "nl"})
import items  # noqa: E402
A1 = items.iconik("A1")                 # het item van asset A1 in de (enige) Iconik van deze proefvoorkeuren
items.path(transcribe.TRANSCRIPTS_DIR, A1, ".json").parent.mkdir(parents=True, exist_ok=True)
items.path(transcribe.TRANSCRIPTS_DIR, A1, ".json").write_text(json.dumps(model.to_dict(doc)))

app = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
BASE = f"http://localhost:{app.server_address[1]}"
serve.settings.update(key="sleutel", hf_token=None, model="m", language=None, mount=None, idle_exit=False)
out = TMP / "uit"
out.mkdir()
asked, answer = [], {"path": None}
serve.save_hook = lambda folder, name: (asked.append((folder, name)), answer["path"])[1]   # het bewaarvenster
serve.open_hook = lambda folder: answer.get("open")                                           # een bestand kiezen om in te laden
transcribe.SIDECARS_FILE = TMP / "sidecars.json"
threading.Thread(target=app.serve_forever, daemon=True).start()
SELECT_ALL = "Meta+a" if sys.platform == "darwin" else "Control+a"


def python_view(rules):
    """Wat de export van de laag op deze Mac maakt: de regels per ondertitel en het niveau van de waarschuwing."""
    lay = model.from_dict(refine.load_subtitles(A1)["transcript"])
    cues = subtitles.cues_of_layer(lay, rules)
    return [[c["lines"], subtitles.warnings(c, cues[i + 1] if i + 1 < len(cues) else None, subtitles.clean_rules(rules))[1]]
            for i, c in enumerate(cues)]


errors, console = [], []
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.environ.get("LT_CHROMIUM") or None)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("response", lambda r: errors.append(f"{r.status} {r.url}") if r.url.startswith(BASE) and r.status >= 400 else None)
        page.on("pageerror", lambda e: console.append(str(e)))
        ev = page.evaluate
        page.goto(f"{BASE}/window/open?t={serve.SESSION}")
        page.goto(f"{BASE}/refine?item={quote(A1)}")
        page.wait_for_function("D && blocks.length === 6", timeout=15000)
        status = lambda: page.locator("#status").inner_text()   # noqa: E731

        # 1. Start: alleen het transcript. Van ondertitels is niets te zien, behalve de tab om ze te maken.
        check("start: alleen het transcript, geen ondertitels", ev("mode") == "transcript" and ev("layer.subtitles") is None)
        check("start: + Ondertitels staat er, de tab en Bewaren niet",
              page.locator("#subAdd").is_visible() and not page.locator("#modeS").is_visible() and not page.locator("#writeBtn").is_visible()
              and not page.locator("#subMore").is_visible())
        check("start: één baan op de tijdlijn", ev("laneTops().sub") is None and ev("laneTops().text") == 20)
        check("start: de knoppen van de tijdlijn heten zoals altijd",
              page.locator("#mergeBtn").inner_text().startswith("Samenvoegen") and page.locator("#splitBtn").inner_text().startswith("Splitsen"),
              page.locator("#mergeBtn").inner_text())

        # 2. + Ondertitels: het maakvenster met regels, inpassen, bewaren en de telling.
        page.click("#subAdd")
        page.locator("#mkCount b").wait_for(timeout=5000)
        check("maakvenster: zonder bestand naast de media is nieuw maken de keuze",
              page.is_checked('input[name="mkHow"][value="new"]') and page.locator("#mkLoadList input").count() == 0 and page.locator("#mkGo").inner_text() == "Maken en bewaren")
        check("maakvenster: open, met de voorkeuze voor streaming en het inpassen aan",
              page.locator("#makeBox").is_visible() and page.input_value("#exPreset") == "streaming" and page.is_checked("#ex_linger") and page.is_checked("#ex_merge"))
        check("maakvenster: zonder origineel alleen een andere plek",
              page.is_disabled('input[name="mkPlace"][value="media"]') and page.is_checked('input[name="mkPlace"][value="other"]'))
        first_count = page.locator("#mkCount").inner_text()
        check("maakvenster: de telling, met wat het zonder uitloop zou zijn", "ondertitels" in first_count and "zonder uitloop" in first_count, first_count)
        check("maakvenster: nog niets gemaakt of bewaard", refine.load_subtitles(A1) is None and ev("layer.subtitles") is None)
        page.uncheck("#ex_linger")
        page.wait_for_function("R.linger === 0 && !document.getElementById('mkCount').textContent.includes('zonder uitloop') && document.querySelector('#mkCount b')", timeout=5000)
        check("maakvenster: uitloop uit telt meteen opnieuw", page.locator("#mkCount").inner_text() != first_count and page.is_disabled("#ex_linger_ms"))
        page.check("#ex_linger")
        page.select_option("#exPreset", "broadcast")
        page.wait_for_function("R.max_chars === 37 && R.cps === 15")
        page.fill("#ex_max_chars", "30")
        page.wait_for_function("R.max_chars === 30")
        check("maakvenster: een eigen waarde heet eigen waarden", page.input_value("#exPreset") == "custom")
        page.select_option("#exPreset", "streaming")
        page.wait_for_function("R.max_chars === 42 && R.cps === 17 && R.linger === 1")
        page.wait_for_timeout(500)
        page.click("#mkGo")                                   # bewaarvenster geannuleerd
        page.wait_for_function("!busy && !document.getElementById('mkGo').disabled", timeout=5000)
        check("maken: annuleren in het bewaarvenster maakt niets", page.locator("#makeBox").is_visible() and refine.load_subtitles(A1) is None,
              refine.load_subtitles(A1))
        answer["path"] = str(out / "interview.srt")
        page.click("#mkGo")
        page.wait_for_function("mode === 'subtitles'", timeout=5000)

        # 3. Gemaakt: het bestand staat er, de tab Ondertitels is de actieve, hun baan staat bovenaan.
        srt = out / "interview.srt"
        check("maken: bewaarvenster met de naam van de media, het bestand staat er", asked[-1] == (None, "interview.srt") and srt.is_file(), asked[-1])
        check("maken: de statusregel zegt wat er is bewaard", status().startswith("Bewaard: interview.srt"), status())
        n_subs = ev("blocks.length")
        check("maken: de lijst toont de ondertitels, het venster is dicht",
              page.locator(".blk").count() == n_subs > 6 and not page.locator("#makeBox").is_visible(), n_subs)
        check("maken: tab, puntjes en Bewaren zijn er, + Ondertitels is weg",
              page.locator("#modeS").is_visible() and page.locator("#subMore").is_visible() and page.locator("#writeBtn").is_visible()
              and not page.locator("#subAdd").is_visible() and not page.locator("#save").is_visible())
        check("maken: de baan van de ondertitels staat boven die van het transcript", ev("laneTops().sub") == 20 and ev("laneTops().text") == 66)
        check("maken: sprekers niet te wijzigen bij ondertitels", page.locator("#addsp").count() == 0)
        seen = ev("blocks.map(b => [b.chk.lines, b.chk.level])")
        check("regelbreuk en niveau in het venster gelijk aan die van de app", python_view(None) == seen, f"{python_view(None)} != {seen}")
        check("twee niveaus: snel (oranje) en niet te lezen (rood)", ev("blocks.some(b => b.chk.level === 1)") and ev("blocks.some(b => b.chk.level === 2)"),
              ev("blocks.map(b => b.chk.level)"))
        count = page.locator("#count").inner_text()
        check("telling met beide niveaus", "ondertitels" in count and "snel" in count and "niet te lezen" in count, count)
        check("uitloop is te zien in de lijst", page.locator(".blk", has_text="uitloop").count() > 0)
        check("niets is met de hand aangepast", ev("blocks.some(isTouched)") is False)
        # In beeld staat een ondertitel in zijn eigen regels, ook in een smal venster; de titel blijft leesbaar.
        page.set_viewport_size({"width": 820, "height": 700})
        two = ev("blocks.findIndex(b => b.chk.lines.length === 2)")
        page.click(f"#b{two}")
        page.wait_for_function(f"document.getElementById('sub').textContent === blocks[{two}].chk.lines.join('\\n')", timeout=3000)
        shape = ev("(() => { const s = document.getElementById('sub'), c = getComputedStyle(s); return [c.whiteSpace, Math.round((s.offsetHeight - 8) / (parseFloat(c.fontSize) * 1.35)), s.offsetWidth <= s.parentElement.clientWidth]; })()")
        check("in beeld: precies twee regels, niet verder afgebroken, binnen het beeld", shape == ["pre", 2, True], shape)
        check("de titel blijft leesbaar naast een lange melding", ev("document.getElementById('title').offsetWidth") >= 150, ev("document.getElementById('title').offsetWidth"))
        page.set_viewport_size({"width": 1280, "height": 860})

        # 4. Instellingen: een andere regel verandert de waarschuwingen, niet de verdeling en niet het bestand.
        before_file = srt.read_text(encoding="utf-8")
        page.click("#rulesBtn")
        check("instellingen: hetzelfde formulier, nu onder de knop", page.locator("#rules #ex_cps").is_visible())
        warned = ev("blocks.filter(b => b.chk.level).length")
        page.fill("#ex_cps", "8")
        page.wait_for_function("R.cps === 8")
        page.wait_for_timeout(700)
        check("instellingen: een strengere leessnelheid geeft meer waarschuwingen, dezelfde ondertitels",
              ev("blocks.length") == n_subs and ev("blocks.filter(b => b.chk.level).length") > warned and srt.read_text(encoding="utf-8") == before_file)
        page.fill("#ex_cps", "17")
        page.wait_for_function("R.cps === 17")
        page.wait_for_timeout(400)
        page.wait_for_function("saveTimer === null && saving === 0", timeout=4000)
        check("instellingen: de regels gaan mee met de laag op deze Mac", refine.load_subtitles(A1)["rules"]["cps"] == 17)

        # 5. Naar de volgende waarschuwing, een ondertitel inkorten, en bewaren.
        page.click("#warnNext")
        check("volgende waarschuwing kiest een ondertitel met een waarschuwing", ev("picked") >= 0 and ev("blocks[picked].chk.level") > 0)
        fast = ev("blocks.findIndex(b => b.chk.level === 2 && b.display.split(' ').length > 4)")
        short = " ".join(ev(f"blocks[{fast}].display").split()[:3])
        page.dblclick(f"#b{fast} .txt")
        page.keyboard.press(SELECT_ALL)
        page.keyboard.type(short)
        page.keyboard.press("Control+Enter")
        page.wait_for_function(f"blocks[{fast}].display === {json.dumps(short)}")
        check("ingekort: geteld als nog niet in het bestand", ev("edits") == 1 and "nog niet in het bestand" in status(), status())
        check("ingekort: deze ondertitel is met de hand aangepast, de rest niet", ev(f"isTouched(blocks[{fast}])") and ev("blocks.filter(isTouched).length") == 1)
        page.wait_for_function("saveTimer === null && saving === 0", timeout=4000)
        check("ingekort: de laag is bewaard op deze Mac, het bestand en het transcript niet veranderd",
              refine.load_subtitles(A1)["edits"] == 1 and srt.read_text(encoding="utf-8") == before_file and not refine._draft_path(A1).exists())
        page.click("#writeBtn")
        page.wait_for_function("edits === 0 && !busy", timeout=5000)
        check("bewaren: het bestand is bijgewerkt", short in srt.read_text(encoding="utf-8") and status().startswith("Bewaard: interview.srt"), status())

        # 6. Inpassen: uitloop uit en opnieuw inpassen. Wat met de hand is aangepast blijft staan.
        lingered = ev("blocks.filter(b => b.chk.linger >= 0.1).length")
        page.uncheck("#ex_linger")
        page.wait_for_function("R.linger === 0")
        page.click("#fitBtn")
        page.wait_for_function("!busy && document.getElementById('status').textContent.includes('ingepast')", timeout=5000)
        check("inpassen: de uitloop is weg bij ondertitels waar niemand aan zat",
              ev("blocks.filter(b => b.chk.linger >= 0.1 && !isTouched(b)).length") < lingered and ev("edits") == 1, ev("blocks.map(b => b.chk.linger)"))
        check("inpassen: de ingekorte ondertitel staat er nog precies zo", ev(f"blocks.some(b => b.display === {json.dumps(short)} && isTouched(b))"))
        page.check("#ex_linger")
        page.wait_for_function("R.linger === 1")
        page.click("#fitBtn")
        page.wait_for_function("!busy && blocks.some(b => b.chk.linger >= 0.1 && !isTouched(b))", timeout=5000)

        # 7. Samenvoegen en ongedaan maken werken op de laag die in beeld is.
        n_before, e_before = ev("blocks.length"), ev("edits")
        page.click("#b0")
        page.keyboard.press("m")
        check("samenvoegen werkt op ondertitels", ev("blocks.length") == n_before - 1 and ev("edits") == e_before + 1)
        page.keyboard.press("Control+z")
        check("ongedaan maken werkt per laag", ev("blocks.length") == n_before and ev("edits") == e_before)

        # 8. Het transcript wijzigen: de ondertitels blijven staan en melden dat ze verouderd zijn.
        page.click("#modeT")
        page.wait_for_function("mode === 'transcript'")
        check("terug in het transcript: 6 blokken, opslaan in beeld, Bewaren niet",
              ev("blocks.length") == 6 and page.locator("#save").is_visible() and not page.locator("#writeBtn").is_visible())
        page.dblclick("#b1 .txt")
        page.keyboard.press("End")
        page.keyboard.type(" Toch?")
        page.keyboard.press("Control+Enter")
        page.wait_for_function("subStale === true", timeout=5000)
        check("de ondertitels blijven staan als het transcript verandert", ev("layer.subtitles.blocks.length") == n_before)
        page.click("#modeS")
        page.wait_for_function("mode === 'subtitles'")
        check("melding dat het transcript is veranderd", "veranderd" in page.locator("#subNote").inner_text(), page.locator("#subNote").inner_text())

        # 9. Opnieuw maken via de puntjes: het maakvenster opnieuw, het handwerk weg, de nieuwe tekst erin.
        page.click("#subMore")
        page.click('#subMenu [data-act="again"]')
        page.locator("#mkCount b").wait_for(timeout=5000)
        check("opnieuw maken: het venster waarschuwt dat handwerk verloren gaat", "verloren" in page.locator("#mkIntro").inner_text())
        page.check('input[name="mkFmt"][value="both"]')
        page.click("#mkGo")
        page.wait_for_function("!busy && subStale === false && blocks.some(b => b.display.includes('Toch?'))", timeout=5000)
        check("opnieuw maken: vers verdeeld, niets met de hand aangepast, beide bestanden geschreven",
              ev("blocks.some(isTouched)") is False and ev("edits") == 0 and "Toch?" in srt.read_text(encoding="utf-8")
              and "<v Spreker" in (out / "interview.vtt").read_text(encoding="utf-8"), status())

        # 10. Herladen: de ondertitels en het concept van het transcript komen terug.
        page.dblclick("#b0 .txt")
        page.keyboard.press(SELECT_ALL)
        page.keyboard.type("Toen we aankwamen,")
        page.keyboard.press("Control+Enter")
        page.wait_for_function("edits === 1")
        page.wait_for_function("saveTimer === null && saving === 0", timeout=4000)
        page.wait_for_timeout(300)
        page.reload()
        page.wait_for_function("D && layer.subtitles && layer.subtitles.blocks.length > 0", timeout=15000)
        check("na herladen: het transcript in beeld, de ondertitels zijn er met hun wijziging",
              ev("mode") == "transcript" and ev("layer.subtitles.edits") == 1 and ev("layer.subtitles.blocks[0].display") == "Toen we aankwamen,"
              and ev("isTouched(layer.subtitles.blocks[0])") and ev("subStale") is False)
        check("na herladen: het concept van het transcript", ev("blocks[1].display").endswith("Toch?"))
        check("na herladen: twee banen, de ondertitels bovenaan", ev("laneTops().sub") == 20 and page.locator("#modeS").is_visible())
        # De opdrachtenlijst toont dat er ondertitels zijn; het label springt naar die tab in Refine.
        from datetime import datetime
        with serve.jobs_lock:
            serve.jobs.append(serve.job_list.Job(n=1, item=A1, title="Interview na de wedstrijd", status="klaar",
                                                 started=datetime.now(), finished=datetime.now()))
        page.goto(f"{BASE}/")
        label = page.locator("[data-subtitles]")
        label.wait_for(timeout=5000)
        check("opdrachtenlijst: het label Ondertitels staat bij de opdracht", label.inner_text() == "Ondertitels")
        look = label.evaluate("el => { const s = getComputedStyle(el); return [s.borderTopWidth, s.fontSize, s.paddingTop]; }")
        check("opdrachtenlijst: het ziet eruit als een label, met een rand en in het kleine lettertype", look == ["1px", "11px", "0px"], look)
        label.click()
        page.wait_for_function("typeof mode !== 'undefined' && D && mode === 'subtitles'", timeout=15000)
        check("opdrachtenlijst: het label opent Refine meteen in de ondertitels", ev("blocks[0].display") == "Toen we aankwamen," and "tab=subtitles" in page.url)
        page.goto(f"{BASE}/refine?asset=A1")   # een oude link met alleen het asset: de enige Iconik
        page.wait_for_function("D && layer.subtitles && layer.subtitles.blocks.length > 0", timeout=15000)

        # 11. Tijdlijn: een klik op de baan van de andere laag wisselt daarheen.
        box = page.locator("#canvas").bounding_box()
        page.mouse.click(box["x"] + 200, box["y"] + 20 + 23)             # de baan van de ondertitels
        page.wait_for_function("mode === 'subtitles'", timeout=3000)
        page.mouse.click(box["x"] + 200, box["y"] + 20 + 46 + 23)        # de baan van het transcript
        page.wait_for_function("mode === 'transcript'", timeout=3000)
        check("klik op de andere baan wisselt van laag", True)

        # 12. Op een andere plek bewaren via de puntjes.
        answer["path"] = str(out / "kopie.srt")
        page.click("#subMore")
        page.click('#subMenu [data-act="elsewhere"]')
        page.wait_for_function("mode === 'subtitles' && !busy && subTarget && subTarget.names[0] === 'kopie.srt'", timeout=5000)
        check("andere plek: daar geschreven, en dat is voortaan de plek",
              "Toen we aankwamen," in (out / "kopie.srt").read_text(encoding="utf-8") and (out / "kopie.vtt").is_file() and ev("edits") == 0, status())

        # 13. Verwijderen: twee keer klikken, de laag weg, de bestanden blijven, terug naar alleen het transcript.
        page.click("#subMore")
        page.click('#subMenu [data-act="delete"]')
        check("verwijderen vraagt eerst om bevestiging", "Klik nogmaals" in page.locator('#subMenu [data-act="delete"]').inner_text()
              and refine.load_subtitles(A1) is not None)
        page.click('#subMenu [data-act="delete"]')
        page.wait_for_function("mode === 'transcript' && layer.subtitles === null && !busy", timeout=5000)
        check("verwijderd: de laag weg, de bestanden blijven staan", refine.load_subtitles(A1) is None and (out / "kopie.srt").is_file() and srt.is_file())
        check("verwijderd: weer alleen het transcript, met + Ondertitels",
              page.locator("#subAdd").is_visible() and not page.locator("#modeS").is_visible() and ev("laneTops().sub") is None)

        # 14. Een bestaand bestand inladen: de ondertitels zoals ze erin staan, en Bewaren schrijft ernaar terug.
        made_file = out / "van-iemand-anders.srt"
        made_file.write_text("1\n00:00:01,000 --> 00:00:03,000\nDus toen we daar aankwamen,\n\n2\n00:00:09,000 --> 00:00:11,000\nEcht? Niet te geloven.\n", encoding="utf-8")
        page.click("#subAdd")
        page.locator("#mkCount b").wait_for(timeout=5000)
        page.check('input[name="mkHow"][value="pick"]')
        check("inladen: het venster toont alleen nog de regels en de uitleg",
              page.locator("#mkGo").inner_text() == "Inladen" and not page.locator("#mkCount").is_visible() and not page.locator("#ex_linger").is_visible()
              and page.locator("#ex_max_chars").is_visible() and page.locator("#mkLoadNote").is_visible())
        page.click("#mkGo")                                   # kiezen geannuleerd
        page.wait_for_function("!busy && !document.getElementById('mkGo').disabled", timeout=5000)
        check("inladen: annuleren bij het kiezen laadt niets", page.locator("#makeBox").is_visible() and refine.load_subtitles(A1) is None)
        answer["open"] = str(made_file)
        page.click("#mkGo")
        page.wait_for_function("mode === 'subtitles' && !busy", timeout=5000)
        check("inladen: de ondertitels uit het bestand, met de sprekers uit het transcript",
              ev("blocks.map(b => b.display)") == ["Dus toen we daar aankwamen,", "Echt? Niet te geloven."] and ev("blocks.map(b => b.speaker)") == ["1", "2"]
              and status().startswith("Ingeladen: van-iemand-anders.srt"), status())
        check("inladen: woordtiming uit het transcript, alles telt als met de hand gemaakt",
              ev("blocks[0].words[1].start") == 1.3 and ev("blocks.every(isTouched)"), ev("blocks[0].words.map(w => w.start)"))
        page.dblclick("#b1 .txt")
        page.keyboard.press(SELECT_ALL)
        page.keyboard.type("Echt?")
        page.keyboard.press("Control+Enter")
        page.wait_for_function("edits === 1")
        page.click("#writeBtn")
        page.wait_for_function("edits === 0 && !busy", timeout=5000)
        check("inladen: Bewaren schrijft terug naar het ingeladen bestand",
              made_file.read_text(encoding="utf-8").endswith("\nEcht?\n") and "Niet te geloven" not in made_file.read_text(encoding="utf-8"), made_file.read_text(encoding="utf-8"))
    finally:
        browser.close()

check("geen fouten in het venster", not console, console[:5])
check("geen mislukte verzoeken", not errors, errors[:5])
print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
