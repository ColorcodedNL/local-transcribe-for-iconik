"""Wie een item is: de bron waar het vandaan komt plus de eigen sleutel daarin, als één tekst.

    iconik-10ce9b:0edde392-a79f-11f0-bbd0-aa870b39fb9a    een asset in één Iconik-omgeving (het exemplaar)
    local:<id>                                            een bestand op deze Mac (lokale modus, stap 6)

De bron is het exemplaar van een koppeling, niet de soort: twee Iconik-omgevingen kunnen hetzelfde asset-ID
hebben. Alles wat de app per item bewaart (de kopie van het transcript, golfvormen, het concept, het origineel
en de ondertitellaag van Refine) staat in een map per bron: transcripts/iconik-10ce9b/<asset>.json. Zo gaat alles
van één bron in één keer weg, en botsen lokale en Iconik-sleutels nooit.

Tot stap 4 stonden die bestanden los in transcripts/, waves/ en refine/ met alleen het asset-ID. migrate() zet ze
eenmalig in de map van de enige Iconik; jobs.json wordt bij het lezen omgezet (jobs.py).
"""
import json
import os
import re

import base
import settings
import transcript as tmodel

LOCAL = "local"
SOURCE_RE = r"iconik-[0-9a-f]{6}|local"
KEY_RE = r"[A-Za-z0-9_-]{1,64}"
_ITEM = re.compile(rf"({SOURCE_RE}):({KEY_RE})")
_KEY = re.compile(KEY_RE)

# Per map de achtervoegsels van de bestanden per item (de langste eerst).
SUFFIXES = {"transcripts": (".json",), "waves": (".media.json", ".waves"),
            "refine": (".subtitles.json", ".original.json", ".draft.json")}


def valid(item):
    return isinstance(item, str) and _ITEM.fullmatch(item) is not None


def split(item):
    """(bron, sleutel). Gooit ValueError bij iets wat geen item is."""
    found = _ITEM.fullmatch(item or "") if isinstance(item, str) else None
    if found is None:
        raise ValueError(f"geen item: {item!r}")
    return found.group(1), found.group(2)


def make(source, key):
    item = f"{source}:{key}"
    split(item)
    return item


def source(item):
    return split(item)[0]


def key(item):
    return split(item)[1]


def is_iconik(item):
    return valid(item) and item.startswith("iconik-")


def asset_id(item):
    """Het asset-ID in Iconik, of "" voor een item dat niet uit Iconik komt."""
    return key(item) if is_iconik(item) else ""


def iconik_source(create=False):
    """Het exemplaar van de Iconik-koppeling (nu hooguit één). Met create komt het er als het er nog niet is."""
    return settings.instance_id("iconik", create=create)


def iconik(asset, source=None):
    """Het item van een asset in Iconik. Zonder bron: de enige Iconik (die er dan komt als hij er nog niet is).
    None bij een asset-ID dat geen asset-ID kan zijn."""
    if not isinstance(asset, str) or not _KEY.fullmatch(asset):
        return None
    return make(source or iconik_source(create=True), asset)


# Een knop in Iconik wordt door één Mac neergezet en door iedereen in die omgeving gebruikt, terwijl elke Mac zijn
# eigen exemplaar-ID maakt. De knop zegt daarom uit welke omgeving hij komt met het system_domain_id van Iconik
# (?lt_domain=...); elke Mac onthoudt na het inloggen bij welk domein zijn exemplaar hoort (settings.note).
DOMAIN_KEY = "system_domain_id"
_DOMAIN = re.compile(r"[0-9A-Fa-f-]{8,64}")


def valid_domain(domain):
    return isinstance(domain, str) and _DOMAIN.fullmatch(domain) is not None


def iconik_for_domain(domain=""):
    """Het exemplaar van de Iconik-omgeving waar een knop vandaan komt, of None. Zonder domein (een knop van vóór
    stap 4e): de enige Iconik. Weet het exemplaar zijn domein nog niet (net ingelogd, nog niet opgehaald) en is er
    maar één Iconik, dan is het die."""
    found = [item["id"] for item in settings.integrations() if item["type"] == "iconik"]
    if not domain:
        return found[0] if found else None
    if not valid_domain(domain):
        return None
    for instance in found:
        if settings.noted(instance, DOMAIN_KEY) == domain:
            return instance
    if len(found) == 1 and not settings.noted(found[0], DOMAIN_KEY):
        return found[0]
    return None


def from_request(item="", asset=""):
    """Het item uit een link of verzoek van het venster: ?item=..., of een kaal ?asset=... van een oudere pagina
    (de enige Iconik). None als het geen item is. Maakt geen Iconik aan."""
    if item:
        return item if valid(item) else None
    if asset and _KEY.fullmatch(asset):
        found = iconik_source()
        return make(found, asset) if found else None
    return None


# --- bestanden per item ---

def path(root, item, suffix):
    """Het bestand van dit item in een map van de app: <root>/<bron>/<sleutel><achtervoegsel>."""
    found, name = split(item)
    return root / found / f"{name}{suffix}"


def from_path(root, file, suffix):
    """Het item bij een bestand dat path() gaf, of None."""
    try:
        relative = file.relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) != 2 or not relative.name.endswith(suffix):
        return None
    candidate = f"{relative.parts[0]}:{relative.name[:-len(suffix)]}"
    return candidate if valid(candidate) else None


def files(root, suffix):
    """Alle bestanden met dit achtervoegsel in de mappen per bron, als (item, pad)."""
    out = []
    if not root.is_dir():
        return out
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        for file in sorted(folder.glob(f"*{suffix}")):
            item = from_path(root, file, suffix)
            if item:
                out.append((item, file))
    return out


def save_transcript(doc, item=None):
    """Bewaart een kopie van het transcript in het eigen model op deze Mac, met de sporenanalyse die Iconik
    niet kent. Voor de editor (Refine) en later voor het bijstellen op basis van correcties. Mag nooit een
    opdracht laten mislukken: het transcript staat dan al in Iconik."""
    try:
        item = item or doc.source.get("item") or iconik(doc.source.get("asset_id") or "")
        if not valid(item):
            raise ValueError("onbekend item")
        doc.source["item"] = item
        target = path(base.TRANSCRIPTS_DIR, item, ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(json.dumps(tmodel.to_dict(doc), ensure_ascii=False))
        os.replace(temp, target)
    except Exception as error:
        base.print(f"Kopie van het transcript bewaren lukte niet: {error}")


# --- eenmalig: de losse bestanden van vóór stap 4 in de map van de enige Iconik ---

def legacy_files(roots):
    """De bestanden die nog los in een map staan, als (map, bestand, sleutel, achtervoegsel). `roots`:
    {"transcripts": pad, "waves": pad, "refine": pad}."""
    found = []
    for kind, root in roots.items():
        if not root.is_dir():
            continue
        for file in sorted(p for p in root.iterdir() if p.is_file()):
            suffix = next((s for s in SUFFIXES[kind] if file.name.endswith(s)), None)
            if suffix is None:
                continue          # .tmp en wat de app niet kent: blijft staan
            name = file.name[:-len(suffix)]
            if _KEY.fullmatch(name):
                found.append((root, file, name, suffix))
    return found


def migrate(roots, log=base.print):
    """Zet losse bestanden van vóór stap 4 in de map van de enige Iconik (die er dan komt als hij er nog niet
    is). Verplaatst, gooit nooit iets weg; staat er al een bestand met die naam, dan blijven beide staan.
    Een tweede keer doet niets. Geeft (verplaatst, blijven staan)."""
    found = legacy_files(roots)
    if not found:
        return 0, 0
    target = iconik_source(create=True)
    moved = kept = 0
    for root, file, name, suffix in found:
        destination = path(root, make(target, name), suffix)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                kept += 1
                continue
            os.replace(file, destination)
            moved += 1
        except OSError as error:
            kept += 1
            log(f"Bestand niet verplaatst ({type(error).__name__}).")
    log(f"Bestanden per item naar de map van {target}: {moved} verplaatst"
        + (f", {kept} bleven staan (bestond al of lukte niet)." if kept else "."))
    return moved, kept
