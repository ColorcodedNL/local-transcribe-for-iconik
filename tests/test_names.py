"""De namen van de app (stap 6g): wat je ziet heet Local Transcribe (bundel, Dock, dmg), wat intern is blijft
Local Transcribe for Iconik (Application Support, Logs, sleutelhanger), en wat bij de klant in Iconik staat
verandert nooit mee. Deze test legt de bouwscripts, de app en de oude naam naast elkaar.

Draaien: python3 -B tests/test_names.py
"""
import re
import sys
from pathlib import Path

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


base = Path("app/base.py").read_text()
display = re.search(r'^DISPLAY_NAME = "([^"]+)"', base, re.M).group(1)
internal = re.search(r'^INTERNAL_NAME = "([^"]+)"', base, re.M).group(1)
check("weergavenaam en interne naam", display == "Local Transcribe" and internal == "Local Transcribe for Iconik", (display, internal))

bundle = Path("scripts/build_bundle.sh").read_text()
check("bundel: de map heet zoals de app", f'OUT="$HERE/build/{display}.app"' in bundle)
check("bundel: CFBundleName en CFBundleDisplayName", f"<key>CFBundleName</key><string>{display}</string>" in bundle
      and f"<key>CFBundleDisplayName</key><string>{display}</string>" in bundle)
check("bundel: de bytecode kent het pad in Programma's", f'INSTALLED_RES="/Applications/{display}.app/Contents/Resources"' in bundle)
check("bundel: de bundle-ID en het linktype blijven (sleutels bij de klant)",
      "<string>nl.colorcoded.local-transcribe</string>" in bundle and "<string>iconiktranscribe</string>" in bundle)
check("dmg: dezelfde naam", f'NAME="{display}"' in Path("scripts/build_dmg.sh").read_text())
check("SBOM: dezelfde naam", f'"name": "{display}",' in Path("scripts/build_sbom.py").read_text()
      and f'"build" / "{display}.app"' in Path("scripts/build_sbom.py").read_text())

# Intern verandert niets: niets hoeft te verhuizen.
check("intern: support-map en sleutelhanger op de interne naam", "SUPPORT_DIR = system.support_dir(INTERNAL_NAME)" in base
      and "KEYCHAIN_SERVICE = INTERNAL_NAME" in base)
app_main = Path("app/app_main.py").read_text()
check("intern: app_main en de vaste laag ook", f'INTERNAL_NAME = "{internal}"' in app_main
      and f'APP_NAME = "{internal}"' in Path("app/app_shell.py").read_text())

# De naam van vóór 0.2.0 is weg, de oude bundel wordt één keer aangeboden voor de prullenmand.
for name in ("app/base.py", "app/app_main.py", "app/iconik_api.py"):
    text = Path(name).read_text()
    check(f"geen verhuizing van Iconik Transcribe meer in {name}", "OLD_APP_NAME" not in text and "_old_dir" not in text
          and 'with_name("Iconik Transcribe")' not in text)
check("oude bundel: de naam tot stap 6g, één keer gevraagd", 'OLD_BUNDLE = "Local Transcribe for Iconik.app"' in app_main
      and "OLD_BUNDLE_KEPT" in app_main and "def offer_old_bundle" in app_main)
check("opstarten bij inloggen: het pad wordt bij elke start bijgewerkt",
      "if serve.login_item_on():" in app_main and "serve.set_login_item(True)" in app_main)

# Wat bij de klant in Iconik staat: de velden, de view en de knoppen houden hun sleutels.
queue = Path("app/iconik_queue.py").read_text()
check("Iconik: geen Nederlandse statuswaarden van 0.3.1 meer", "aangevraagd\": REQUESTED" not in queue and "LEGACY_VALUE" not in queue)

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
