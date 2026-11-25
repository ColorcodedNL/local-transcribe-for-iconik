"""Geheimen van een koppeling in de sleutelhanger, per exemplaar.

Een connector bewaart nooit zelf een geheim: hij vraagt het de app (connectors.Host.secret), en de app bewaart
het hier. Elk exemplaar heeft zijn eigen items, onder de dienst van de app (base.KEYCHAIN_SERVICE) met als
account "<exemplaar>:<naam>", bijvoorbeeld "iconik-10ce9b:login". Zo raakt uitloggen of weghalen van één Iconik
nooit de inlog van een andere.

Wat gelezen is, blijft in het geheugen: de sleutelhanger één keer lezen in plaats van bij elke aanroep. Bij een
build die macOS nog niet herkent, scheelt dat een rij wachtwoordvragen. Twee draden die tegelijk lezen (het
venster en de werker bij het opstarten) wachten op elkaar, zodat macOS maar één keer vraagt.

`keyring` is er op elk platform; de tests vervangen het door een nep-sleutelhanger.
"""
import re
import threading

import base

_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_INSTANCE = re.compile(r"[a-z]+-[0-9a-f]{6}")

_cache = {}               # (exemplaar, naam) -> waarde of None (gelezen, maar er stond niets)
_lock = threading.Lock()


def account(instance, name):
    """De accountnaam in de sleutelhanger. Gooit ValueError bij een exemplaar of naam die niet mag."""
    if not isinstance(instance, str) or not _INSTANCE.fullmatch(instance):
        raise ValueError(f"geen exemplaar: {instance!r}")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(f"geen naam voor een geheim: {name!r}")
    return f"{instance}:{name}"


def get(instance, name, fresh=False):
    """Het geheim (tekst), of None. Met `fresh` opnieuw uit de sleutelhanger in plaats van uit het geheugen."""
    key = (instance, name)
    which = account(instance, name)
    with _lock:
        if key in _cache and not fresh:
            return _cache[key]
        import keyring
        value = keyring.get_password(base.KEYCHAIN_SERVICE, which)
        _cache[key] = value
        return value


def put(instance, name, value):
    if not isinstance(value, str):
        raise ValueError("een geheim is tekst")
    which = account(instance, name)
    with _lock:
        import keyring
        keyring.set_password(base.KEYCHAIN_SERVICE, which, value)
        _cache[(instance, name)] = value


def delete(instance, name):
    """Haalt het geheim weg. Geeft False als weghalen niet lukte (dan staat het er misschien nog)."""
    which = account(instance, name)
    with _lock:
        _cache[(instance, name)] = None
        import keyring
        try:
            keyring.delete_password(base.KEYCHAIN_SERVICE, which)
        except Exception as error:
            if type(error).__name__ == "PasswordDeleteError":   # stond er niet: ook goed
                return True
            base.print(f"Geheim weghalen uit de sleutelhanger mislukte ({type(error).__name__}).")
            return False
        return True


def forget(instance=None):
    """Vergeet wat in het geheugen staat (van één exemplaar, of alles). De sleutelhanger zelf blijft."""
    with _lock:
        for key in [k for k in _cache if instance is None or k[0] == instance]:
            del _cache[key]
