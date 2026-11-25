"""Opdrachten op afstand: de lus die de wachtrij van elke koppeling afloopt, en de naam van deze Mac.

Een koppeling met een wachtrij (capability "queue", nu Iconik) zegt zelf wat er ligt: elk rondje vraagt deze lus
per exemplaar `poll` (iets aannemen) en `beat` (laten weten dat de lopende opdracht nog leeft), via
connectors.call met alleen gewone gegevens. Wat de wachtrij in Iconik precies doet, staat in iconik_queue.py.

Op deze Mac loopt één opdracht tegelijk. De lus vraagt een exemplaar alleen om werk als de app er ruimte voor heeft
(de gastheer zegt dat met may_take); een aanvraag gaat binnen een exemplaar altijd voor automatisch werk.

Tot stap 5 stond de rij in Iconik zelf hier. De oude namen (worker.Queue, worker.state, worker.FIELD, ...) verwijzen
nog naar iconik_queue, zodat bestaande code en tests ze vinden.
"""
import threading

import connectors
import items
from system import computer_name  # noqa: F401  (tests vervangen hem onder deze naam)

POLL_SECONDS = 60
wake = threading.Event()   # een schakelaar in Instellingen zet hem, zodat de werker meteen kijkt

machine_alias = [""]      # eigen naam voor deze Mac in de eerste Iconik (Instellingen); leeg: de computernaam
_aliases = {}             # exemplaar -> eigen naam, voor een tweede en volgende Iconik
MAX_NAME = 40


def __getattr__(name):
    """De namen van de rij in Iconik, die tot stap 5 hier stonden."""
    import iconik_queue
    if name in iconik_queue.__dict__ and not name.startswith("__"):
        return iconik_queue.__dict__[name]
    raise AttributeError(f"module 'worker' has no attribute {name!r}")


def clean_alias(name):
    """Een eigen naam zoals hij in het detailveld mag: leesbare tekens, geen tekens die de lijst "mislukt op"
    breken, geen regeleinden, niet te lang."""
    import iconik_queue
    name = "".join(c for c in (name or "") if c.isprintable())
    return " ".join(iconik_queue.clean_name(name).split())[:MAX_NAME].strip()


def _first(instance):
    found = connectors.settings.instances("iconik")
    return instance is None or not found or instance == found[0]


def set_machine_alias(name, instance=None):
    """De eigen naam van deze Mac in een Iconik (zonder exemplaar: de eerste). Elke Iconik kan een eigen naam hebben:
    hij staat in de instellingen van die koppeling."""
    cleaned = clean_alias(name)
    if _first(instance):
        machine_alias[0] = cleaned
    else:
        _aliases[instance] = cleaned
    return cleaned


def alias_of(instance=None):
    return machine_alias[0] if _first(instance) else _aliases.get(instance, "")


def machine_name(instance=None):
    """De naam waaronder deze Mac in de wachtrij van een Iconik staat: de eigen naam uit Instellingen, anders de
    computernaam. Iedereen die het asset ziet, ziet deze naam, en in een computernaam staat vaak de naam van een persoon."""
    return alias_of(instance) or computer_name()


def job_data(job):
    """Een opdracht (jobs.Job) als gewone gegevens voor de connector."""
    return {"key": items.key(job.item), "status": job.status, "message": job.message or "",
            "local": bool(job.local), "attempt": int(job.remote or 1)}


def report(job, log=print):
    """Meldt het resultaat van een opdracht uit de rij terug bij de koppeling waar hij vandaan kwam. Geeft de uitkomst
    van de connector: "requeued" (terug in de rij voor een andere Mac), "lost" (niet meer van deze Mac) of None.
    Een tekst die de connector aan de melding toevoegt, komt bij de opdracht."""
    instance = items.source(job.item)
    result = connectors.call(instance, "report", job=job_data(job), who=machine_name(instance)) or {}
    if result.get("message") is not None:
        job.set(message=result["message"])
    return result.get("outcome")


def round_once(enabled, add_job, log=print, healthy=None, auto=None):
    """Eén rondje langs alle exemplaren met een wachtrij. Geeft het item dat is aangenomen, of None.

    `enabled(exemplaar)`: staan opdrachten op afstand aan voor dit exemplaar. `auto(exemplaar)`: de regels voor
    automatisch werk (opslag, audio, langste asset) als dat aan staat en deze Mac ruimte heeft, anders None.
    `healthy()`: vordert de lopende opdracht nog; zo niet, dan blijft het laatste teken staan en neemt een andere
    Mac het na een paar minuten over."""
    taken = None
    for instance in connectors.instances("queue"):
        on = bool(enabled(instance))
        try:
            connectors.call(instance, "beat", healthy=bool(healthy()) if healthy else True)
        except Exception as error:
            log(f"Werker: laatste teken schrijven mislukte: {str(error)[:200]}")
        if taken is not None:
            on = False   # één opdracht per rondje; het volgende exemplaar komt de volgende keer aan bod
        rules = auto(instance) if (auto and on) else None
        try:
            got = connectors.call(instance, "poll", who=machine_name(instance), enabled=on, auto=rules)
        except Exception as error:
            log(f"Werker: {type(error).__name__}: {str(error)[:300]}")
            continue
        if got:
            taken = items.make(instance, got["key"])
            add_job(got["key"], remote=got["attempt"], auto=bool(got.get("auto")), source=instance)
            log(f"Werker: asset {got['key']} aangenomen uit de rij.")
    return taken


def run(enabled, add_job, stop, log=print, interval=POLL_SECONDS, healthy=None, auto=None):
    """De lus van de werker. `enabled` en `auto` worden elk rondje opnieuw gevraagd, zodat een schakelaar zonder
    herstart werkt. Fouten worden gelogd en het volgende rondje probeert het opnieuw."""
    while not stop.is_set():
        try:
            round_once(enabled, add_job, log, healthy, auto)
        except Exception as error:
            log(f"Werker: {type(error).__name__}: {str(error)[:300]}")
        if wake.wait(interval):
            wake.clear()


def start(enabled, add_job, log=print, healthy=None, auto=None):
    stop = threading.Event()
    threading.Thread(target=run, args=(enabled, add_job, stop, log, POLL_SECONDS, healthy, auto),
                     daemon=True, name="werker").start()
    return stop
