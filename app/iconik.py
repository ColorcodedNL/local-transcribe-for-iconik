"""De Iconik-connector, in de vaste vorm van connectors.py. De app roept hem aan met connectors.call(exemplaar,
functie, ...), met alleen gewone gegevens erin en eruit:

  status(i)                            verbonden of niet, en de stand van de wachtrij (voor het venster)
  open(i, key, push, replace, allow_refined)
                                       het asset en zijn transcripten; weigert (Problem) wat niet mag
  find_media(i, info, mount, ask_mount, interactive)
                                       de mediabestanden op deze Mac, als lijst paden
  write(i, info, doc, engine_info, from_queue, replace)
                                       een transcript (transcript.to_dict) naar Iconik; {"id", "segments"}
  read(i, info, which)                 het eigen transcript ("own") of de correctie ("refined") als dict, of None
  poll(i, who, enabled, auto)          één asset uit de wachtrij aannemen: {"key", "attempt", "auto"} of None
  report(i, job, who)                  het resultaat van een opdracht uit de rij terugmelden
  beat(i, healthy)                     het laatste teken van de lopende opdracht verversen

De wachtrij zelf staat in iconik_queue.py; de verbinding (inloggen, de client, ISG) in iconik_api.py. Binnen deze
module doet IconikConnector het werk op een client; die client komt van client(exemplaar), en tests vervangen
die functie onder die naam.
"""
import base
import iconik_api
import transcript as tmodel

NAME = "iconik"
TITLE = ("Iconik", "Iconik")
CAPABILITIES = ("source", "write_back", "read_back", "queue", "auto_scan")
SETTINGS = iconik_api.SETTINGS
REINDEX_REALMS = ["TRANSCRIPTIONS", "SEGMENTS"]   # de namen die Iconik voor transcripten gebruikt

# Waaraan de app zijn eigen transcripten in Iconik herkent (engine_info.name). Op één plek, zodat een tweede
# engine of een ander platform zijn eigen werk nooit voor een transcript uit een andere bron aanziet.
# Whisper op MLX schrijft nog "mlx-whisper", zodat Macs met een oudere versie in dezelfde rij het blijven
# herkennen; een volgende engine schrijft PRODUCER, en die naam herkent deze versie nu al.
PRODUCER = "local-transcribe"
OWN_NAMES = (base.ENGINE_NAME, PRODUCER)
REFINED_NAMES = (base.REFINED_ENGINE,)
ALL_OWN_NAMES = OWN_NAMES + REFINED_NAMES   # alles wat de app zelf schreef


def engine_name(transcription):
    return (transcription.get("engine_info") or {}).get("name")


def kind(transcription):
    """"own" (een transcript van deze app), "refined" (gecorrigeerd in Refine) of "foreign" (andere bron)."""
    name = engine_name(transcription)
    if name in REFINED_NAMES:
        return "refined"
    if name in OWN_NAMES:
        return "own"
    return "foreign"


class IconikConnector:
    name = NAME
    own_names, refined_names, all_own_names = OWN_NAMES, REFINED_NAMES, ALL_OWN_NAMES

    def __init__(self, client):
        self.client = client   # iconik_api.Iconik, of iets met dezelfde get/post/delete

    # --- open ---

    def open(self, asset_id, push=True, replace=True, allow_refined=False):
        """Het asset en zijn transcripten. Weigert (fail) wat de app niet mag doen: nooit een transcript uit
        een andere bron overschrijven, zonder `replace` ook het eigen eerdere transcript niet, en een correctie
        uit Refine alleen als dat de bedoeling is (`allow_refined`: Refine zelf)."""
        L, fail, print_ = base.L, base.fail, base.print
        asset = self.client.get(f"assets/v1/assets/{asset_id}/")
        version_id = iconik_api.find_version(asset)
        print_(f"Asset:   {asset.get('title')}")
        existing = self.client.get(
            f"assets/v1/assets/{asset_id}/versions/{version_id}/transcriptions/properties/").get("objects", [])
        own = [t for t in existing if kind(t) == "own"]
        refined = [t for t in existing if kind(t) == "refined"]
        foreign = [t for t in existing if kind(t) == "foreign"]
        if foreign:
            message = f"Dit asset heeft {len(foreign)} transcript(en) uit een andere bron."
            if push:
                fail(L(message + " Die worden nooit overschreven.",
                       f"This asset has {len(foreign)} transcript(s) from another source. Those are never overwritten."), skip=True)
            print_(f"Let op: {message} Een proefrun kan, pushen niet.")
        if refined and push and not allow_refined:
            fail(L("Dit asset heeft een in Refine gecorrigeerd transcript. Dat wordt niet overschreven; "
                   "zet in Refine eerst het origineel terug als je opnieuw wilt transcriberen.",
                   "This asset has a transcript corrected in Refine. It is never overwritten; "
                   "put the original back in Refine first if you want to transcribe again."), skip=True)
        if own:
            message = f"Dit asset heeft al {len(own)} transcript(en) van dit script."
            if push and not replace:
                fail(message + " Gebruik --push --replace om te vervangen.")
            print_(f"Let op: {message}" + (" Wordt vervangen." if push else ""))
        return {"connector": NAME, "asset_id": asset_id, "version_id": version_id,
                "title": asset.get("title") or "", "asset": asset, "own": own, "refined": refined, "foreign": foreign}

    # --- media ---

    def find_media(self, info, mount_override=None, ask_mount=False, interactive=True):
        """De mediabestanden van het asset op deze Mac. Nu altijd één: de eerste kopie die hier te vinden is."""
        return [iconik_api.find_media_file(self.client, info["asset_id"], mount_override, ask_mount, interactive)]

    # --- schrijven ---

    def write(self, info, doc, engine_info, still_ours=None, replaces=OWN_NAMES):
        """Schrijft het transcript naar Iconik en geeft het id van het nieuwe transcript.

        Eerst het nieuwe helemaal neerzetten, pas daarna het oude weghalen. Mislukt het schrijven halverwege,
        dan blijft het oude staan en gaat het halve nieuwe weer weg. Daarna gaan ook eigen transcripten weg
        die een andere Mac intussen op dit asset zette, zodat er precies één van deze app overblijft.
        `replaces`: welke eigen transcripten (engine-namen) wijken; Refine vervangt ook een eerdere correctie (ALL_OWN_NAMES)."""
        L, fail, print_ = base.L, base.fail, base.print
        asset_id, version_id = info["asset_id"], info["version_id"]
        if still_ours is not None and not still_ours():
            fail(L("Een andere Mac heeft dit asset overgenomen; er is niets naar Iconik geschreven.",
                   "Another Mac took over this asset; nothing was written to Iconik."), skip=True)
        properties = dict(tmodel.to_iconik(doc, "", version_id)[0], engine_info=engine_info)
        transcriptions = f"assets/v1/assets/{asset_id}/versions/{version_id}/transcriptions/properties/"
        transcription_id = self.client.post(transcriptions, properties)["id"]
        segments = [s for s in tmodel.to_iconik(doc, transcription_id, version_id)[1] if s["segment_text"]]
        try:
            for offset in range(0, len(segments), iconik_api.BULK_LIMIT):
                batch = segments[offset:offset + iconik_api.BULK_LIMIT]
                self.client.post(f"assets/v1/assets/{asset_id}/segments/bulk/", {"objects": batch})
                print_(f"Gepusht: segment {offset + 1} tot en met {offset + len(batch)}")
        except BaseException:
            try:
                iconik_api.delete_transcription(self.client, asset_id, version_id, transcription_id)
                print_(f"Schrijven mislukte; het halve transcript {transcription_id} is weer weggehaald.")
            except Exception as error:
                print_(f"Schrijven mislukte en het halve transcript {transcription_id} weghalen ook: {error}")
            raise
        try:
            existing = self.client.get(transcriptions).get("objects", [])
        except base.Problem:
            existing = (info.get("own") or []) + (info.get("refined") or [])
        replaced = False
        for old in existing:
            if engine_name(old) in replaces and old["id"] != transcription_id:
                removed = iconik_api.delete_transcription(self.client, asset_id, version_id, old["id"])
                print_(f"Oud transcript {old['id']} verwijderd ({removed} segmenten).")
                replaced = True
        if replaced:
            self.reindex(info)
        return transcription_id, len(segments)

    # --- zoekindex ---

    def reindex(self, info):
        """Laat Iconik de transcripten van het asset opnieuw indexeren. Nodig na verwijderen: Iconik haalt een
        verwijderd transcript niet vanzelf uit de zoekindex, en het blijft dan vindbaar (getest 2 november).
        Lukt het niet (bijvoorbeeld zonder het recht can_reindex_assets), dan alleen een logregel."""
        try:
            self.client.post(f"assets/v1/assets/{info['asset_id']}/reindex/", {"realms": REINDEX_REALMS})
            base.print("Zoekindex van het asset wordt bijgewerkt.")
            return True
        except Exception as error:
            base.print(f"Zoekindex bijwerken lukte niet ({error}); oude woorden kunnen vindbaar blijven tot een herindexering.")
            return False

    # --- lezen ---

    def read(self, info, names=OWN_NAMES):
        """Het transcript van deze app op het asset, terug in het eigen model, of None als er geen is.
        `names`: welke eigen transcripten (een naam of een rij namen), bijvoorbeeld REFINED_NAMES voor de correctie."""
        wanted = (names,) if isinstance(names, str) else tuple(names)
        asset_id, version_id = info["asset_id"], info["version_id"]
        listing = self.client.get(
            f"assets/v1/assets/{asset_id}/versions/{version_id}/transcriptions/properties/").get("objects", [])
        matches = [t for t in listing if engine_name(t) in wanted]
        if not matches:
            return None
        properties = matches[-1]
        segments, page = [], 1
        while True:
            objects = self.client.get(
                f"assets/v1/assets/{asset_id}/segments/?segment_type=TRANSCRIPTION"
                f"&transcription_id={properties['id']}&per_page={iconik_api.PAGE_SIZE}&page={page}").get("objects", [])
            # Zelf nog eens filteren, zodat er nooit segmenten van een ander transcript meekomen.
            segments += [o for o in objects if o.get("transcription_id") == properties["id"]]
            if len(objects) < iconik_api.PAGE_SIZE:
                break
            page += 1
        return tmodel.from_iconik(properties, segments, source={
            "connector": NAME, "asset_id": asset_id, "version_id": version_id,
            "title": info.get("title") or "", "transcription_id": properties["id"]})


# --- de connector voor de app (connectors.py): gewone gegevens erin en eruit ---

host = None          # de gastheer (connectors.Host): instellingen, geheimen, taal


def start(the_host):
    global host
    host = the_host


def client(instance):
    """De verbinding met de Iconik van dit exemplaar, of None als er niet is ingelogd. Tests vervangen deze
    functie onder deze naam."""
    return iconik_api.make_client()


def _connector(instance):
    connection = client(instance)
    if connection is None:
        base.fail(base.L("Niet ingelogd bij Iconik.", "Not logged in to Iconik."))
    return IconikConnector(connection)


def status(instance):
    import iconik_queue
    return {"connected": client(instance) is not None, "queue": _plain_state(iconik_queue.state)}


def _plain_state(state):
    return {k: v for k, v in state.items() if v is None or isinstance(v, (bool, int, float, str, dict, list))}


def open(instance, key, push=True, replace=True, allow_refined=False):
    info = _connector(instance).open(key, push, replace, allow_refined)
    info["instance"] = instance
    return info


def find_media(instance, info, mount=None, ask_mount=False, interactive=False):
    return [str(path) for path in _connector(instance).find_media(info, mount, ask_mount, interactive)]


def write(instance, info, doc, engine_info, from_queue=False, replace="own"):
    """Schrijft het transcript (als dict) naar Iconik. `replace`: "own" vervangt het eerdere transcript van de app,
    "all" ook een correctie uit Refine. Met `from_queue` schrijft hij niets als een andere Mac het asset intussen
    heeft overgenomen."""
    import iconik_queue
    still_ours = (lambda: info["asset_id"] not in iconik_queue.lost) if from_queue else None
    replaces = ALL_OWN_NAMES if replace == "all" else OWN_NAMES
    transcription_id, written = _connector(instance).write(info, tmodel.from_dict(doc), engine_info, still_ours, replaces)
    return {"id": transcription_id, "segments": written}


def read(instance, info, which="own"):
    found = _connector(instance).read(info, REFINED_NAMES if which == "refined" else OWN_NAMES)
    return tmodel.to_dict(found) if found is not None else None


# --- de wachtrij ---

_views = {}          # exemplaar -> view-ID van de rij in die Iconik


def _queue(instance, connection):
    import iconik_queue
    labels = host.setting(instance, "iconik.iconik_language") if host else ""
    language = host.language() if host else "nl"
    iconik_queue.set_language(language)
    iconik_queue.iconik_language[0] = labels if labels in ("nl", "en") else language
    queue = iconik_queue.Queue(connection, _views.get(instance))
    if queue.view_id is None:
        _views[instance] = queue.setup(create=True, language=iconik_queue.iconik_language[0])
    return queue


def poll(instance, who, enabled=True, auto=None):
    """Kijkt of er iets in de rij ligt en neemt hooguit één asset aan. Een fout komt in de staat (voor het venster)
    en in het log, niet als uitzondering: het volgende rondje probeert het opnieuw."""
    import iconik_queue
    state = iconik_queue.state
    state["enabled"] = bool(enabled)
    if not enabled:
        return None
    may = (lambda: host.may_take()) if host else (lambda: True)
    try:
        connection = client(instance)
        if connection is None:
            state["error"] = iconik_queue.tr("Niet ingelogd bij Iconik.", "Not logged in to Iconik.")
            return None
        queue = _queue(instance, connection)
        state["last_poll"] = iconik_queue.stamp()
        taken = []
        iconik_queue.poll_once(queue, who, busy=lambda: not may() or bool(taken),
                               add_job=lambda asset, remote=0, auto=False: taken.append((asset, remote, auto)),
                               auto=(lambda: auto) if auto else None, allowed=may)
        state["error"] = ""
    except Exception as error:
        state["error"] = iconik_queue.plain_error(error)
        base.print(f"Werker: {type(error).__name__}: {str(error)[:300]}")   # het hele verhaal alleen in het log
        return None
    if not taken:
        return None
    asset, attempt, was_auto = taken[0]
    state["last_asset"] = asset
    iconik_queue.current["instance"] = instance
    return {"key": asset, "attempt": int(attempt), "auto": bool(was_auto)}


class _Job:
    """Wat iconik_queue.report van een opdracht wil weten, uit gewone gegevens."""
    def __init__(self, job):
        self.asset_id, self.status, self.message = job["key"], job["status"], job.get("message") or ""
        self.local, self.remote = bool(job.get("local")), int(job.get("attempt") or 1)
        self.changed = False

    def set(self, message=None, **_):
        if message is not None:
            self.message, self.changed = message, True


def report(instance, job, who):
    """Zet het resultaat van een opdracht uit de rij terug in Iconik: {"outcome", "message"}. outcome is
    "requeued", "lost" of None; message is de aangevulde tekst van de opdracht, of None als die gelijk blijft."""
    import iconik_queue
    connection = client(instance)
    if connection is None:
        base.fail(base.L("Niet ingelogd bij Iconik.", "Not logged in to Iconik."))
    queue = _queue(instance, connection)
    view = _Job(job)
    outcome = iconik_queue.report(queue, view, who)
    return {"outcome": outcome, "message": view.message if view.changed else None}


def beat(instance, healthy=True):
    """Ververst het laatste teken van het asset waar deze Mac voor dit exemplaar aan werkt. True als geschreven."""
    import iconik_queue
    if not iconik_queue.current or iconik_queue.current.get("instance", instance) != instance:
        return False
    connection = client(instance)
    if connection is None:
        return False
    return bool(iconik_queue.beat(_queue(instance, connection), healthy=lambda: healthy))


def set_labels_language(instance, code):
    """De labels van de rij in deze Iconik in een taal (geldt voor iedereen in die omgeving)."""
    import iconik_queue
    connection = client(instance)
    if connection is None:
        base.fail(base.L("Niet ingelogd bij Iconik.", "Not logged in to Iconik."))
    queue = iconik_queue.Queue(connection)
    queue.setup(create=True, language=code)
    queue.apply_language(code)
