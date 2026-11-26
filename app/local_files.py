"""Losse bestanden op deze Mac als bron (lokale modus, stap 6), in de vaste vorm van connectors.py. Geen koppeling
maar een deel van de app zelf: het enige "exemplaar" heet local, en een item is local:<vingerafdruk>.

  status(i)                            altijd verbonden
  open(i, key, push, replace, allow_refined)
                                       het bestand en wat er al naast staat; weigert (Problem) wat niet mag
  find_media(i, info, ...)             het mediabestand, als lijst met één pad
  write(i, info, doc, engine_info, from_queue, replace)
                                       het transcript als clip.transcript.json; {"id": pad, "segments"}
  read(i, info, which)                 het transcript naast de media ("own" of "refined") als dict, of None

Wie een item is (gekozen 26 november, na een meting op de ISG): een vingerafdruk van de inhoud, niet het pad.
De grootte plus de eerste, middelste en laatste MiB, met sha256. Op de ISG kost dat ongeveer 63 ms per clip; het
hele bestand lezen kost bij 87 MiB/s minuten voor een grote MXF. De ISG komt via SMB binnen zonder vaste
volume-ID, dus een pad zou na hernoemen, verplaatsen of een volume dat terugkomt als Archief-1 een nieuwe clip
zijn. Een kopie elders heeft dezelfde vingerafdruk en krijgt hetzelfde transcript zonder opnieuw te transcriberen;
een opnieuw geëxporteerde clip is een nieuwe.

Waar een bestand staat, onthoudt de app in local-files.json (base.LOCAL_INDEX): per vingerafdruk de paden die
iemand toevoegde, en welke nog een transcript moeten krijgen.

Waar het transcript komt (files.transcript_place): naast de media, of in één map met de mappenstructuur van de
bron en de naam van het volume ervoor. De naam is clip.transcript.json; heeft een ander bestand met dezelfde
basisnaam (clip.wav naast clip.mxf) die al, dan wordt het clip.wav.transcript.json.

Wat de app nooit doet: een transcript overschrijven dat niet van deze app is (een ander formaat, of iets wat
niet te lezen is), of zonder Refine een correctie uit Refine. Wat hij schreef, staat met een hash in sidecars.json.
Het bestand naast de media heeft alleen de bestandsnaam van de media, geen pad: het kan gedeeld worden.
"""
import errno
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

import base
import items
import settings
import subtitles
import system
import transcript as tmodel

NAME = items.LOCAL
TITLE = ("Bestanden", "Files")
CAPABILITIES = ("source", "write_back", "read_back")
SETTINGS = ()          # de instellingen staan in de app zelf: de groep files in settings.py

SUFFIX = ".transcript.json"
PART = 1024 * 1024
KEY_LENGTH = 32        # tekens hex van de sha256: 128 bits

host = None
_lock = threading.RLock()   # local-files.json


def start(the_host):
    global host
    host = the_host


def L(dutch, english):
    return base.L(dutch, english)


# --- wie een bestand is ---

def fingerprint(path):
    """De vingerafdruk van een bestand: grootte plus de eerste, middelste en laatste MiB. Gooit OSError."""
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.sha256(f"local-transcribe 1\0{size}\0".encode())
    with path.open("rb") as f:   # niet open(): dat is hier de functie van de vaste vorm
        if size <= 3 * PART:
            digest.update(f.read())
        else:
            digest.update(f.read(PART))
            f.seek(size // 2 - PART // 2)
            digest.update(f.read(PART))
            f.seek(size - PART)
            digest.update(f.read(PART))
    return digest.hexdigest()[:KEY_LENGTH]


AUDIO_EXTENSIONS = {".wav", ".bwf", ".aif", ".aiff", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}


def is_media(path):
    return Path(path).suffix.lower() in base.MEDIA_EXTENSIONS


def is_audio(path):
    return Path(path).suffix.lower() in AUDIO_EXTENSIONS


def media_in(folder, subfolders=True, audio=True, limit=100000):
    """De video- en audiobestanden in een map (en de submappen), als paden. Verborgen bestanden en mappen,
    macOS-pakketten (.app, .fcpbundle, ...) en de ._-bestanden van een netwerkvolume tellen niet. Zonder `audio`
    alleen video. Een map die niet te lezen is, slaat hij over."""
    found = []
    folder = Path(folder)

    def walk(directory):
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
        except OSError:
            return
        for entry in entries:
            if len(found) >= limit or entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if subfolders and "." not in entry.name[1:]:
                        walk(entry.path)
                elif entry.is_file() and is_media(entry.name) and (audio or not is_audio(entry.name)):
                    found.append(Path(entry.path))
            except OSError:
                continue
    walk(folder)
    return found


def has_transcript(media):
    """Staat er al een transcript (van wie dan ook) waar dat van dit bestand zou komen? Zonder de vingerafdruk te
    lezen: voor een gevolgde map, die zo elke ronde snel overslaat wat al klaar is."""
    media = Path(media)
    try:
        folder = directory_for(media)
    except base.Problem:
        return False
    return (folder / (media.stem + SUFFIX)).exists() or (folder / (media.name + SUFFIX)).exists()


# --- local-files.json ---

def _load():
    try:
        data = json.loads(Path(base.LOCAL_INDEX).read_text())
    except (OSError, ValueError):
        return {}
    found = data.get("items") if isinstance(data, dict) else None
    return found if isinstance(found, dict) else {}


def _save(found):
    target = Path(base.LOCAL_INDEX)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps({"version": 1, "items": found}, ensure_ascii=False))
    os.replace(temp, target)


def _entry(found, key):
    entry = found.get(key)
    if not isinstance(entry, dict):
        entry = found[key] = {}
    for field in ("paths", "pending"):
        if not isinstance(entry.get(field), list):
            entry[field] = []
    return entry


def remember(path):
    """Neemt een bestand op dat iemand toevoegde: (item, titel). Het pad moet een transcript krijgen. Gooit
    OSError als het bestand niet te lezen is."""
    path = Path(path).absolute()
    key = fingerprint(path)
    with _lock:
        found = _load()
        entry = _entry(found, key)
        text = str(path)
        entry["paths"] = [text] + [p for p in entry["paths"] if p != text][:19]
        entry["pending"] = [text] + [p for p in entry["pending"] if p != text]
        entry["title"] = path.name
        entry["seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save(found)
    return items.make(NAME, key), path.name


def known(key):
    """Wat de app van een vingerafdruk weet: {"paths", "pending", "title"} (leeg als niets)."""
    with _lock:
        entry = _load().get(key)
    return dict(entry) if isinstance(entry, dict) else {}


def _done(key, paths):
    with _lock:
        found = _load()
        if key in found:
            entry = _entry(found, key)
            entry["pending"] = [p for p in entry["pending"] if p not in paths]
            _save(found)


def _stamp(path):
    st = Path(path).stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def skip(path):
    """Een gevolgde map probeert dit bestand niet opnieuw (mislukt of overgeslagen), tot het verandert."""
    try:
        stamp = _stamp(path)
    except OSError:
        return
    with _lock:
        found = _load()
        skipped = found.setdefault("~skip", {})   # pad -> "grootte:mtime"; ~ botst nooit met een vingerafdruk
        if isinstance(skipped, dict):
            skipped[str(path)] = stamp
            _save(found)


def skipped(path):
    with _lock:
        stamp = (_load().get("~skip") or {}).get(str(path))
    try:
        return stamp is not None and stamp == _stamp(path)
    except OSError:
        return False


def remove_written(key, others=()):
    """Ook het transcript naast de media weghalen (stap 6f, het vinkje bij Ook van deze Mac verwijderen): voor elke
    plek waar de app dit bestand zag het transcript, plus `others` (de ondertitelbestanden van Refine of de
    automatische ronde). Alleen wat de app zelf schreef en sindsdien niet veranderde (de hash in sidecars.json), en
    alleen naar de prullenmand: lukt dat niet, dan blijft het staan. Een bestand in een gevolgde map wordt daarna niet
    vanzelf opnieuw gedaan. Geeft {"trashed": [namen], "kept": [[naam, reden]], "wipe": [paden]}: wipe zijn de
    bestanden die alleen bleven staan omdat de schijf geen prullenmand heeft (de knop Toch wissen, wipe())."""
    candidates = []
    for text in known(key).get("paths") or []:
        media = Path(text)
        directory = directory_for(media)
        candidates += [directory / (media.stem + SUFFIX), directory / (media.name + SUFFIX)]
    candidates += [Path(p) for p in others]
    trashed, kept, no_trash = [], [], []
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            continue
        if not subtitles.unchanged(path, base.SIDECARS_FILE):
            kept.append([path.name, L("niet door de app geschreven, of daarna veranderd", "not written by the app, or changed since")])
            continue
        reason = system.trash(path)
        if reason == system.NO_TRASH:
            no_trash.append(str(path))
            reason = L("deze schijf heeft geen prullenmand, haal het zelf weg als het niet meer nodig is",
                       "this disk has no Trash, remove it yourself if it is no longer needed")
        if reason:
            kept.append([path.name, reason])
            continue
        subtitles.forget([path], base.SIDECARS_FILE)
        trashed.append(path.name)
    for text in known(key).get("paths") or []:
        if scope_of(text) in settings.get("files.watched"):
            skip(text)
    return {"trashed": trashed, "kept": kept, "wipe": no_trash}


def wipe(paths):
    """Toch wissen (stap 6f): definitief, voor bestanden die naar de prullenmand moesten op een schijf zonder. Pas na
    een tweede bevestiging in het venster, en opnieuw alleen wat de app schreef en sindsdien niet veranderde.
    Geeft {"wiped": [namen], "kept": [[naam, reden]]}."""
    wiped, kept = [], []
    for text in paths:
        path = Path(text)
        if not path.is_file():
            continue
        if not subtitles.unchanged(path, base.SIDECARS_FILE):
            kept.append([path.name, L("daarna veranderd", "changed since")])
            continue
        try:
            path.unlink()
        except OSError as error:
            kept.append([path.name, error.strerror or str(error)])
            continue
        subtitles.forget([path], base.SIDECARS_FILE)
        wiped.append(path.name)
    return {"wiped": wiped, "kept": kept}


def forget(key):
    """Haalt een vingerafdruk uit local-files.json (Ook van deze Mac weghalen). True als hij er was."""
    with _lock:
        found = _load()
        if found.pop(key, None) is None:
            return False
        _save(found)
        return True


def _where_now(path):
    """Het pad, of hetzelfde bestand op een volume dat opnieuw gekoppeld is (Opslag-1). None als het weg is."""
    path = Path(path)
    try:
        if path.is_file():
            return path
    except OSError:
        return None
    moved = system.find_moved_mount(path.parent, path.name)
    return moved / path.name if moved is not None else None


def _matching(key, paths):
    """De paden (nu bereikbaar) waar dit bestand met deze vingerafdruk staat, in volgorde."""
    out = []
    for text in paths:
        path = _where_now(text)
        if path is None or str(path) in out:
            continue
        try:
            if fingerprint(path) == key:
                out.append(str(path))
        except OSError:
            continue
    return out


def media_path(key):
    """Een bereikbaar pad van dit bestand. Gooit Problem (overgeslagen) als het weg of veranderd is."""
    entry = known(key)
    paths = list(dict.fromkeys(entry.get("pending", []) + entry.get("paths", [])))
    found = _matching(key, paths)
    if found:
        return Path(found[0])
    if any(_where_now(p) for p in paths):
        base.fail(L("Het bestand is veranderd sinds het werd toegevoegd. Voeg het opnieuw toe.",
                    "The file changed since it was added. Add it again."), skip=True)
    base.fail(L("Het bestand is niet (meer) te vinden op deze Mac.", "The file cannot be found on this Mac (any more)."),
              skip=True)


# --- waar het transcript komt ---

def scope_of(media):
    """Waar een gekozen andere plek voor geldt (stap 6d): de gevolgde map waar het bestand in staat, anders het volume
    onder /Volumes (een kaart, een schijf, de opslag), anders de map van het bestand zelf. Nooit de hele opstartschijf:
    dan gingen ook alle andere bestanden daarheen."""
    media = Path(media)
    inside = [Path(p) for p in settings.get("files.watched") if isinstance(p, str)]
    inside = [p for p in inside if p in media.parents]
    if inside:
        return str(max(inside, key=lambda p: len(p.parts)))
    parts = media.parts
    if len(parts) > 3 and parts[1] == "Volumes":
        return str(Path(*parts[:3]))
    return str(media.parent)


def elsewhere():
    """De gekozen andere plekken: {bereik: map}."""
    found = settings.get("files.elsewhere")
    return {k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, str) and v}


def _elsewhere_root(media):
    """(bereik, map) als er voor dit bestand een andere plek gekozen is, anders None. Het langste bereik telt."""
    media, chosen = Path(media), elsewhere()
    found = [scope for scope in chosen if Path(scope) in media.parents]
    if not found:
        return None
    scope = max(found, key=lambda s: len(Path(s).parts))
    return scope, chosen[scope]


WRITE_REASONS = {"readonly": ("alleen-lezen (een vergrendelde kaart of een alleen-lezen schijf)",
                              "read-only (a locked card or a read-only disk)"),
                 "denied": ("geen schrijfrechten", "no permission to write")}


def reason_of(error):
    """De reden achter een schrijffout, als sleutel: readonly, denied of de tekst van het systeem."""
    if getattr(error, "errno", None) == errno.EROFS:
        return "readonly"
    if getattr(error, "errno", None) in (errno.EACCES, errno.EPERM):
        return "denied"
    return getattr(error, "strerror", None) or type(error).__name__


def reason_text(reason):
    return L(*WRITE_REASONS[reason]) if reason in WRITE_REASONS else reason


def can_write(directory):
    """Kan de app in deze map schrijven? None als het kan, anders de reden (reason_of). Probeert het echt, met een
    verborgen proefbestand dat meteen weer weggaat: gemeten 26 november gaven een alleen-lezen schijf EROFS en een map
    zonder rechten EACCES; bij een netwerkshare zegt os.access niet altijd de waarheid."""
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return reason_of(error)
    probe = directory / f".lt-schrijftest-{secrets.token_hex(4)}"
    try:
        probe.write_bytes(b"")
    except OSError as error:
        return reason_of(error)
    try:
        probe.unlink()
    except OSError:
        pass
    return None


def cannot_write_here(reason):
    """De melding (Problem, place) als er niet geschreven kan worden: de opdracht wacht op een andere plek."""
    base.fail(L(f"Kan hier niet schrijven: {reason_text(reason)}.", f"Cannot write here: {reason_text(reason)}."),
              place=True)


def _folder():
    """De map voor In één map, of None voor naast de media."""
    if settings.get("files.transcript_place") != "folder":
        return None
    folder = settings.get("files.transcript_folder").strip()
    if not folder:
        base.fail(L("Er is nog geen map voor de transcripten gekozen (Instellingen, Bestanden).",
                    "No folder for transcripts has been chosen yet (Settings, Files)."))
    return Path(folder)


def place_root(scope, chosen):
    """De map die een gekozen andere plek voor een bereik wordt: daarin een map met de naam van het bereik. Valt die
    samen met het bereik zelf (iemand koos de map erboven), ligt hij erin of het bereik erin, dan een eigen map ernaast:
    "<naam> transcripten". Gezien 26 november: Bureaublad kiezen voor Bureaublad/GEEN_RECHTEN gaf de map zelf terug."""
    scope, chosen = Path(scope), Path(chosen)
    root = chosen / scope.name
    if root == scope or scope in root.parents or root in scope.parents:
        root = chosen / (scope.name + L(" transcripten", " transcripts"))
    return root


def directory_for(media):
    """De map waar het transcript van dit bestand komt."""
    media = Path(media)
    chosen = _elsewhere_root(media)
    if chosen is not None:   # een andere plek, gekozen omdat het hier niet kon (stap 6d)
        scope, root = chosen
        return Path(root) / media.parent.relative_to(scope)
    root = _folder()
    if root is None:
        return media.parent
    volume, inside = system.volume_place(media)
    return root / volume / inside.parent


def subtitle_base(media, key):
    """Waar de automatische ondertitels komen (stap 6e), als pad zonder formaat: subtitles.write_files maakt er
    clip.srt of clip.vtt van. De basisnaam volgt die van het transcript (clip.mxf.srt als het transcript
    clip.mxf.transcript.json heet), zodat twee bestanden met dezelfde basisnaam ook hier niet botsen.
    Plaats: bij het transcript (ook een andere plek uit stap 6d), naast de media, of in één map."""
    media = Path(media)
    target, _ = target_for(media, key)
    stem = target.name[:-len(SUFFIX)]
    place = settings.get("files.subtitle_place")
    folder = settings.get("files.subtitle_folder").strip()
    if place == "next":
        directory = media.parent
    elif place == "folder" and folder:
        volume, inside = system.volume_place(media)
        directory = Path(folder) / volume / inside.parent
    else:   # bij het transcript; In één map zonder map valt hier ook op terug
        directory = target.parent
    return directory / (stem + ".ondertitel")


def classify(path, key):
    """Wat er op een plek staat: "none", "mine" (van deze app, dit bestand), "mine-refined" (idem, gecorrigeerd
    in Refine), "other" (van deze app, een ander bestand met dezelfde basisnaam) of "foreign" (iets anders)."""
    path = Path(path)
    try:
        if not path.exists():
            return "none"
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return "foreign"
    if not isinstance(data, dict) or data.get("format") != tmodel.FORMAT:
        return "foreign"
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    if source.get("fingerprint") != key:
        return "other"
    refined = (data.get("engine") or {}).get("name") == base.REFINED_ENGINE or bool(source.get("refined"))
    return "mine-refined" if refined else "mine"


def target_for(media, key):
    """(pad, wat er staat) voor het transcript van dit bestand. Volgorde: clip.mxf.transcript.json als de app die
    eerder voor dit bestand schreef; anders clip.transcript.json als die vrij of van dit bestand is; anders,
    als die bij een ander bestand met dezelfde basisnaam hoort, clip.mxf.transcript.json."""
    media = Path(media)
    folder = directory_for(media)
    short, full = folder / (media.stem + SUFFIX), folder / (media.name + SUFFIX)
    state = classify(full, key)
    if state.startswith("mine"):
        return full, state
    short_state = classify(short, key)
    if short_state != "other":
        return short, short_state
    return full, state


# --- de vaste vorm ---

def status(instance):
    return {"connected": True}


def _kinds(state):
    return {"own": ["file"] if state == "mine" else [], "refined": ["file"] if state == "mine-refined" else [],
            "foreign": ["file"] if state in ("foreign", "other") else []}


def open(instance, key, push=True, replace=True, allow_refined=False):
    media = media_path(key)
    item = items.make(NAME, key)
    target, state = target_for(media, key)
    title = media.name
    base.print(f"Bestand: {title}")
    if state in ("foreign", "other") and push:
        base.fail(L(f"Naast dit bestand staat al {target.name}, niet van deze app. Dat wordt nooit overschreven.",
                    f"{target.name} is already there and does not come from this app. It is never overwritten."),
                  skip=True)
    if state == "mine-refined" and push and not allow_refined:
        base.fail(L("Dit bestand heeft een in Refine gecorrigeerd transcript. Dat wordt niet overschreven; "
                    "zet in Refine eerst het origineel terug als je opnieuw wilt transcriberen.",
                    "This file has a transcript corrected in Refine. It is never overwritten; "
                    "put the original back in Refine first if you want to transcribe again."), skip=True)
    if state == "mine" and push and not replace:
        base.fail(L("Dit bestand heeft al een transcript van deze app.", "This file already has a transcript from this app."))
    if push:
        problem = can_write(target.parent)
        if problem:
            cannot_write_here(problem)   # vooraf: niet eerst een half uur transcriberen
    cached = items.path(base.TRANSCRIPTS_DIR, item, ".json").is_file()
    return {"connector": NAME, "key": key, "item": item, "asset_id": "", "version_id": "", "title": title,
            "asset": {"title": title}, "media": str(media), "target": str(target), "state": state,
            # Staat er nog niets en heeft deze Mac het transcript al (een kopie elders, of een nieuwe plek):
            # alleen dat neerzetten, niet opnieuw transcriberen.
            "write_existing": bool(push and state == "none" and cached and not allow_refined),
            **_kinds(state)}


def find_media(instance, info, mount=None, ask_mount=False, interactive=False):
    return [info["media"]]


def file_dict(doc, key, media_name, engine_info=None):
    """Het transcript zoals het naast de media komt: het eigen model, met alleen de bestandsnaam van de media."""
    data = tmodel.to_dict(doc)
    engine = dict(data.get("engine") or {})
    if engine_info:
        engine.update({k: engine_info[k] for k in ("name", "version") if engine_info.get(k)})
    refined = engine.get("name") == base.REFINED_ENGINE or bool(doc.source.get("refined"))
    if refined:
        engine["name"] = base.REFINED_ENGINE
    data["engine"] = engine
    data["source"] = {"connector": NAME, "fingerprint": key, "media": media_name,
                      "title": doc.source.get("title") or media_name, **({"refined": True} if refined else {})}
    return data


def _write_one(target, data):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    temp = target.with_name(f".{target.name}.tmp")
    try:
        temp.write_bytes(raw)
        os.replace(temp, target)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    subtitles.record(target, raw, base.SIDECARS_FILE)


def cannot_write(error, target):
    """Tijdens het werk ging schrijven mis (de rechten veranderden, de kaart ging op slot): de opdracht wacht op een
    andere plek. Het transcript staat dan al op deze Mac (pipeline), dus een plek kiezen schrijft het alleen nog weg."""
    cannot_write_here(reason_of(error))


def write(instance, info, doc, engine_info, from_queue=False, replace="own"):
    """Zet het transcript (als dict) naast de media of in de gekozen map. `replace`: "own" vervangt het eerdere
    transcript van de app, "all" (Refine) ook een correctie. Daarna ook naast kopieën die nog wachten."""
    key, media = info["key"], Path(info["media"])
    model = tmodel.from_dict(doc)
    allowed = ("none", "mine", "mine-refined") if replace == "all" else ("none", "mine")
    target, state = target_for(media, key)
    if state not in allowed:
        open(instance, key, push=True, replace=True, allow_refined=replace == "all")   # meldt waarom
        base.fail(L("Het transcript kon niet worden weggeschreven.", "The transcript could not be written."))
    data = file_dict(model, key, media.name, engine_info)
    try:
        _write_one(target, data)
    except OSError as error:
        cannot_write(error, target)
    base.print(f"Transcript naast de media: {target.name}" if target.parent == media.parent
               else "Transcript in de map voor transcripten.")
    written = [str(media)]
    copies = 0
    for other in _matching(key, [p for p in known(key).get("pending", []) if p != str(media)]):
        other = Path(other)
        try:
            spot, spot_state = target_for(other, key)
            if spot_state in ("none", "mine"):
                _write_one(spot, file_dict(model, key, other.name, engine_info))
                copies += 1
            written.append(str(other))
        except (OSError, base.Problem) as error:
            base.print(f"Kopie niet bijgeschreven ({type(error).__name__}).")
    if copies:
        base.print(f"Ook naast {copies} kopie(ën) gezet.")
    _done(key, written + [p for p in known(key).get("pending", []) if _where_now(p) == media])
    return {"id": str(target), "segments": sum(1 for s in model.segments if s.words or s.text.strip()),
            "copies": copies}


def read(instance, info, which="own"):
    target = Path(info["target"])
    state = classify(target, info["key"])
    if state != ("mine-refined" if which == "refined" else "mine"):
        return None
    try:
        data = json.loads(target.read_text())
    except (OSError, ValueError):
        return None
    source = dict(data.get("source") or {})
    source.update(item=items.make(NAME, info["key"]), media=info["media"], transcription_id=str(target))
    if which == "refined":
        source["refined"] = True
    data["source"] = source
    return data
