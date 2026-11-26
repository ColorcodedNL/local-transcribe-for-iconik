# The Local Transcribe transcript format

Local Transcribe writes every transcript in its own format, `local-transcribe`. For a local file it sits next to the media as `clip.transcript.json` (or in the folder chosen under Settings › Files). Other programs may read it; this page and the JSON Schema next to it are the contract.

- Schema: [`local-transcribe-v1.schema.json`](local-transcribe-v1.schema.json) (JSON Schema 2020-12)
- Examples: [`examples/interview.transcript.json`](examples/interview.transcript.json), as Local Transcribe writes it, and [`examples/interview.refined.transcript.json`](examples/interview.refined.transcript.json), the same transcript after a correction in Refine

## The file

A UTF-8 JSON object:

| Field | What it holds |
|---|---|
| `format` | Always `"local-transcribe"`. |
| `version` | The major version, now `1`. |
| `minor` | Optional, the minor version. Absent means `0`. |
| `language` | Language code as Whisper gives it (`nl`, `en`, ...), or empty. |
| `engine` | What made it. `name` is `mlx-whisper` for a transcription and `local-transcribe-refined` once someone corrected it in Refine; `model`, `revision` and `version` are informative. |
| `source` | Where it belongs. Next to the media: `connector` `"local"`, `fingerprint` and `media` (the file name, never a path), `title`, and `refined: true` after a correction. |
| `speakers` | `id` and `name` per speaker. Segments refer to the `id`. |
| `tracks` | The audio tracks as the track analysis saw them: `number`, `label` (free text for people, in the app's language), `used` (taken into the transcription) and `db`. |
| `segments` | Blocks of text of one speaker, in order of time: `start`, `end`, `speaker`, `words`, and `text` only when it differs from the words joined with spaces. |

Times are whole milliseconds from the start of the media, also for words. A word is an array `[text, start, end, score]`; `score` is the recognition confidence from 0 to 1. Punctuation belongs to the word it follows.

### The fingerprint

`source.fingerprint` identifies the media without its path: a sha256 over a short prefix with the file size, then the whole file when it is at most 3 MiB, otherwise the first MiB, the MiB around the middle and the last MiB; the first 32 hex characters of that. A file that is moved or renamed keeps its fingerprint; a re-export gets a new one. Local Transcribe only replaces a `clip.transcript.json` whose fingerprint matches the media next to it; any other file with that name is left alone.

```python
import hashlib, os

def fingerprint(path, part=1024 * 1024):
    size = os.path.getsize(path)
    digest = hashlib.sha256(f"local-transcribe 1\0{size}\0".encode())
    with open(path, "rb") as f:
        if size <= 3 * part:
            digest.update(f.read())
        else:
            digest.update(f.read(part))
            f.seek(size // 2 - part // 2)
            digest.update(f.read(part))
            f.seek(size - part)
            digest.update(f.read(part))
    return digest.hexdigest()[:32]
```

## The version rule

- **`version` is the major version.** It goes up only when a field changes meaning or disappears. A reader refuses a file with a higher major version than it knows, and says so; it never guesses.
- **`minor` only adds.** A new minor version only adds optional fields. A reader accepts any minor version of its major version and ignores fields it does not know. Files of version 1 written so far have no `minor` field.
- **Writers keep what they do not know.** A program that changes a file and writes it back keeps unknown fields where it can.

## Reading it

Python, standard library only:

```python
import json

with open("interview.transcript.json", encoding="utf-8") as f:
    data = json.load(f)
if data.get("format") != "local-transcribe" or data.get("version") != 1:
    raise ValueError("not a Local Transcribe transcript of version 1")
names = {s["id"]: s["name"] for s in data["speakers"]}
for segment in data["segments"]:
    text = segment.get("text") or " ".join(w[0] for w in segment["words"])
    print(f'{segment["start"] / 1000:8.2f}  {names[segment["speaker"]]}: {text}')
```

Subtitles (.srt, .vtt) are made from this format in Refine or with the setting under Settings › Files; they are a second layer and not part of the transcript file.
