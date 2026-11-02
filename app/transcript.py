"""Het eigen transcriptmodel van Local Transcribe: wat een transcript is, los van waar het vandaan komt
of naartoe gaat.

Alles werkt op dit model. Iconik is één manier om het op te slaan (to_iconik / from_iconik); later
komen er andere bij, zoals een bestand naast de media. De editor (Refine) leest en schrijft dit model.

Tijden zijn overal hele milliseconden vanaf het begin van de media, ook van de woorden (in Iconik zijn
woordtijden relatief aan het segment; de omzetting regelt dat). Alleen de standaardbibliotheek.

Bestandsformaat: JSON met "format": "local-transcribe" en "version": 1 (zie to_dict / from_dict).
"""
from dataclasses import dataclass, field

FORMAT = "local-transcribe"
VERSION = 1


@dataclass
class Word:
    text: str
    start: int          # ms vanaf het begin van de media
    end: int
    score: float = 0.0  # zekerheid van de herkenning, 0 tot 1


@dataclass
class Segment:
    """Een blok tekst van één spreker: een regel in Iconik, een blok in de editor."""
    start: int
    end: int
    speaker: str        # id uit Transcript.speakers
    words: list = field(default_factory=list)
    text: str = ""      # leeg: de woorden met spaties ertussen

    def display_text(self):
        return self.text or " ".join(w.text for w in self.words)


@dataclass
class Speaker:
    id: str             # vast binnen een transcript ("1", "2", ...)
    name: str           # wat mensen zien ("Spreker 1", of een naam die iemand invulde)


@dataclass
class Track:
    """Een audiospoor van de media, zoals de sporenanalyse het zag."""
    number: int         # 1, 2, ... zoals ffprobe ze telt (kanalen los)
    label: str          # wat erop staat volgens de analyse ("zender", "camera", "tijdcode", "stilte", ...)
    used: bool          # meegenomen in de audio voor Whisper
    db: float = None    # gemiddeld niveau


@dataclass
class Transcript:
    language: str = ""
    speakers: list = field(default_factory=list)
    segments: list = field(default_factory=list)
    tracks: list = field(default_factory=list)
    engine: dict = field(default_factory=dict)   # {"name", "model", "revision", "version"}
    source: dict = field(default_factory=dict)   # waar het bij hoort, bv. {"connector": "iconik", "asset_id", "version_id", "title"}

    def speaker_name(self, speaker_id):
        for speaker in self.speakers:
            if speaker.id == speaker_id:
                return speaker.name
        return speaker_id


# --- van de herkenning naar het model ---

def words_from_whisper(words):
    """Whisper-woorden (seconden, met een spatie voor elk nieuw woord) naar Word's in ms.

    Ontbreekt de spatie, dan hoort dit stuk bij het vorige woord ("woke" + "-predikant"). Lege stukken
    vallen weg. Dit zijn precies de regels die to_iconik_segment in transcribe.py altijd volgde."""
    result = []
    for word in words:
        raw = word["word"]
        value = raw.strip()
        if not value:
            continue
        start = round(word["start"] * 1000)
        end = max(round(word["end"] * 1000), start)
        score = round(float(word.get("probability", 0.0)), 2)
        if result and not raw.startswith(" "):
            previous = result[-1]
            previous.text += value
            previous.end = max(previous.end, end)
            previous.score = min(previous.score, score)
            continue
        result.append(Word(value, start, end, score))
    return result


def from_pieces(pieces, language, speaker_label, engine=None, tracks=None, source=None):
    """Uit de uitkomst van de herkenning: `pieces` is een lijst van (sprekernummer vanaf 0, Whisper-woorden).
    `speaker_label` is het woord voor een spreker in de taal van de app ("Spreker" of "Speaker").

    Het segment loopt van het eerste tot het laatste Whisper-woord, ook als een woord leeg bleek: zo is het
    hetzelfde als wat er tot nu toe naar Iconik ging."""
    numbers = sorted({number for number, _ in pieces})
    transcript = Transcript(
        language=language,
        speakers=[Speaker(str(n + 1), f"{speaker_label} {n + 1}") for n in numbers],
        engine=dict(engine or {}), tracks=list(tracks or []), source=dict(source or {}))
    for number, words in pieces:
        if not words:
            continue
        start = round(words[0]["start"] * 1000)
        end = max(round(words[-1]["end"] * 1000), start)
        transcript.segments.append(Segment(start, end, str(number + 1), words_from_whisper(words)))
    return transcript


def tracks_from_infos(infos):
    """De sporen zoals de sporenanalyse ze aan het venster meldt (track_info in transcribe.py)."""
    return [Track(int(i["n"]), str(i.get("note") or ""), bool(i.get("used")), i.get("db")) for i in infos or []]


# --- Iconik ---

def _speaker_index(transcript):
    """Iconik telt sprekers als getallen vanaf 0, in de volgorde van transcript.speakers."""
    return {speaker.id: index for index, speaker in enumerate(transcript.speakers)}


def to_iconik(transcript, transcription_id, version_id):
    """(properties, segmenten) voor de transcription-API van Iconik. Zonder de velden die alleen de app zet
    (engine_info): die vult de schrijvende kant aan."""
    index = _speaker_index(transcript)
    properties = {
        "language": transcript.language,
        "speaker_labels": {str(index[s.id]): s.name for s in transcript.speakers},
    }
    segments = []
    for segment in transcript.segments:
        words = [{"value": w.text,
                  "start_ms": max(w.start - segment.start, 0),
                  "end_ms": max(w.end - segment.start, max(w.start - segment.start, 0)),
                  "score": w.score} for w in segment.words]
        segments.append({
            "segment_type": "TRANSCRIPTION",
            "transcription_id": transcription_id,
            "version_id": version_id,
            "time_start_milliseconds": segment.start,
            "time_end_milliseconds": segment.end,
            "segment_text": segment.display_text() if segment.text else " ".join(w["value"] for w in words),
            "transcription": {"speaker": index.get(segment.speaker, 0), "words": words},
        })
    return properties, segments


def from_iconik(properties, segments, source=None):
    """Een transcript uit Iconik terug naar het model, bijvoorbeeld om het in de editor te openen.
    Velden die Iconik zelf toevoegt (id's, datums) worden genegeerd; onbekende sprekers krijgen een naam."""
    labels = {str(k): v for k, v in ((properties or {}).get("speaker_labels") or {}).items()}
    rows = sorted(segments or [], key=lambda s: (s.get("time_start_milliseconds") or 0, s.get("time_end_milliseconds") or 0))
    used = sorted({int((s.get("transcription") or {}).get("speaker") or 0) for s in rows} | {int(k) for k in labels if k.isdigit()})
    transcript = Transcript(
        language=(properties or {}).get("language") or "",
        speakers=[Speaker(str(n + 1), labels.get(str(n), f"Speaker {n + 1}")) for n in used],
        engine=dict((properties or {}).get("engine_info") or {}), source=dict(source or {}))
    for row in rows:
        start = int(row.get("time_start_milliseconds") or 0)
        end = max(int(row.get("time_end_milliseconds") or start), start)
        info = row.get("transcription") or {}
        words = [Word(str(w.get("value", "")), start + int(w.get("start_ms") or 0),
                      start + int(w.get("end_ms") or w.get("start_ms") or 0), float(w.get("score") or 0.0))
                 for w in info.get("words") or []]
        text = row.get("segment_text") or ""
        segment = Segment(start, end, str(int(info.get("speaker") or 0) + 1), words)
        if text != " ".join(w.text for w in words):
            segment.text = text    # tekst die iemand anders aanpaste dan de woorden: die gaat voor
        transcript.segments.append(segment)
    return transcript


# --- bestand ---

def to_dict(transcript):
    return {
        "format": FORMAT, "version": VERSION,
        "language": transcript.language,
        "engine": transcript.engine, "source": transcript.source,
        "speakers": [{"id": s.id, "name": s.name} for s in transcript.speakers],
        "tracks": [{"number": t.number, "label": t.label, "used": t.used, "db": t.db} for t in transcript.tracks],
        "segments": [{"start": s.start, "end": s.end, "speaker": s.speaker,
                      **({"text": s.text} if s.text else {}),
                      "words": [[w.text, w.start, w.end, w.score] for w in s.words]} for s in transcript.segments],
    }


def from_dict(data):
    if data.get("format") != FORMAT:
        raise ValueError("Geen Local Transcribe-transcript")
    if int(data.get("version") or 0) > VERSION:
        raise ValueError(f"Transcript in een nieuwere versie van het formaat ({data.get('version')})")
    return Transcript(
        language=data.get("language") or "",
        engine=dict(data.get("engine") or {}), source=dict(data.get("source") or {}),
        speakers=[Speaker(str(s["id"]), str(s["name"])) for s in data.get("speakers") or []],
        tracks=[Track(int(t["number"]), str(t.get("label") or ""), bool(t.get("used")), t.get("db")) for t in data.get("tracks") or []],
        segments=[Segment(int(s["start"]), int(s["end"]), str(s["speaker"]),
                          [Word(str(w[0]), int(w[1]), int(w[2]), float(w[3])) for w in s.get("words") or []],
                          str(s.get("text") or "")) for s in data.get("segments") or []],
    )
