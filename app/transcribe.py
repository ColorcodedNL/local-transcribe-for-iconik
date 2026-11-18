#!/usr/bin/env python3
"""Transcribeer een Iconik-asset lokaal met Whisper en push het resultaat
als transcript met woord-sync naar Iconik.

Gebruik:
    source ~/.iconik-env
    uv run transcribe.py <asset_id>            # proefrun, schrijft niets naar Iconik
    uv run transcribe.py <asset_id> --push     # schrijft het transcript naar Iconik
    uv run transcribe.py <asset_id> --speakers 2   # aantal sprekers opgeven
    uv run transcribe.py <asset_id> --no-speakers  # sprekerherkenning overslaan
    uv run transcribe.py <asset_id> --push --replace   # eigen eerdere transcript vervangen

Sinds november 2026 staat de code in losse delen (base, iconik_api, audio, speakers, filters, whisper_mlx,
pipeline). Dit bestand is de vaste ingang daarvoor:
  - `transcribe.X` lezen geeft altijd de actuele waarde uit het deel waar X thuishoort
  - `transcribe.X = ...` zet de waarde in elk deel dat X kent, zodat een vervanging (in een test of in de
    ontvanger, zoals LISTENER) aankomt waar de code hem gebruikt
Nieuwe code importeert liever het deel zelf. Het versienummer blijft hier staan: de buildscripts lezen het.
"""
import sys
import types

import base  # noqa: E402  als eerste: zet de telemetrie uit voordat pyannote geladen kan worden
import iconik_api
import audio
import speakers
import filters
import whisper_mlx
import pipeline

APP_VERSION = "0.6.2"  # ook het versienummer van de app-bundel (build_bundle.sh leest het hier)
RELEASES_REPO = "ColorcodedNL/local-transcribe-for-iconik"  # hier kijkt de app of er een nieuwere versie is

# In deze volgorde: een deel importeert alleen uit delen die eerder staan, dus het eerste deel met een naam
# is het deel waar die naam thuishoort.
PARTS = (base, iconik_api, audio, speakers, filters, whisper_mlx, pipeline)


def __getattr__(name):
    for part in PARTS:
        if name in part.__dict__:
            return part.__dict__[name]
    raise AttributeError(f"module 'transcribe' has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | {n for part in PARTS for n in part.__dict__ if not n.startswith("__")})


class _Entrance(types.ModuleType):
    def __setattr__(self, name, value):
        known = False
        if not name.startswith("__"):
            for part in PARTS:
                if name in part.__dict__:
                    setattr(part, name, value)
                    known = True
        if not known or name in self.__dict__:
            super().__setattr__(name, value)

    def __delattr__(self, name):
        found = False
        for part in PARTS:
            if name in part.__dict__:
                delattr(part, name)
                found = True
        if name in self.__dict__:
            super().__delattr__(name)
        elif not found:
            raise AttributeError(name)


sys.modules[__name__].__class__ = _Entrance


if __name__ == "__main__":
    try:
        pipeline.main()
    except base.Problem as problem:
        print(f"FOUT: {problem}", file=sys.stderr)
        sys.exit(1)
