"""De Iconik-connector: alles wat de transcriptie met Iconik te maken heeft, achter één vaste vorm.

Een connector doet vier dingen voor de rest van de app:
  open(asset_id, ...)   wat er bekend is over het asset: titel, versie, welke transcripten er al staan
  find_media(...)       de mediabestanden op deze Mac, als lijst (kopieën; later losse audiobestanden)
  write(info, doc, ...) een transcript uit het eigen model (transcript.py) wegschrijven
  read(info, ...)       een transcript terug in het eigen model, bijvoorbeeld voor de editor (Refine)

De wachtrij en de status van opdrachten op afstand staan nog in worker.py; die horen ook bij Iconik en
verhuizen hierheen zodra er een tweede connector met een eigen wachtrij is (lokale bestanden).

De verbinding zelf (inloggen, tokens, verzoeken) en het zoeken van het bestand op een ISG staan nog in
transcribe.py (Iconik, find_media_file, delete_transcription); deze module roept ze via `core` aan, zodat
tests die daar iets vervangen ook hier werken.
"""
import transcribe as core
import transcript as tmodel

NAME = "iconik"
REINDEX_REALMS = ["TRANSCRIPTIONS", "SEGMENTS"]   # de namen die Iconik voor transcripten gebruikt


class IconikConnector:
    name = NAME

    def __init__(self, client):
        self.client = client   # transcribe.Iconik, of iets met dezelfde get/post/delete

    # --- open ---

    def open(self, asset_id, push=True, replace=True):
        """Het asset en zijn transcripten. Weigert (fail) wat de app niet mag doen: nooit een transcript uit
        een andere bron overschrijven, en zonder `replace` ook het eigen eerdere transcript niet."""
        L, fail, print_ = core.L, core.fail, core.print
        asset = self.client.get(f"assets/v1/assets/{asset_id}/")
        version_id = core.find_version(asset)
        print_(f"Asset:   {asset.get('title')}")
        existing = self.client.get(
            f"assets/v1/assets/{asset_id}/versions/{version_id}/transcriptions/properties/").get("objects", [])
        own = [t for t in existing if (t.get("engine_info") or {}).get("name") == core.ENGINE_NAME]
        foreign = [t for t in existing if t not in own]
        if foreign:
            message = f"Dit asset heeft {len(foreign)} transcript(en) uit een andere bron."
            if push:
                fail(L(message + " Die worden nooit overschreven.",
                       f"This asset has {len(foreign)} transcript(s) from another source. Those are never overwritten."), skip=True)
            print_(f"Let op: {message} Een proefrun kan, pushen niet.")
        if own:
            message = f"Dit asset heeft al {len(own)} transcript(en) van dit script."
            if push and not replace:
                fail(message + " Gebruik --push --replace om te vervangen.")
            print_(f"Let op: {message}" + (" Wordt vervangen." if push else ""))
        return {"connector": NAME, "asset_id": asset_id, "version_id": version_id,
                "title": asset.get("title") or "", "asset": asset, "own": own, "foreign": foreign}

    # --- media ---

    def find_media(self, info, mount_override=None, ask_mount=False, interactive=True):
        """De mediabestanden van het asset op deze Mac. Nu altijd één: de eerste kopie die hier te vinden is."""
        return [core.find_media_file(self.client, info["asset_id"], mount_override, ask_mount, interactive)]

    # --- schrijven ---

    def write(self, info, doc, engine_info, still_ours=None):
        """Schrijft het transcript naar Iconik en geeft het id van het nieuwe transcript.

        Eerst het nieuwe helemaal neerzetten, pas daarna het oude weghalen. Mislukt het schrijven halverwege,
        dan blijft het oude staan en gaat het halve nieuwe weer weg. Daarna gaan ook eigen transcripten weg
        die een andere Mac intussen op dit asset zette, zodat er precies één van deze app overblijft."""
        L, fail, print_ = core.L, core.fail, core.print
        asset_id, version_id = info["asset_id"], info["version_id"]
        if still_ours is not None and not still_ours():
            fail(L("Een andere Mac heeft dit asset overgenomen; er is niets naar Iconik geschreven.",
                   "Another Mac took over this asset; nothing was written to Iconik."), skip=True)
        properties = dict(tmodel.to_iconik(doc, "", version_id)[0], engine_info=engine_info)
        transcriptions = f"assets/v1/assets/{asset_id}/versions/{version_id}/transcriptions/properties/"
        transcription_id = self.client.post(transcriptions, properties)["id"]
        segments = [s for s in tmodel.to_iconik(doc, transcription_id, version_id)[1] if s["segment_text"]]
        try:
            for offset in range(0, len(segments), core.BULK_LIMIT):
                batch = segments[offset:offset + core.BULK_LIMIT]
                self.client.post(f"assets/v1/assets/{asset_id}/segments/bulk/", {"objects": batch})
                print_(f"Gepusht: segment {offset + 1} tot en met {offset + len(batch)}")
        except BaseException:
            try:
                core.delete_transcription(self.client, asset_id, version_id, transcription_id)
                print_(f"Schrijven mislukte; het halve transcript {transcription_id} is weer weggehaald.")
            except Exception as error:
                print_(f"Schrijven mislukte en het halve transcript {transcription_id} weghalen ook: {error}")
            raise
        try:
            existing = self.client.get(transcriptions).get("objects", [])
        except core.Problem:
            existing = info.get("own") or []
        replaced = False
        for old in existing:
            if (old.get("engine_info") or {}).get("name") == core.ENGINE_NAME and old["id"] != transcription_id:
                removed = core.delete_transcription(self.client, asset_id, version_id, old["id"])
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
            core.print("Zoekindex van het asset wordt bijgewerkt.")
            return True
        except Exception as error:
            core.print(f"Zoekindex bijwerken lukte niet ({error}); oude woorden kunnen vindbaar blijven tot een herindexering.")
            return False

    # --- lezen ---

    def read(self, info, engine_name=None):
        """Het transcript van deze app op het asset, terug in het eigen model, of None als er geen is.
        `engine_name`: een ander eigen transcript, bijvoorbeeld de gecorrigeerde versie van Refine later."""
        wanted = engine_name or core.ENGINE_NAME
        asset_id, version_id = info["asset_id"], info["version_id"]
        listing = self.client.get(
            f"assets/v1/assets/{asset_id}/versions/{version_id}/transcriptions/properties/").get("objects", [])
        matches = [t for t in listing if (t.get("engine_info") or {}).get("name") == wanted]
        if not matches:
            return None
        properties = matches[-1]
        segments, page = [], 1
        while True:
            objects = self.client.get(
                f"assets/v1/assets/{asset_id}/segments/?segment_type=TRANSCRIPTION"
                f"&transcription_id={properties['id']}&per_page={core.PAGE_SIZE}&page={page}").get("objects", [])
            # Zelf nog eens filteren, zodat er nooit segmenten van een ander transcript meekomen.
            segments += [o for o in objects if o.get("transcription_id") == properties["id"]]
            if len(objects) < core.PAGE_SIZE:
                break
            page += 1
        return tmodel.from_iconik(properties, segments, source={
            "connector": NAME, "asset_id": asset_id, "version_id": version_id,
            "title": info.get("title") or "", "transcription_id": properties["id"]})
