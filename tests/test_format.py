"""Het transcriptformaat local-transcribe v1 als openbaar contract (stap 6f): het schema, de voorbeelden en de
uitleg in docs/format/ moeten kloppen met wat de app schrijft en leest.

Draaien: uv run --no-project --with jsonschema python -B tests/test_format.py
(zonder jsonschema slaat hij alleen het toetsen aan het schema over)
"""
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, "app")
_kr = types.ModuleType("keyring")
_kr.get_password = lambda *a: None
_kr.set_password = lambda *a: None
_kr.delete_password = lambda *a: None
sys.modules["keyring"] = _kr
import base  # noqa: E402

base.L = lambda nl, en: nl
tmp = Path(tempfile.mkdtemp())
base.CONFIG_PATH = tmp / "config.json"
import local_files  # noqa: E402
import transcript as model  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


DOCS = Path("docs/format")
schema = json.loads((DOCS / "local-transcribe-v1.schema.json").read_text())
examples = {p.name: json.loads(p.read_text()) for p in sorted((DOCS / "examples").glob("*.json"))}
check("voorbeelden: er zijn er twee", len(examples) == 2, list(examples))

# --- het schema volgt de code ---
check("schema: format en versie van de code", schema["properties"]["format"]["const"] == model.FORMAT
      and schema["properties"]["version"]["const"] == model.VERSION)
written = model.to_dict(model.Transcript())
check("schema: verplicht is precies wat to_dict altijd schrijft", set(schema["required"]) == set(written), sorted(written))
check("schema: elk veld dat to_dict schrijft staat erin", set(written) <= set(schema["properties"]))

# --- de voorbeelden lezen, en ongewijzigd terugschrijven ---
for name, data in examples.items():
    doc = model.from_dict(data)
    again = model.to_dict(doc)
    again["source"], again["engine"] = data["source"], data["engine"]
    check(f"voorbeeld {name}: gelezen en hetzelfde teruggeschreven", again == data)
refined = examples["interview.refined.transcript.json"]
check("voorbeeld na Refine: engine en source zoals de app ze schrijft", refined["engine"]["name"] == base.REFINED_ENGINE
      and refined["source"].get("refined") is True)

# --- wat de app naast de media schrijft, volgt het schema ---
doc = model.from_dict(examples["interview.transcript.json"])
beside = local_files.file_dict(doc, "0123456789abcdef0123456789abcdef", "interview.mov", {"name": "mlx-whisper", "version": "0.4.1"})
check("naast de media: alleen de bestandsnaam, nooit een pad", beside["source"]["media"] == "interview.mov"
      and "/" not in json.dumps(beside["source"]))
try:
    import jsonschema
except ImportError:
    jsonschema = None
    print("OK   schema toetsen overgeslagen: jsonschema niet geïnstalleerd (run_tests.sh heeft het wel)")
if jsonschema:
    validator = jsonschema.Draft202012Validator
    validator.check_schema(schema)
    check("schema: zelf geldig (2020-12)", True)
    for name, data in examples.items():
        errors = [e.message for e in validator(schema).iter_errors(data)]
        check(f"voorbeeld {name}: volgt het schema", not errors, errors)
    errors = [e.message for e in validator(schema).iter_errors(beside)]
    check("wat de app naast de media schrijft volgt het schema", not errors, errors)
    broken = json.loads(json.dumps(beside))
    del broken["source"]["fingerprint"]
    check("schema: lokaal zonder vingerafdruk wordt geweigerd", any(validator(schema).iter_errors(broken)))

# --- de versieregel ---
newer_minor = dict(examples["interview.transcript.json"], minor=3, iets_nieuws={"x": 1})
check("versieregel: een hogere minor met onbekende velden wordt gelezen", len(model.from_dict(newer_minor).segments) == 2)
try:
    model.from_dict(dict(examples["interview.transcript.json"], version=2))
    check("versieregel: een hogere major wordt geweigerd", False)
except ValueError:
    check("versieregel: een hogere major wordt geweigerd", True)

# --- de vingerafdruk uit de uitleg geeft wat de app geeft ---
readme = (DOCS / "README.md").read_text()
code = re.search(r"```python\n(import hashlib.*?)```", readme, re.S).group(1)
space = {}
exec(code, space)
small, large = tmp / "klein.mov", tmp / "groot.mov"
small.write_bytes(os.urandom(5000))
large.write_bytes(os.urandom(3 * 1024 * 1024 + 12345))
check("vingerafdruk: de code in de uitleg is die van de app (klein en groot bestand)",
      all(space["fingerprint"](p) == local_files.fingerprint(p) for p in (small, large)))
check("uitleg: noemt het schema en beide voorbeelden", all(n in readme for n in ["local-transcribe-v1.schema.json", *examples]))

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
