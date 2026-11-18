"""Wat er na Whisper afgaat: zinnen splitsen, herhalingen, hallucinaties en taal die niet klopt.
"""
import re

from base import load_config  # noqa: F401
from speakers import speech_share  # noqa: F401


# Een regel van Whisper wordt op zinseinden in zinnen geknipt; elke zin krijgt een spreker.
SENTENCE_END = (".", "?", "!", "\u2026")

REPEAT_LIMIT = 4  # dezelfde zin zo vaak achter elkaar geldt als een lus van Whisper

# Verzonnen tekst begint met een eerste woord waar Whisper nauwelijks in gelooft,
# op een plek waar het sprekermodel weinig of geen spraak hoort. Beide moeten gelden.
FIRST_WORD_MIN_PROBABILITY = 0.05
MIN_SPEECH_SHARE = 0.5

# Gemiddelde woordzekerheid waaronder een heel asset als onverstaanbaar geldt.
# Gemeten: geroezemoes via cameramicrofoon 0.47, heldere spraak 0.96.
MIN_CONFIDENCE = 0.65
EXPECTED_LANGUAGES = ("nl", "en")   # standaard; in config.json: expected_languages
MIN_WORDS_OTHER_LANGUAGE = 10       # minder woorden in een onverwachte taal: waarschijnlijk muziek die Whisper verkeerd hoort

# Bekende verzinsels van Whisper bij stilte of muziek. Vergelijking is hoofdletterongevoelig.
# Per taal, want een Nederlands patroon mag geen Engelse zin wegfilteren.
HALLUCINATION_PATTERNS = {
    "nl": [
        r"tv gelderland",
        r"ondertiteld door",
        r"ondertiteling[: ]",
        r"ondertitels[: ]",
        r"^\W*intromuziek\W*$",
        r"^\W*muziek\W*$",
        r"^\W*applaus\W*$",
        r"bedankt voor het kijken",
    ],
    "en": [
        r"thanks? (you )?for watching",
        r"subtitles by",
        r"amara\.org",
        r"^\W*music\W*$",
        r"^\W*applause\W*$",
    ],
}


def split_sentences(words):
    """Knipt de woorden van een Whisper-regel in zinnen."""
    sentences, current = [], []
    for index, word in enumerate(words):
        current.append(word)
        following = words[index + 1]["word"] if index + 1 < len(words) else " "
        # Alleen knippen als het volgende stuk een nieuw woord is ("ad" + ".nl" blijft heel).
        if word["word"].strip().endswith(SENTENCE_END) and following.startswith(" "):
            sentences.append(current)
            current = []
    if current:
        sentences.append(current)
    return sentences


def drop_repeats(pieces):
    """Haalt herhalingslussen weg: dezelfde zin vier keer of vaker achter elkaar.

    Whisper blijft soms in een zin hangen, vooral bij muziek of een filmpje dat
    op de achtergrond speelt. Van zo'n reeks blijft alleen de eerste staan.
    """
    def text(piece):
        return "".join(word["word"] for word in piece[1]).strip().lower()

    result, removed, index = [], 0, 0
    while index < len(pieces):
        end = index
        while end + 1 < len(pieces) and text(pieces[end + 1]) == text(pieces[index]):
            end += 1
        run = end - index + 1
        if run >= REPEAT_LIMIT:
            result.append(pieces[index])
            removed += run - 1
        else:
            result.extend(pieces[index:end + 1])
        index = end + 1
    return result, removed


def is_hallucination(segment, turns=None, language="nl"):
    """Geeft de reden terug als dit segment verzonnen lijkt, anders None.

    `turns` is de uitkomst van het sprekermodel, of None als dat niet gedraaid heeft.
    Een laag eerste woord alleen is niet genoeg: dat komt ook voor bij echte zinnen
    na een pauze. Pas samen met "hier is nauwelijks spraak" telt het.
    """
    words = segment.get("words") or []
    if not words:
        return "geen woorden"
    text = segment.get("text", "").strip().lower()
    for pattern in HALLUCINATION_PATTERNS.get(language, []):
        if re.search(pattern, text):
            return f"zwarte lijst: {pattern}"
    probability = words[0].get("probability", 1.0)
    if probability < FIRST_WORD_MIN_PROBABILITY and turns is not None:
        share = speech_share(words[0]["start"], words[-1]["end"], turns)
        if share < MIN_SPEECH_SHARE:
            return f"eerste woord kans {probability:.3f}, {share:.0%} spraak"
    return None


def expected_languages():
    """De talen die in dit archief te verwachten zijn (config.json: expected_languages)."""
    value = load_config().get("expected_languages")
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return tuple(v.lower() for v in value)
    return EXPECTED_LANGUAGES
