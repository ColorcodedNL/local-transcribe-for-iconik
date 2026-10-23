#!/usr/bin/env python3
"""Make a CycloneDX 1.6 SBOM of the built app bundle.

Reads what really ships in the bundle: the Python packages (their dist-info),
the bundled Python with the OpenSSL and Expat linked into it, FFmpeg and the
bundled speaker model. Writes dist/sbom-<version>.cdx.json for the release page,
where scanners such as Grype or Dependency-Track can read it.

    python3 scripts/build_sbom.py [path/to/Local Transcribe for Iconik.app]

Standard library only, so it runs with any Python 3.8 or later.
"""
import datetime
import email.parser
import json
import plistlib
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "Local Transcribe for Iconik.app"
RES = APP / "Contents" / "Resources"
REPO = "ColorcodedNL/local-transcribe-for-iconik"


def search(path, pattern):
    """First group of pattern in a file, or stop with a clear message."""
    match = re.search(pattern, path.read_bytes())
    if not match:
        sys.exit(f"build_sbom: no version found in {path} ({pattern!r})")
    return match.group(1).decode()


def one(pattern):
    found = sorted(RES.glob(pattern))
    if len(found) != 1:
        sys.exit(f"build_sbom: expected one match for {pattern}, found {len(found)}")
    return found[0]


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def native(name, version, cpe, description, license_id=None, supplier=None):
    component = {
        "type": "library",
        "bom-ref": f"native:{name.lower()}@{version}",
        "name": name,
        "version": version,
        "cpe": cpe,
        "description": description,
    }
    if license_id:
        component["licenses"] = [{"expression": license_id}]
    if supplier:
        component["supplier"] = {"name": supplier}
    return component


def python_packages():
    components = []
    for metadata in sorted(RES.glob("python/lib/python3.*/site-packages/*.dist-info/METADATA")):
        headers = email.parser.Parser().parsestr(metadata.read_text(errors="replace"), headersonly=True)
        name, version = headers["Name"], headers["Version"]
        purl = f"pkg:pypi/{normalize(name)}@{version}"
        component = {"type": "library", "bom-ref": purl, "name": name, "version": version, "purl": purl}
        if headers["License-Expression"]:
            component["licenses"] = [{"expression": headers["License-Expression"].strip()}]
        components.append(component)
    if not components:
        sys.exit(f"build_sbom: no Python packages found in {RES}")
    return components


def native_components():
    libpython = one("python/lib/libpython3.*.dylib")
    python = search(one("python/include/python3.*/patchlevel.h"), rb'#define PY_VERSION\s+"([^"]+)"')
    openssl = search(libpython, rb"OpenSSL (\d+\.\d+\.\d+)")
    expat = search(libpython, rb"expat_(\d+\.\d+\.\d+)")
    ffmpeg = search(RES / "licenses" / "ffmpeg-README.txt", rb"FFmpeg (\d+\.\d+(?:\.\d+)?)")
    return [
        native("CPython", python, f"cpe:2.3:a:python:python:{python}:*:*:*:*:*:*:*",
               "Bundled Python runtime", "PSF-2.0", "Python Software Foundation"),
        native("OpenSSL", openssl, f"cpe:2.3:a:openssl:openssl:{openssl}:*:*:*:*:*:*:*",
               "Linked into the bundled Python", "Apache-2.0", "OpenSSL Foundation"),
        native("libexpat", expat, f"cpe:2.3:a:libexpat_project:libexpat:{expat}:*:*:*:*:*:*:*",
               "Linked into the bundled Python", "MIT"),
        native("FFmpeg", ffmpeg, f"cpe:2.3:a:ffmpeg:ffmpeg:{ffmpeg}:*:*:*:*:*:*:*",
               "ffmpeg and ffprobe, built by scripts/build_ffmpeg.sh with --disable-network "
               "--disable-autodetect --disable-gpl --disable-nonfree --enable-zlib",
               "LGPL-2.1-or-later", "FFmpeg"),
    ]


def models():
    found = []
    for config in sorted(RES.glob("models/*/config.yaml")):
        name = config.parent.name
        found.append({
            "type": "machine-learning-model",
            "bom-ref": f"model:{name}",
            "name": f"pyannote/{name}",
            "description": "Speaker model, bundled",
            "licenses": [{"expression": "CC-BY-4.0"}],
        })
    return found


def main():
    info = plistlib.loads((APP / "Contents" / "Info.plist").read_bytes())
    version = info["CFBundleShortVersionString"]
    app_ref = f"pkg:github/{REPO}@v{version}"
    components = native_components() + python_packages() + models()
    bom = {
        "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
            "tools": {"components": [{"type": "application", "name": "scripts/build_sbom.py"}]},
            "component": {
                "type": "application",
                "bom-ref": app_ref,
                "name": "Local Transcribe for Iconik",
                "version": version,
                "purl": app_ref,
                "licenses": [{"expression": "MIT"}],
                "externalReferences": [{"type": "vcs", "url": f"https://github.com/{REPO}"}],
            },
        },
        "components": components,
        "dependencies": [{"ref": app_ref, "dependsOn": [c["bom-ref"] for c in components]}],
    }
    out = ROOT / "dist" / f"sbom-{version}.cdx.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(bom, indent=2) + "\n")
    natives = ", ".join(f"{c['name']} {c['version']}" for c in components if "cpe" in c)
    packages = sum(1 for c in components if "purl" in c)
    print(f"{out.relative_to(ROOT)}: {natives}, {packages} Python packages, {len(components) - packages - 4} model(s)")


if __name__ == "__main__":
    main()
