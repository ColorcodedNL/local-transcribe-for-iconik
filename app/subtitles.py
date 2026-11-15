"""Ondertitels uit het eigen transcriptmodel: .srt en .vtt.

Een transcriptblok is geen ondertitel: het mag zo lang zijn als iemand praat. Hier worden de woorden van
elk blok opnieuw verdeeld over ondertitels die aan regels voldoen (tekens per regel, aantal regels,
kortste en langste duur, ruimte ertussen). De regels zijn een voorkeuze (PRESETS) of eigen waarden.

Daarna volgt het inpassen, wat de tekst nooit verandert: korte zinnen van dezelfde spreker komen samen als
er geen pauze tussen zit (merge), en een ondertitel die te snel gaat blijft staan in de stilte erna
(linger, de uitloop). Wat dan nog te snel is, is een waarschuwing en geen fout: de tekst blijft letterlijk.

Ondertitels zijn een eigen laag (layer): een transcript waarin elk blok één ondertitel is. Iemand maakt die
laag bewust in Refine, bewerkt hem met dezelfde editor als het transcript en bewaart hem als bestand.
write_files zet dat bestand neer; wat de app niet zelf schreef, blijft staan tenzij iemand het zo koos.
Alleen de standaardbibliotheek en het eigen model.
"""
import difflib
import hashlib
import html
import json
import os
import re
from pathlib import Path

import transcript as tmodel

PRESETS = {
    # online en streaming: 42 tekens (Netflix, de meeste talen met Latijns schrift), 17 tekens per seconde
    "streaming": {"max_chars": 42, "max_lines": 2, "min_ms": 1000, "max_ms": 7000, "gap_ms": 80, "cps": 17},
    # televisie met teletekst: 37 tekens met vaste breedte (BBC), rustiger leestempo
    "broadcast": {"max_chars": 37, "max_lines": 2, "min_ms": 1000, "max_ms": 7000, "gap_ms": 80, "cps": 15},
}
DEFAULT_PRESET = "streaming"
FIT = {"linger": 1, "linger_ms": 2000, "merge": 1, "merge_gap_ms": 500}   # het inpassen; staat los van de voorkeuze
LIMITS = {"max_chars": (20, 80), "max_lines": (1, 3), "min_ms": (300, 3000), "max_ms": (2000, 15000),
          "gap_ms": (0, 500), "cps": (8, 30),
          "linger": (0, 1), "linger_ms": (0, 5000), "merge": (0, 1), "merge_gap_ms": (0, 2000)}
FORMATS = ("srt", "vtt")
CHOICES = {"srt": ("srt",), "vtt": ("vtt",), "both": ("srt", "vtt")}   # wat iemand bij het bewaren kiest

PAUSE_MS = 1500          # zo'n stilte binnen een blok begint altijd een nieuwe ondertitel
SHORTEST_MS = 300        # korter maken we een ondertitel nooit om ruimte te maken voor de volgende
UNREADABLE = 1.5         # zoveel keer de leessnelheid: niet meer te lezen (rood); daaronder alleen snel (oranje)
_SENTENCE_END = re.compile(r"[.!?…][\"'”’»)\]]*$")
_CLAUSE_END = re.compile(r"[,;:][\"'”’»)\]]*$")
# Woorden waar een regel liever niet op eindigt (ze horen bij wat volgt). Klein en alleen nl/en: het is een
# voorkeur bij het kiezen van de regelbreuk, geen regel.
_WEAK = {"de", "het", "een", "en", "of", "van", "in", "op", "te", "met", "voor", "naar", "om", "dat", "die",
         "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "with", "that"}


def clean_rules(raw=None):
    """Regels zoals de app ze gebruikt: de voorkeuze en het inpassen, met wat er in raw aan geldige getallen staat."""
    rules = dict(PRESETS[DEFAULT_PRESET], **FIT)
    for key, (low, high) in LIMITS.items():
        try:
            rules[key] = max(low, min(high, int(round(float((raw or {})[key])))))
        except (KeyError, TypeError, ValueError):
            pass
    rules["max_ms"] = max(rules["max_ms"], rules["min_ms"] + 500)
    return rules


def preset_of(rules):
    """De naam van de voorkeuze waar deze regels precies mee overeenkomen, anders "custom"."""
    for name, preset in PRESETS.items():
        if all(rules.get(key) == value for key, value in preset.items()):
            return name
    return "custom"


def _words(segment):
    """(tekst, begin, eind, zekerheid) per woord. Een blok zonder woorden: de tekst gelijkmatig over de duur."""
    words = [(part, int(w.start), max(int(w.start), int(w.end)), w.score)
             for w in segment.words for part in w.text.split()]
    if words:
        return words
    parts = segment.display_text().split()
    total = sum(len(p) for p in parts)
    if not total:
        return []
    out, at, span = [], float(segment.start), max(0, segment.end - segment.start)
    for part in parts:
        step = span * len(part) / total
        out.append((part, int(at), int(at + step), 1.0))
        at += step
    return out


def _greedy(texts, width):
    lines, line = [], ""
    for text in texts:
        if line and len(line) + 1 + len(text) > width:
            lines.append(line)
            line = text
        else:
            line = f"{line} {text}" if line else text
    return lines + ([line] if line else [])


def _fits(texts, rules):
    return len(_greedy(texts, rules["max_chars"])) <= rules["max_lines"]


def layout(texts, rules):
    """De regels van één ondertitel. Past het op één regel, dan één regel; anders zo gelijk mogelijk
    verdeeld, bij voorkeur gebroken na een leesteken en niet na een lidwoord of voorzetsel.
    Dezelfde keuzes staan in refine.html (layoutLines) voor het scherm: wie hier iets verandert, daar ook."""
    width, most = rules["max_chars"], rules["max_lines"]
    whole = " ".join(texts)
    if len(whole) <= width or most == 1 or len(texts) < 2:
        return [whole]
    if most == 2:
        best = None
        for i in range(1, len(texts)):
            first, second = " ".join(texts[:i]), " ".join(texts[i:])
            if len(first) > width or len(second) > width:
                continue
            last = texts[i - 1]
            score = abs(len(first) - len(second))
            score -= 12 if _SENTENCE_END.search(last) or _CLAUSE_END.search(last) else 0
            score += 16 if last.lower().strip("\"'“‘«(") in _WEAK else 0
            if best is None or score < best[0]:
                best = (score, [first, second])
        if best:
            return best[1]
    for narrow in range(max(1, -(-len(whole) // most)), width + 1):   # smalste breedte die nog past
        lines = _greedy(texts, narrow)
        if len(lines) <= most:
            return lines
    return _greedy(texts, width)   # een woord langer dan een regel: dan maar meer regels


def _back_up(group):
    """Een volle ondertitel liever eerder breken, na een zin of een komma. Geeft (kop, staart)."""
    length = len(" ".join(w[0] for w in group))
    sentence = clause = None
    for k in range(len(group) - 1):
        head = len(" ".join(w[0] for w in group[:k + 1]))
        if _SENTENCE_END.search(group[k][0]) and head >= 0.25 * length:
            sentence = k
        elif _CLAUSE_END.search(group[k][0]) and head >= 0.3 * length:
            clause = k
    cut = sentence if sentence is not None else clause
    if cut is None:
        # Geen leesteken om op te breken: dan in elk geval niet eindigen op een lidwoord of voorzetsel.
        cut = len(group) - 1
        while cut > len(group) - 3 and cut > 0 and group[cut][0].lower() in _WEAK:
            cut -= 1
        if cut == len(group) - 1:
            return group, []
    return group[:cut + 1], group[cut + 1:]


def _groups(words, rules):
    """Verdeelt de woorden van één blok over ondertitels: elke zin de zijne, en een zin die niet past
    gebroken op de beste plek. Korte zinnen weer samenvoegen is een stap van het inpassen (_merge)."""
    groups, group = [], []
    for word in words:
        if group:
            pause = word[1] - group[-1][2] >= PAUSE_MS
            full = word[2] - group[0][1] > rules["max_ms"] or not _fits([w[0] for w in group] + [word[0]], rules)
            if full and not pause:
                head, tail = _back_up(group)
                if tail and _fits([w[0] for w in tail] + [word[0]], rules):
                    groups.append(head)
                    group = tail
                else:
                    groups.append(group)
                    group = []
            elif pause:
                groups.append(group)
                group = []
        group.append(word)
        if _SENTENCE_END.search(word[0]):
            groups.append(group)
            group = []
    return groups + ([group] if group else [])


def _text(words):
    return " ".join(w[0] for w in words)


def _merge(parts, rules):
    """Korte zinnen bij hun buur van dezelfde spreker, als er geen pauze tussen zit en het geheel nog binnen
    de regels past. parts: [(spreker, woorden, vast)] op volgorde; vast = met de hand aangepast, blijft zo."""
    if not rules["merge"]:
        return parts
    short, out = rules["max_chars"] // 2, []
    for speaker, words, fixed in parts:
        if out and not fixed and not out[-1][2] and out[-1][0] == speaker:
            before = out[-1][1]
            close = 0 <= words[0][1] - before[-1][2] <= rules["merge_gap_ms"]
            small = len(_text(before)) < short or len(_text(words)) < short
            whole = before + words
            if (close and small and _SENTENCE_END.search(before[-1][0]) and _fits([w[0] for w in whole], rules)
                    and whole[-1][2] - whole[0][1] <= rules["max_ms"]):
                out[-1] = (speaker, whole, False)
                continue
        out.append((speaker, words, fixed))
    return out


def _chars(lines):
    return sum(len(line) for line in lines) + len(lines) - 1


def _time(cue_list, rules):
    """De tijden van ondertitels die de app mag inpassen (niet "fixed"): ruimte laten voor de volgende, een
    te korte rekken, en een te snelle laten uitlopen in de stilte erna. Nooit over de volgende heen."""
    cue_list.sort(key=lambda cue: (cue["start"], cue["end"]))
    for i, cue in enumerate(cue_list):
        if cue.get("fixed"):
            continue
        limit = cue_list[i + 1]["start"] - rules["gap_ms"] if i + 1 < len(cue_list) else None
        if limit is not None and cue["end"] > limit and limit - cue["start"] >= SHORTEST_MS:
            cue["end"] = limit
        wanted = cue["start"] + rules["min_ms"]
        if rules["linger"]:   # zolang als het lezen vraagt, hooguit linger_ms na het laatste woord
            reading = cue["start"] + -(-_chars(cue["lines"]) * 1000 // rules["cps"])
            wanted = max(wanted, min(reading, cue["end"] + rules["linger_ms"]))
        if cue["end"] < wanted:
            cue["end"] = wanted if limit is None else max(cue["end"], min(wanted, limit))
    return cue_list


def _cue(speaker, words, rules, fixed=False, end=None):
    return {"start": words[0][1], "end": max(words[-1][2], words[0][1]) if end is None else end,
            "lines": layout([w[0] for w in words], rules), "speaker": speaker, "words": words, "fixed": fixed}


def cues(transcript, rules=None):
    """Het transcript verdeeld en ingepast: [{"start", "end", "lines", "speaker", "words"}], tijden in ms."""
    rules = clean_rules(rules)
    parts = [(segment.speaker, group, False) for segment in transcript.segments
             for group in _groups(_words(segment), rules)]
    parts.sort(key=lambda part: (part[1][0][1], part[1][-1][2]))
    return _time([_cue(speaker, words, rules) for speaker, words, _ in _merge(parts, rules)], rules)


def _layer(transcript, cue_list):
    segments = []
    for cue in cue_list:
        low, high = cue["start"], cue["end"]
        words = [tmodel.Word(text, min(max(start, low), high), min(max(end, low), high), score)
                 for text, start, end, score in cue["words"]]
        segments.append(tmodel.Segment(low, high, cue["speaker"], words))
    return tmodel.Transcript(language=transcript.language, speakers=list(transcript.speakers), segments=segments,
                             tracks=list(transcript.tracks), engine=dict(transcript.engine), source=dict(transcript.source))


def layer(transcript, rules=None):
    """De ondertitels als eigen laag: een transcript waarin elk blok één ondertitel is, met zijn woorden.
    De woordtijden blijven binnen de ondertitel, ook waar die is ingekort om ruimte te laten voor de volgende."""
    return _layer(transcript, cues(transcript, rules))


def fit_layer(layer_doc, touched, rules=None):
    """Past een bestaande laag opnieuw in met deze regels: samenvoegen en tijden (uitloop). Wat iemand met de
    hand aanpaste (touched, een lijst waar/niet waar naast de blokken) blijft precies zoals het is.
    Geeft (laag, touched) terug; verdelen doet dit niet opnieuw, dat is layer()."""
    rules = clean_rules(rules)
    parts, bounds = [], []
    for index, segment in enumerate(layer_doc.segments):
        words = _words(segment)
        if words:
            parts.append((segment.speaker, words, index < len(touched or []) and bool(touched[index])))
            bounds.append((int(segment.start), int(segment.end)))
    order = sorted(range(len(parts)), key=lambda i: bounds[i])
    parts, bounds = [parts[i] for i in order], [bounds[i] for i in order]
    span = {id(words): bound for (_, words, _), bound in zip(parts, bounds)}
    cue_list = []
    for speaker, words, fixed in _merge(parts, rules):
        cue = _cue(speaker, words, rules, fixed)
        if fixed:                                   # ook de tijden die iemand zelf zette
            cue["start"], cue["end"] = span[id(words)]
        cue_list.append(cue)
    cue_list = _time(cue_list, rules)
    return _layer(layer_doc, cue_list), [bool(cue["fixed"]) for cue in cue_list]


def cues_of_layer(layer_doc, rules=None):
    """Een laag als ondertitels: elk blok is er één, zoals het staat. Alleen de regelbreuk komt uit de regels."""
    rules = clean_rules(rules)
    out = []
    for segment in layer_doc.segments:
        texts = segment.display_text().split()
        if texts:
            out.append({"start": int(segment.start), "end": max(int(segment.end), int(segment.start)),
                        "lines": layout(texts, rules), "speaker": segment.speaker})
    out.sort(key=lambda cue: (cue["start"], cue["end"]))
    return out


def warnings(cue, following, rules):
    """Wat er aan één ondertitel niet klopt, en hoe erg: (lijst, niveau). Niveau 1 (oranje): sneller dan de
    leessnelheid, te kort of te lang. Niveau 2 (rood): niet te lezen, past niet binnen de regels, of loopt
    over de volgende heen. Dezelfde keuzes staan in refine.html (check)."""
    found = []
    duration = max(1, cue["end"] - cue["start"])
    speed = _chars(cue["lines"]) * 1000 / duration
    if speed > rules["cps"] * UNREADABLE:
        found.append("unreadable")
    elif speed > rules["cps"]:
        found.append("fast")
    if len(cue["lines"]) > rules["max_lines"] or any(len(line) > rules["max_chars"] for line in cue["lines"]):
        found.append("wide")
    if duration < rules["min_ms"] - 1:
        found.append("short")
    if duration > rules["max_ms"] + 1:
        found.append("long")
    if following is not None and following["start"] < cue["end"] - 1:
        found.append("overlap")
    level = 2 if {"unreadable", "wide", "overlap"} & set(found) else 1 if found else 0
    return found, level


def report(cue_list, rules=None):
    """Tellingen: hoeveel ondertitels, hoeveel goed, hoeveel met een lichte (fast) en een zware (bad) waarschuwing."""
    rules = clean_rules(rules)
    levels = [warnings(cue, cue_list[i + 1] if i + 1 < len(cue_list) else None, rules)[1]
              for i, cue in enumerate(cue_list)]
    return {"cues": len(cue_list), "good": levels.count(0), "fast": levels.count(1), "bad": levels.count(2),
            "cps": rules["cps"]}


def preview(transcript, rules=None):
    """De telling voor het maakvenster, met erbij hoeveel waarschuwingen er zonder uitloop zouden zijn."""
    rules = clean_rules(rules)
    counted = report(cues(transcript, rules), rules)
    plain = report(cues(transcript, dict(rules, linger=0)), rules)
    counted["without_linger"] = plain["fast"] + plain["bad"]
    return counted


def _clock(ms, comma):
    ms = max(0, int(ms))
    hours, rest = divmod(ms, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{',' if comma else '.'}{millis:03d}"


def to_srt(cue_list):
    """SubRip. Geen sprekernamen: die zouden in beeld staan."""
    blocks = [f"{n}\n{_clock(c['start'], True)} --> {_clock(c['end'], True)}\n" + "\n".join(c["lines"])
              for n, c in enumerate(cue_list, 1)]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _vtt_text(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_vtt(cue_list, transcript=None):
    """WebVTT. De spreker staat als <v Naam> in elke ondertitel: spelers tonen hem niet, tenzij ze dat willen."""
    blocks = ["WEBVTT"]
    for cue in cue_list:
        text = "\n".join(_vtt_text(line) for line in cue["lines"])
        name = transcript.speaker_name(cue["speaker"]) if transcript is not None else ""
        name = re.sub(r"[<>\r\n]", " ", name or "").strip()
        if name:
            text = f"<v {_vtt_text(name)}>{text}"
        blocks.append(f"{_clock(cue['start'], False)} --> {_clock(cue['end'], False)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def render(layer_doc, fmt, rules=None):
    """(tekst van het bestand, telling) van een laag ondertitels. fmt is "srt" of "vtt"."""
    if fmt not in FORMATS:
        raise ValueError(f"onbekend formaat: {fmt!r:.20}")
    cue_list = cues_of_layer(layer_doc, rules)
    text = to_srt(cue_list) if fmt == "srt" else to_vtt(cue_list, layer_doc)
    return text, report(cue_list, rules)


# --- een bestaand bestand inladen ---

_TIME = r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
_TIMING = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}")
_VOICE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]*)>")
_TAG = re.compile(r"<[^>]*>|\{\\[^}]*\}")
MAX_IMPORT = 20 * 1024 * 1024


def _ms(hours, minutes, seconds, fraction):
    return ((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(fraction.ljust(3, "0")[:3])


def parse(data):
    """Leest .srt of .vtt (bytes of tekst): [{"start", "end", "text", "voice"}] op volgorde, tijden in ms.
    Opmaak (<i>, {\\an8}), nummers, NOTE- en STYLE-blokken en de plaatsing achter de tijden vallen weg;
    de naam uit <v Naam> blijft apart bewaard. Wat geen ondertitel is, wordt overgeslagen."""
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            data = data.decode("cp1252", errors="replace")
    out = []
    for block in re.split(r"\n[ \t]*\n", data.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")):
        lines = [line for line in block.split("\n") if line.strip()]
        at = next((i for i, line in enumerate(lines) if _TIMING.match(line)), None)
        if at is None:
            continue
        found = _TIMING.match(lines[at]).groups()
        start, end = _ms(*found[:4]), _ms(*found[4:])
        raw = " ".join(lines[at + 1:])
        voice = _VOICE.search(raw)
        text = " ".join(html.unescape(_TAG.sub("", raw)).split())
        if text and end >= start:
            out.append({"start": start, "end": end, "text": text, "voice": html.unescape(voice.group(1)).strip() if voice else ""})
    out.sort(key=lambda cue: (cue["start"], cue["end"]))
    return out


def _plain(word):
    return re.sub(r"[^\w]", "", word.lower())


def import_layer(cue_list, transcript):
    """Ingelezen ondertitels als laag naast een transcript. Wat het bestand niet heeft, komt uit het transcript:
    de spreker (de naam uit <v>, anders het blok dat in de tijd het meest overlapt) en de woordtiming (waar de
    tekst overeenkomt met wat er rond die tijd gezegd is; de rest gelijkmatig ertussen verdeeld)."""
    spoken = sorted((w for segment in transcript.segments for w in _words(segment)), key=lambda w: w[1])
    names = {speaker.name.strip().lower(): speaker.id for speaker in transcript.speakers}
    fallback = transcript.speakers[0].id if transcript.speakers else "1"
    segments = []
    for cue in cue_list:
        low, high = cue["start"], max(cue["end"], cue["start"])
        speaker = names.get(cue["voice"].lower()) if cue["voice"] else None
        if speaker is None:
            overlap = [(min(high, s.end) - max(low, s.start), -i, s.speaker) for i, s in enumerate(transcript.segments)]
            best = max(overlap, default=None)
            speaker = best[2] if best and best[0] > 0 else fallback
        tokens = cue["text"].split()
        near = [w for w in spoken if w[2] > low - 1000 and w[1] < high + 1000]
        times = [None] * len(tokens)
        matcher = difflib.SequenceMatcher(None, [_plain(t) for t in tokens], [_plain(w[0]) for w in near], autojunk=False)
        for a, b, size in matcher.get_matching_blocks():
            for k in range(size):
                if _plain(tokens[a + k]):
                    word = near[b + k]
                    times[a + k] = (min(max(word[1], low), high), min(max(word[2], low), high), word[3])
        words, i = [], 0
        while i < len(tokens):
            if times[i] is not None:
                words.append(tmodel.Word(tokens[i], times[i][0], max(times[i][1], times[i][0]), times[i][2]))
                i += 1
                continue
            j = i
            while j < len(tokens) and times[j] is None:
                j += 1
            begin = words[-1].end if words else low
            until = max(begin, times[j][0] if j < len(tokens) else high)
            step = (until - begin) / (j - i)
            for k in range(i, j):       # niet teruggevonden in het transcript: gelijkmatig over de ruimte ertussen
                words.append(tmodel.Word(tokens[k], int(begin + (k - i) * step), int(begin + (k - i + 1) * step), 1.0))
            i = j
        for k in range(1, len(words)):  # op volgorde houden, ook als het transcript een woord twee keer heeft
            words[k].start = max(words[k].start, words[k - 1].start)
            words[k].end = max(words[k].end, words[k].start)
        segments.append(tmodel.Segment(low, high, speaker, words))
    return tmodel.Transcript(language=transcript.language, speakers=list(transcript.speakers), segments=segments,
                             tracks=list(transcript.tracks), engine=dict(transcript.engine), source=dict(transcript.source))


def adopt(path, registry_path):
    """Iemand koos dit bestand zelf om in te laden: vanaf nu mag de app het bijwerken, zolang het niet door
    iets anders verandert."""
    written = _registry(registry_path)
    try:
        written[str(path)] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        registry_path = Path(registry_path)
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        temp = registry_path.with_name(registry_path.name + ".tmp")
        temp.write_text(json.dumps(written, ensure_ascii=False))
        os.replace(temp, registry_path)
    except OSError:
        pass


# --- bestanden ---

def _registry(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_files(layer_doc, base, choice, rules, registry_path, confirmed=()):
    """Schrijft de laag als bestand(en): base met de extensie van elk formaat (clip.mxf wordt clip.srt).

    choice is een sleutel van CHOICES. registry_path is het bestand waarin de app bijhoudt wat hij zelf
    schreef (pad en hash): alleen een bestand dat daar nog precies mee overeenkomt wordt vervangen. Zo blijft
    een ondertitel van iemand anders, of een die na het schrijven is bewerkt, staan. confirmed: de formaten
    waarvoor iemand in het bewaarvenster zelf voor dit pad koos; die worden altijd geschreven.
    Geeft per formaat {"name", "path", "state": "written" | "kept" | "failed", "detail", telling}."""
    base, results = Path(base), []
    written = _registry(registry_path)
    for fmt in CHOICES.get(choice, ()):
        target = base.with_suffix(f".{fmt}")
        result = {"name": target.name, "path": str(target), "state": "written", "detail": ""}
        results.append(result)
        try:
            text, counted = render(layer_doc, fmt, rules)
            result.update(counted)
            if not counted["cues"]:
                result.update(state="kept", detail="empty")
                continue
            if fmt not in confirmed and target.exists():
                if written.get(str(target)) != hashlib.sha256(target.read_bytes()).hexdigest():
                    result.update(state="kept", detail="exists")
                    continue
            data = text.encode("utf-8")
            temp = target.with_name(f".{target.name}.tmp")
            temp.write_bytes(data)
            os.replace(temp, target)
            written[str(target)] = hashlib.sha256(data).hexdigest()
        except OSError as error:
            result.update(state="failed", detail=error.strerror or str(error))
    if any(r["state"] == "written" for r in results):
        try:
            registry_path = Path(registry_path)
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            temp = registry_path.with_name(registry_path.name + ".tmp")
            temp.write_text(json.dumps(written, ensure_ascii=False))
            os.replace(temp, registry_path)
        except OSError:
            pass   # zonder lijst blijft het bestand de volgende keer staan: de veilige kant
    return results
