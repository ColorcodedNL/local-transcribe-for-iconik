"""Vaste laag van Local Transcribe for Iconik: wat een code-update niet kan veranderen.

Dit bestand en main.py zitten alleen in de app-bundel, nooit in een code-update.

SHELL_VERSION gaat omhoog als een update iets nodig heeft wat de bundel niet heeft: een andere
Python, nieuwe pakketten, een nieuwe ffmpeg of een aangepaste main.py of app_shell.py. Zo'n update
is dan een nieuwe dmg, geen code-update.

PUBLIC_KEY controleert de handtekening van updates. De privésleutel staat in Proton Pass
(zie tools/sign_update.py). Een andere sleutel invoeren kan alleen met een nieuwe dmg.
"""
import re
from pathlib import Path

SHELL_VERSION = 3
PUBLIC_KEY = "g4XE02D2oSS+XiFvXbzqQdRpF0Mboixkr8iKnKRIczM="  # Ed25519, base64

APP_NAME = "Local Transcribe for Iconik"
SUPPORT = Path.home() / "Library" / "Application Support" / APP_NAME
CODE_DIR = SUPPORT / "code"   # hier staan gedownloade code-updates, naast de bundel en niet erin


def version_tuple(text):
    numbers = [int(n) for n in re.findall(r"\d+", text or "")[:3]]
    return tuple(numbers + [0] * (3 - len(numbers)))
