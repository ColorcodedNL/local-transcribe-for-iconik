"""Alle instellingen van de app op één plek: wat er is, de standaardwaarde, wat mag en een uitleg in twee talen.

Elke instelling heeft een sleutel met de groep ervoor ("general.app_language", "iconik.client_id"). De groep
is waar hij later in config.json en in het venster Instellingen staat; "iconik" is een koppeling en komt
straks per koppeling onder integrations.<id>.

    register(setting) een deel dat later komt (een engine, een koppeling) voegt zijn eigen instelling toe
    get(key)          de waarde, of de standaardwaarde als er niets (bruikbaars) staat
    put(key, value)   controleert de waarde tegen het schema en bewaart hem; anders SettingError
    put_many(changes) hetzelfde voor meerdere tegelijk: alles of niets

Lezen is ruim: alleen het type en de grenzen tellen, zoals de app het altijd deed. Bewaren is streng.
config.json is in groepen ingedeeld (zie "opslag" hieronder); `flat` is de naam uit het platte bestand van 0.6.
"""
import re
import secrets
import shutil
from dataclasses import dataclass, field

import base


class SettingError(ValueError):
    """Een waarde die niet mag. `dutch` en `english` zijn de melding voor de gebruiker."""
    def __init__(self, dutch, english):
        super().__init__(english)
        self.dutch, self.english = dutch, english

    def message(self):
        return base.L(self.dutch, self.english)


@dataclass(frozen=True)
class Setting:
    key: str                 # groep.naam
    kind: str                # bool, number, text, choice, languages, mapping
    default: object
    label: tuple             # (nl, en)
    help: tuple = ("", "")   # (nl, en)
    flat: str = ""           # de naam in het platte config.json van 0.6 (voor de omzetting)
    choices: tuple = ()      # bij choice
    minimum: float = None    # bij number: groter dan dit
    zero_ok: bool = False    # bij number: 0 mag ook (betekent iets, zoals geen buffer)
    pattern: str = ""        # bij text: de hele tekst moet hier aan voldoen
    max_length: int = 0      # bij text: 0 is geen grens
    advanced: bool = False   # alleen voor wie weet wat hij doet; niet in het gewone venster
    required: bool = False   # bij text: leeg mag niet
    invalid: tuple = ()      # (nl, en): eigen melding bij een ongeldige waarde
    section: str = ""        # kopje binnen de groep (een sleutel uit SECTIONS); leeg is bovenaan zonder kopje
    extra: dict = field(default_factory=dict)

    @property
    def group(self):
        return self.key.split(".", 1)[0]


# Kopjes binnen een groep, met de uitleg die het venster eronder zet.
SECTIONS = {
    "automatic": (("Werk dat de app vanzelf oppakt", "Work the app picks up by itself"),
                  ("De app begint alleen aan werk als deze Mac ruimte heeft: genoeg vrij geheugen en een processor en "
                   "grafische kaart die niet te druk zijn. Dit geldt voor automatisch transcriberen in Iconik, en later "
                   "ook voor een map die de app in de gaten houdt.",
                   "The app only starts work when this Mac has room: enough free memory and a processor and graphics "
                   "card that are not too busy. This applies to auto transcribe in Iconik, and later also to a folder "
                   "the app watches.")),
}

S = Setting
_SCHEMA = [
    # --- algemeen ---
    S("general.app_language", "choice", "auto", ("Taal van de app", "App language"),
      ("Automatisch volgt de taal van macOS.", "Automatic follows the macOS language."),
      flat="app_language", choices=("auto", "nl", "en")),

    # --- transcriptie ---
    S("transcription.language", "text", "auto", ("Gesproken taal", "Spoken language"),
      ("Automatisch herkent de taal per opdracht.", "Automatic detects the language per job."),
      flat="language", pattern=r"auto|[a-z]{2,3}",
      # Voor het venster: de keuzes in de lijst; getest zijn alleen automatisch, Nederlands en Engels.
      extra={"options": ["auto", "nl", "en", "de", "fr", "es", "it", "sv"], "tested": ["auto", "nl", "en"]}),
    S("transcription.expected_languages", "languages", ["nl", "en"], ("Te verwachten talen", "Expected languages"),
      ("Een paar woorden in een andere taal worden overgeslagen.", "A few words in another language are skipped."),
      flat="expected_languages", advanced=True),
    S("transcription.prefetch_min_free_gb", "number", 6.0,
      ("Vooruit lezen vanaf vrij geheugen (GB)", "Read ahead from free memory (GB)"),
      flat="prefetch_min_free_gb", minimum=0, advanced=True),
    # De ruimtecheck voor automatisch werk; geldt straks ook voor een map die lokale modus in de gaten houdt.
    S("transcription.auto_min_free_gb", "number", 12.0, ("Minimaal vrij geheugen (GB)", "Minimum free memory (GB)"),
      flat="auto_min_free_gb", minimum=0, advanced=True, section="automatic"),
    S("transcription.auto_max_cpu", "number", 40.0, ("Maximale processorbelasting (%)", "Maximum CPU load (%)"),
      flat="auto_max_cpu", minimum=0, advanced=True, section="automatic"),
    S("transcription.auto_max_gpu", "number", 40.0, ("Maximale GPU-belasting (%)", "Maximum GPU load (%)"),
      flat="auto_max_gpu", minimum=0, advanced=True, section="automatic"),

    # --- Refine ---
    S("refine.subtitles", "mapping", {}, ("Ondertitels", "Subtitles"),
      ("De regels en keuzes van de vorige keer; refine.py controleert ze.",
       "The rules and choices of last time; refine.py checks them."),
      flat="subtitles", advanced=True),

    # --- Iconik ---
    S("iconik.host", "text", "https://app.iconik.io", ("Iconik-adres", "Iconik address"),
      ("Alleen anders bij een eigen Iconik-omgeving.", "Only different for an own Iconik environment."),
      flat="host", pattern=r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?", max_length=200),
    S("iconik.client_id", "text", "", ("Client-ID", "Client ID"),
      ("Van de app-registratie in Iconik.", "From the app registration in Iconik."),
      flat="client_id", pattern=r"[0-9a-f-]{36}", required=True, extra={"lower": True},
      invalid=("Dat lijkt geen geldige client-ID.", "That does not look like a valid client ID.")),
    S("iconik.mounts", "mapping", {}, ("Opslag op deze Mac", "Storage on this Mac"),
      ("Per storage in Iconik de map op deze Mac.", "For each storage in Iconik the folder on this Mac."),
      flat="mounts"),
    S("iconik.worker_enabled", "bool", False, ("Opdrachten op afstand", "Remote jobs"), flat="worker_enabled"),
    S("iconik.machine_name", "text", "", ("Naam van deze Mac", "Name of this Mac"),
      ("Leeg is de computernaam.", "Empty means the computer name."), flat="machine_name", max_length=64),
    S("iconik.iconik_language", "choice", "", ("Taal van de labels in Iconik", "Language of the labels in Iconik"),
      ("Leeg volgt de app.", "Empty follows the app."), flat="iconik_language", choices=("", "nl", "en")),
    S("iconik.auto_transcribe", "bool", False, ("Automatisch transcriberen", "Auto transcribe"),
      ("Alleen als deze Mac ruimte heeft (zie Transcriptie).", "Only when this Mac has room (see Transcription)."),
      flat="auto_transcribe"),
    S("iconik.auto_audio", "bool", False, ("Ook audio", "Audio too"), flat="auto_audio"),
    S("iconik.auto_max_minutes", "number", 60.0, ("Langste asset (minuten)", "Longest asset (minutes)"),
      flat="auto_max_minutes", minimum=0, advanced=True),
]
SCHEMA = {s.key: s for s in _SCHEMA}
del S


def register(setting):
    """Een deel dat later in de volgorde staat (een engine, straks een koppeling) meldt zijn eigen instelling aan.
    Twee keer dezelfde is goed (een deel kan opnieuw geladen worden), twee verschillende met één sleutel niet."""
    known = SCHEMA.get(setting.key)
    if known is not None and known != setting:
        raise ValueError(f"instelling {setting.key} bestaat al anders")
    if known is None:
        SCHEMA[setting.key] = setting
        _SCHEMA.append(setting)
    return setting


def spec(key):
    try:
        return SCHEMA[key]
    except KeyError:
        raise KeyError(f"onbekende instelling: {key}") from None


def _usable(s, value):
    """Wat de app bij het lezen accepteert. Bewust ruim, zoals de verspreide .get()-aanroepen van 0.6."""
    if s.kind == "bool":
        return isinstance(value, bool)
    if s.kind == "number":
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and (s.minimum is None or value > s.minimum or (s.zero_ok and value == 0)))
    if s.kind in ("text", "choice"):
        return isinstance(value, str) and (s.kind != "choice" or value in s.choices)
    if s.kind == "languages":
        return isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)
    if s.kind == "mapping":
        return isinstance(value, dict)
    return False


def _copy(value):
    return dict(value) if isinstance(value, dict) else list(value) if isinstance(value, list) else value


# --- opslag: config.json in groepen (versie 2) ---
#
#   {"version": 2, "general": {...}, "transcription": {...}, "refine": {...},
#    "integrations": {"iconik-a1b2c3": {"type": "iconik", ...}}}
#
# Een koppeling staat per exemplaar onder integrations, met een eigen ID; de app kent er nu één per soort.
# Tot 0.6 was het bestand één platte lijst. Die wordt bij het eerste lezen eenmalig omgezet; het oude bestand
# blijft daarbij als config-0.6.json naast het nieuwe staan.

VERSION = 2
GROUPS = ("general", "transcription", "refine")
INTEGRATIONS = ("iconik",)        # groepen die een koppeling zijn
BACKUP_NAME = "config-0.6.json"


def _new_id(kind):
    return f"{kind}-{secrets.token_hex(3)}"


def _instance(config, kind, create=False):
    """Het exemplaar van deze koppeling (nu altijd het eerste van die soort), of None."""
    found = config.setdefault("integrations", {}) if create else config.get("integrations") or {}
    for iid in sorted(found):
        if isinstance(found[iid], dict) and found[iid].get("type") == kind:
            return found[iid]
    if not create:
        return None
    found[_new_id(kind)] = item = {"type": kind}
    return item


def _home(config, s, create=False):
    """Het blok in config waar deze instelling staat, of None als het er (nog) niet is."""
    if s.group in INTEGRATIONS:
        return _instance(config, s.group, create)
    if create:
        return config.setdefault(s.group, {})
    block = config.get(s.group)
    return block if isinstance(block, dict) else None


def _name(s):
    return s.key.split(".", 1)[1]


def _adopt(config):
    """Zet platte sleutels die de lijst kent op hun plek. Bij de omzetting alles; daarna alleen wat er los
    bijkwam (een instelling die een later deel pas aanmeldde). Geeft True als er iets veranderde."""
    by_flat = {s.flat: s for s in SCHEMA.values() if s.flat}
    changed = False
    for flat in [k for k in config if k in by_flat]:
        s = by_flat[flat]
        _home(config, s, create=True)[_name(s)] = config.pop(flat)
        changed = True
    return changed


def _migrate(config):
    """Maakt van een plat bestand (0.6) versie 2. Onbekende sleutels blijven ongemoeid staan."""
    if config.get("version") == VERSION:
        return _adopt(config)
    try:
        backup = base.CONFIG_PATH.with_name(BACKUP_NAME)
        if base.CONFIG_PATH.is_file() and not backup.exists():
            shutil.copy2(base.CONFIG_PATH, backup)
    except OSError as error:
        base.print(f"Reservekopie van de voorkeuren mislukte: {error}")
    _adopt(config)
    config["version"] = VERSION
    for group in GROUPS:
        config.setdefault(group, {})
    config.setdefault("integrations", {})
    return True


def _config():
    """config.json als versie 2. Zet een oud bestand eenmalig om (en schrijft dat weg); een leeg of
    ontbrekend bestand blijft leeg tot er iets bewaard wordt."""
    config = base.load_config()
    if not config:
        return {}
    if config.get("version") == VERSION and not any(s.flat in config for s in SCHEMA.values()):
        return config
    return base.update_config(_migrate)


def _read(config, s):
    home = _home(config, s)
    return home.get(_name(s)) if home is not None else None


def _write(config, s, value):
    _migrate(config)
    _home(config, s, create=True)[_name(s)] = value


def get(key):
    s = spec(key)
    value = _read(_config(), s)
    if not _usable(s, value):
        return _copy(s.default)
    return float(value) if s.kind == "number" else _copy(value)


def stored(key):
    """Wat er echt in het bestand staat, zonder standaardwaarde of controle; None als er niets staat."""
    return _copy(_read(_config(), spec(key)))


def values(group):
    """Alle instellingen van een groep met hun huidige waarde, voor het venster."""
    return {key: get(key) for key, s in SCHEMA.items() if s.group == group}


def integrations():
    """De koppelingen in config.json: [{"id", "type"}]. Nu hooguit één per soort."""
    found = _config().get("integrations") or {}
    return [{"id": iid, "type": item.get("type")} for iid, item in sorted(found.items()) if isinstance(item, dict)]


def update(key, change):
    """Voor een instelling met een eigen weg (mounts, ondertitels): leest de huidige waarde, laat `change`
    hem aanpassen en bewaart, in één keer onder de grendel. Geen controle: die doet de eigen weg."""
    s = spec(key)

    def apply(config):
        current = _read(config, s)
        current = _copy(current) if _usable(s, current) else _copy(s.default)
        change(current)
        _write(config, s, current)
    base.update_config(apply)


def raw_put(key, value):
    """Bewaart zonder controle. Alleen voor tests (een kapotte waarde in het bestand) en de omzetting."""
    s = spec(key)
    base.update_config(lambda config: _write(config, s, value))


def clean(key, value):
    """De waarde zoals hij bewaard wordt, of SettingError. Verandert niets aan config.json."""
    s = spec(key)
    wrong = SettingError(*(s.invalid or (f"Ongeldige waarde voor {s.label[0]}.", f"Invalid value for {s.label[1]}.")))
    if s.kind == "bool":
        if not isinstance(value, bool):
            raise wrong
        return value
    if s.kind == "number":
        if isinstance(value, str):
            try:
                value = float(value.replace(",", "."))
            except ValueError:
                raise wrong from None
        if not _usable(s, value):
            raise wrong
        return float(value)
    if s.kind == "choice":
        if value not in s.choices:
            raise wrong
        return value
    if s.kind == "text":
        if not isinstance(value, str):
            raise wrong
        value = value.strip()
        if s.extra.get("lower"):
            value = value.lower()
        if s.max_length and len(value) > s.max_length:
            raise SettingError(f"{s.label[0]}: hooguit {s.max_length} tekens.", f"{s.label[1]}: at most {s.max_length} characters.")
        if s.required and not value:
            raise wrong
        if s.pattern and value and not re.fullmatch(s.pattern, value):
            raise wrong
        if s.pattern and not value and s.default:   # leeg bij een tekst met een standaardwaarde: terug naar de standaard
            return s.default
        return value
    if s.kind == "languages":
        if isinstance(value, str):
            value = [v for v in re.split(r"[\s,]+", value) if v]
        if not isinstance(value, list) or not value or not all(isinstance(v, str) and re.fullmatch(r"[a-zA-Z]{2,3}", v) for v in value):
            raise wrong
        return [v.lower() for v in value]
    if s.kind == "mapping":
        if not isinstance(value, dict):
            raise wrong
        return dict(value)
    raise wrong  # een soort die het schema niet kent


def put(key, value):
    """Controleert en bewaart. Geeft de bewaarde waarde terug."""
    return put_many({key: value})[key]


def put_many(changes):
    """Controleert alles eerst; mag één waarde niet, dan wordt niets bewaard (SettingError van de eerste).
    Daarna in één keer weggeschreven. Geeft de bewaarde waarden terug."""
    cleaned = {key: clean(key, value) for key, value in changes.items()}

    def change(config):
        for key, value in cleaned.items():
            _write(config, spec(key), value)
    base.update_config(change)
    return cleaned


def defaults():
    return {key: _copy(s.default) for key, s in SCHEMA.items()}


def reset(keys):
    """Terug naar de standaardwaarde: de sleutels gaan uit config.json, zodat een latere andere standaard
    (bijvoorbeeld per Mac) vanzelf geldt. Geeft de sleutels die er echt stonden."""
    specs = [spec(key) for key in keys]
    removed = []

    def change(config):
        for s in specs:
            home = _home(config, s)
            if home is not None and _name(s) in home:
                del home[_name(s)]
                removed.append(s.key)
    if specs and _config():
        base.update_config(change)
    return removed


def remove_integration(instance_id):
    """Haalt een koppeling met al haar instellingen uit config.json."""
    if _config():
        base.update_config(lambda config: (config.get("integrations") or {}).pop(instance_id, None))


def custom(group):
    """De sleutels van een groep die iemand zelf anders heeft ingesteld dan de standaard. Een bewaarde waarde
    die gelijk is aan de standaard telt niet: dan is er niets aangepast."""
    config = _config()
    return [key for key, s in SCHEMA.items() if s.group == group and _read(config, s) is not None
            and _read(config, s) != s.default]


def describe(group=None, advanced=False):
    """Het schema als gewone gegevens, voor het venster: per instelling soort, standaard, grenzen en tekst
    in de taal van de app. Zonder advanced alleen wat in het gewone venster hoort."""
    out = []
    for s in _SCHEMA:
        if (group and s.group != group) or (s.advanced and not advanced):
            continue
        item = {"key": s.key, "kind": s.kind, "default": _copy(s.default), "advanced": s.advanced,
                "label": base.L(*s.label), "help": base.L(*s.help) if any(s.help) else ""}
        if s.choices:
            item["choices"] = list(s.choices)
        if s.minimum is not None:
            item["minimum"] = s.minimum
        if s.max_length:
            item["max_length"] = s.max_length
        for hint in ("options", "tested"):   # aanwijzingen voor het venster, zoals de talen in een keuzelijst
            if hint in s.extra:
                item[hint] = list(s.extra[hint])
        if s.section:
            heading, text = SECTIONS[s.section]
            item["section"] = {"id": s.section, "title": base.L(*heading), "help": base.L(*text)}
        out.append(item)
    return out
