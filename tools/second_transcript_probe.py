"""Proef voor Refine: hoe toont Iconik twee transcripten op één asset?

Zet naast het transcript van de app een tweede, zoals Refine dat straks met een correctie doet: zelfde
tekst, maar met een eigen kenmerk (engine_info.name), zodat de app het nooit als het eigen transcript ziet
en het dus nooit vervangt. De eerste regel begint met [REFINE-PROEF] en de sprekers heten anders, zodat
je in Iconik meteen ziet welk transcript getoond wordt.

Schrijft naar Iconik (alleen dit ene proeftranscript). Gebruikt het token van de app zonder het te verversen.

Draaien:
  uv run tools/second_transcript_probe.py <asset-ID>            zet het proeftranscript erop
  uv run tools/second_transcript_probe.py <asset-ID> --list     laat zien welke transcripten er staan
  uv run tools/second_transcript_probe.py <asset-ID> --remove   haalt het proeftranscript weer weg
  uv run tools/second_transcript_probe.py <asset-ID> --replace WOORD NIEUW
      zet nog een proeftranscript erop (ook als er al een staat) met WOORD vervangen door NIEUW in de
      woorden zelf. Met twee van deze op elkaar is het onderste verborgen: vindt zoeken het NIEUW daarvan nog?
  uv run tools/second_transcript_probe.py <asset-ID> --reindex [REALM ...]
      laat Iconik het asset opnieuw indexeren (POST assets/v1/assets/<id>/reindex/) met deze realms;
      zonder realms gaat er een ongeldige naam mee, zodat Iconik in de foutmelding de geldige noemt
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import video_probe  # noqa: E402  (ReadOnlyIconik: het token van de app, nooit verversen)
import transcribe  # noqa: E402
import transcript as tmodel  # noqa: E402
import iconik  # noqa: E402

PROBE_ENGINE = "local-transcribe-refine-proef"


def listing(client, info):
    return client.get(f"assets/v1/assets/{info['asset_id']}/versions/{info['version_id']}"
                      f"/transcriptions/properties/").get("objects", [])


def show(client, info):
    print(f"Transcripten op '{info['title']}':")
    for t in listing(client, info):
        engine = (t.get("engine_info") or {}).get("name") or "?"
        print(f"  {t['id']}  engine={engine}  taal={t.get('language')}  sprekers={t.get('speaker_labels')}")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asset_id, action = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    client = video_probe.ReadOnlyIconik()
    connector = iconik.IconikConnector(client)
    original_print = transcribe.print
    transcribe.print = lambda *a, **k: None          # de meldingen van open() zijn hier niet nodig
    try:
        info = connector.open(asset_id, push=False)
    finally:
        transcribe.print = original_print

    if action == "--reindex":
        realms = sys.argv[3:] or ["ongeldige-realm-om-de-namen-te-zien"]
        try:
            answer = client.post(f"assets/v1/assets/{asset_id}/reindex/", {"realms": realms})
            print(f"Herindexering gestart met realms {realms}. Antwoord: {answer or '(leeg)'}")
        except transcribe.Problem as error:
            print(f"Iconik weigerde: {error}")
        return
    if action not in ("", "--list", "--remove", "--replace") or (action == "--replace" and len(sys.argv) < 5):
        sys.exit(__doc__)
    if action == "--list":
        return show(client, info)
    if action == "--remove":
        probes = [t for t in listing(client, info) if (t.get("engine_info") or {}).get("name") == PROBE_ENGINE]
        for t in probes:
            removed = transcribe.delete_transcription(client, asset_id, info["version_id"], t["id"])
            print(f"Proeftranscript {t['id']} verwijderd ({removed} segmenten).")
        if not probes:
            print("Geen proeftranscript gevonden.")
        return show(client, info)

    if action != "--replace" and any((t.get("engine_info") or {}).get("name") == PROBE_ENGINE for t in listing(client, info)):
        sys.exit("Er staat al een proeftranscript op. Eerst --remove, of kijk met --list.")
    doc = connector.read(info)
    if doc is None:
        sys.exit("Dit asset heeft geen transcript van de app om naast te zetten.")
    for speaker in doc.speakers:
        speaker.name = f"Proef {speaker.name}"
    if doc.segments:
        doc.segments[0].text = "[REFINE-PROEF] " + doc.segments[0].display_text()
    if action == "--replace":
        wanted, new, replaced = sys.argv[3].lower(), sys.argv[4].lower(), 0
        for segment in doc.segments:
            for word in segment.words:
                core = word.text.strip(".,!?;:\"'").lower()
                if core == wanted:
                    word.text = word.text.lower().replace(wanted, new)
                    word.score = 1.0
                    replaced += 1
            if replaced and segment.text:
                segment.text = ""     # regeltekst weer uit de woorden opbouwen
        if not replaced:
            sys.exit(f"'{sys.argv[3]}' komt niet voor in de woorden van het transcript.")
        print(f"'{sys.argv[3]}' {replaced} keer vervangen door '{new}'.")

    engine_info = {"type": "EXTERNAL", "name": PROBE_ENGINE, "model": doc.engine.get("model", ""),
                   "version": transcribe.APP_VERSION}
    version_id = info["version_id"]
    properties = dict(tmodel.to_iconik(doc, "", version_id)[0], engine_info=engine_info)
    path = f"assets/v1/assets/{asset_id}/versions/{version_id}/transcriptions/properties/"
    transcription_id = client.post(path, properties)["id"]
    segments = [s for s in tmodel.to_iconik(doc, transcription_id, version_id)[1] if s["segment_text"]]
    try:
        for offset in range(0, len(segments), transcribe.BULK_LIMIT):
            client.post(f"assets/v1/assets/{asset_id}/segments/bulk/",
                        {"objects": segments[offset:offset + transcribe.BULK_LIMIT]})
    except BaseException:
        transcribe.delete_transcription(client, asset_id, version_id, transcription_id)
        raise
    print(f"Proeftranscript {transcription_id} gezet ({len(segments)} segmenten).\n")
    show(client, info)


if __name__ == "__main__":
    main()
