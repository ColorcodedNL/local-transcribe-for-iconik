"""Connectors: hoe de app met een ander systeem praat (Iconik nu; later Nextcloud, een andere MAM, of een
connector van iemand anders als apart programma). De vaste vorm, zodat een connector een toevoeging is.

Een connector is een module met deze namen:
  NAME                  de soort, ook het begin van het exemplaar-ID ("iconik" -> iconik-a1b2c3)
  TITLE                 (nl, en): hoe de soort heet in het venster
  CAPABILITIES          wat hij kan, een deel van CAPABILITIES hieronder
  SETTINGS              zijn instellingen (settings.Setting, groep = NAME); de app meldt ze aan bij settings
  start(host)           krijgt de gastheer (Host hieronder): wat de app hem geeft
  en per capability de functies uit FORM.

Alles wat de app aan een connector geeft en terugkrijgt is gewone gegevens: tekst, getallen, waar/onwaar,
None, lijsten en woordenboeken met tekst als sleutel. Geen objecten, geen open verbindingen. Een transcript gaat
als woordenboek (transcript.to_dict). Zo kan dezelfde vorm later over een pijp lopen, met een connector als
eigen programma (een versie van JSON over stdin/stdout, zoals LSP of MCP). call() controleert dat elke keer.

Elke functie krijgt als eerste het exemplaar (iconik-a1b2c3): één connector bedient alle exemplaren van zijn
soort, zoals twee Iconik-omgevingen. Wat de connector per exemplaar nodig heeft, vraagt hij de gastheer.

Geheimen (een inlog, een token) bewaart een connector nooit zelf: hij vraagt en geeft ze via de gastheer, die
ze per exemplaar in de sleutelhanger zet (keychain.py).

Alles wat Macs met elkaar delen (knoppen, een wachtrij) gebruikt het domein van de omgeving, nooit het
exemplaar-ID: dat maakt elke Mac zelf.

Een fout die de gebruiker moet zien, is base.Problem (met skip=True als de opdracht overgeslagen wordt in plaats
van mislukt). Over een pijp wordt dat een foutbericht met dezelfde tekst en hetzelfde skip.
"""
import importlib
import json

import base
import settings

CAPABILITIES = (
    "source",       # opdrachten over items in dit systeem: openen en het mediabestand vinden
    "write_back",   # een transcript terugschrijven
    "read_back",    # een transcript teruglezen (Refine)
    "queue",        # een wachtrij die meerdere Macs delen (opdrachten op afstand)
    "auto_scan",    # zelf werk zoeken als deze Mac ruimte heeft (automatisch transcriberen)
)

# Per capability de functies die een connector dan heeft. De betekenis staat bij de connector zelf (iconik.py).
FORM = {
    None: ("status",),                                     # elke connector
    "source": ("open", "find_media"),
    "write_back": ("write",),
    "read_back": ("read",),
    "queue": ("poll", "report", "beat"),
    "auto_scan": (),                                       # poll neemt dan ook automatisch werk mee
}

# Welke module bij welke soort hoort. Pas geladen als iemand hem nodig heeft.
KINDS = {"iconik": "iconik"}

_loaded = {}        # soort -> gestarte module
host = None         # de gastheer van deze app (start_all zet hem)


class FormError(TypeError):
    """Een connector of een aanroep houdt zich niet aan de vaste vorm. Een fout in de code."""


# --- gewone gegevens ---

def plain(value, where="waarde"):
    """Gooit FormError als `value` geen gewone gegevens zijn. Geeft de waarde terug."""
    def walk(v, path):
        if v is None or isinstance(v, (bool, int, str)):
            return
        if isinstance(v, float):
            if v != v or v in (float("inf"), float("-inf")):
                raise FormError(f"{path}: geen eindig getal")
            return
        if isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                walk(item, f"{path}[{i}]")
            return
        if isinstance(v, dict):
            for k, item in v.items():
                if not isinstance(k, str):
                    raise FormError(f"{path}: sleutel {k!r} is geen tekst")
                walk(item, f"{path}.{k}")
            return
        raise FormError(f"{path}: {type(v).__name__} is geen gewoon gegeven")
    walk(value, where)
    return value


# --- de gastheer: wat de app een connector geeft ---

class Host:
    """Wat een connector van de app krijgt. Ook dit is alleen gewone gegevens erin en eruit, zodat het
    later over dezelfde pijp kan."""

    def setting(self, instance, key):
        """Een instelling van dit exemplaar (key zoals "iconik.host"), of van de app ("transcription.language")."""
        return settings.get(key, instance) if spec_group(key) in settings.INTEGRATIONS else settings.get(key)

    def put_setting(self, instance, key, value):
        """Bewaart een instelling van dit exemplaar met de controle van het schema (settings.SettingError)."""
        if spec_group(key) not in settings.INTEGRATIONS:
            raise FormError(f"een connector bewaart alleen zijn eigen instellingen, niet {key}")
        return settings.put(key, value, instance)

    def noted(self, instance, key):
        """Een feit dat de app bij het exemplaar bijhoudt (geen instelling), zoals het domein van de omgeving."""
        return settings.noted(instance, key)

    def note(self, instance, key, value):
        settings.note(instance, key, plain(value, "note"))

    def secret(self, instance, name):
        import keychain
        return keychain.get(instance, name)

    def put_secret(self, instance, name, value):
        import keychain
        keychain.put(instance, name, value)

    def delete_secret(self, instance, name):
        import keychain
        return keychain.delete(instance, name)

    def log(self, text):
        base.print(str(text))

    def language(self):
        """"nl" of "en": de taal van de app."""
        return base.app_language()


def spec_group(key):
    return settings.spec(key).group


# --- laden en aanroepen ---

def check_module(module):
    """Gooit FormError als een connector-module niet de vaste vorm heeft."""
    for name in ("NAME", "TITLE", "CAPABILITIES", "SETTINGS", "start"):
        if not hasattr(module, name):
            raise FormError(f"connector {getattr(module, '__name__', module)} mist {name}")
    unknown = [c for c in module.CAPABILITIES if c not in CAPABILITIES]
    if unknown:
        raise FormError(f"connector {module.NAME}: onbekende capability {unknown}")
    if "auto_scan" in module.CAPABILITIES and "queue" not in module.CAPABILITIES:
        raise FormError(f"connector {module.NAME}: auto_scan gaat via de wachtrij (queue)")
    missing = [f for f in functions(module.CAPABILITIES) if not callable(getattr(module, f, None))]
    if missing:
        raise FormError(f"connector {module.NAME} mist {', '.join(missing)}")
    wrong = [s.key for s in module.SETTINGS if s.group != module.NAME]
    if wrong:
        raise FormError(f"connector {module.NAME}: instellingen buiten zijn groep: {wrong}")


def functions(capabilities):
    out = list(FORM[None])
    for capability in capabilities:
        out += [f for f in FORM[capability] if f not in out]
    return out


def load(kind):
    """De gestarte module van een soort. De eerste keer: laden, controleren, instellingen aanmelden, starten."""
    if kind in _loaded:
        return _loaded[kind]
    if kind not in KINDS:
        raise FormError(f"onbekende connector: {kind!r}")
    module = importlib.import_module(KINDS[kind])
    install(module)
    return module


def install(module):
    """Neemt een connector-module op (ook voor de tests, met een nep-connector)."""
    check_module(module)
    settings.register_integration(module.NAME)
    for setting in module.SETTINGS:
        if setting.key not in settings.SCHEMA:
            settings.register(setting)
    KINDS.setdefault(module.NAME, module.__name__)
    module.start(host or Host())
    _loaded[module.NAME] = module
    return module


def kind_of(instance):
    """De soort van een exemplaar, uit config.json. FormError als het exemplaar er niet is."""
    for item in settings.integrations():
        if item["id"] == instance:
            return item["type"]
    raise FormError(f"geen koppeling: {instance!r}")


def can(instance, capability):
    try:
        return capability in load(kind_of(instance)).CAPABILITIES
    except FormError:
        return False


def instances(capability=None):
    """De exemplaren (ID's) die er zijn, eventueel alleen die met deze capability."""
    found = []
    for item in settings.integrations():
        if item["type"] not in KINDS:
            continue
        if capability is None or capability in load(item["type"]).CAPABILITIES:
            found.append(item["id"])
    return found


def call(instance, function, **params):
    """Roept een functie van de connector van dit exemplaar aan, met gewone gegevens erin en eruit."""
    module = load(kind_of(instance))
    if function not in functions(module.CAPABILITIES):
        raise FormError(f"{module.NAME} heeft geen {function} (capabilities: {', '.join(module.CAPABILITIES)})")
    plain(params, f"{function}()")
    result = getattr(module, function)(instance, **params)
    return plain(result, f"{function}() gaf")


def roundtrip(value):
    """Zoals het over een pijp zou aankomen. Voor de tests."""
    return json.loads(json.dumps(plain(value)))


def start_all(the_host=None):
    """Zet de gastheer neer en laadt de connectors van de koppelingen die er zijn."""
    global host
    host = the_host or Host()
    for item in settings.integrations():
        if item["type"] in KINDS:
            load(item["type"])
