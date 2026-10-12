# /// script
# requires-python = ">=3.12"
# dependencies = ["pynacl>=1.5.0"]
# ///
"""Sleutel maken en updates ondertekenen voor Local Transcribe for Iconik.

    uv run tools/sign_update.py keygen            # eenmalig: maakt het sleutelpaar
    uv run tools/sign_update.py sign bestand.zip  # schrijft bestand.zip.sig
    uv run tools/sign_update.py verify bestand.zip --pub <publieke sleutel>

De privésleutel komt in een bestand (standaard ~/.config/local-transcribe/update-signing.key,
rechten 600) en wordt nooit getoond. Bewaar een kopie in je wachtwoordmanager. Wie de sleutel
heeft, kan updates maken die de app vertrouwt. De publieke sleutel staat in de app
(updater.PUBLIC_KEY) en mag openbaar zijn.
"""
import argparse
import base64
import os
import sys
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

DEFAULT_KEY = Path.home() / ".config" / "local-transcribe" / "update-signing.key"


def key_path(arg):
    return Path(arg or os.environ.get("LOCALTRANSCRIBE_SIGNING_KEY") or DEFAULT_KEY).expanduser()


def load_signing_key(path):
    if not path.is_file():
        sys.exit(f"Geen sleutel gevonden op {path}. Maak er een met: uv run tools/sign_update.py keygen")
    return SigningKey(base64.b64decode(path.read_text().strip()))


def keygen(args):
    path = key_path(args.key)
    if path.exists():
        sys.exit(f"{path} bestaat al. Ik overschrijf een sleutel nooit. Verplaats of verwijder hem zelf als je echt een nieuwe wilt.")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = SigningKey.generate()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(bytes(key)).decode() + "\n")
    public = base64.b64encode(bytes(key.verify_key)).decode()
    print(f"Privésleutel: {path} (rechten 600, niet getoond)")
    print("Zet hem in je wachtwoordmanager zonder hem op het scherm te tonen, bijvoorbeeld:")
    print(f"    pbcopy < {path}")
    print("en plak hem in Proton Pass. Wis het klembord daarna.")
    print()
    print(f"Publieke sleutel (mag openbaar): {public}")


def sign(args):
    key = load_signing_key(key_path(args.key))
    data = Path(args.file).read_bytes()
    signature = key.sign(data).signature
    out = Path(str(args.file) + ".sig")
    out.write_text(base64.b64encode(signature).decode() + "\n")
    print(f"Ondertekend: {out}")
    print(f"Publieke sleutel: {base64.b64encode(bytes(key.verify_key)).decode()}")


def verify(args):
    data = Path(args.file).read_bytes()
    signature = base64.b64decode(Path(str(args.file) + ".sig").read_text().strip())
    try:
        VerifyKey(base64.b64decode(args.pub)).verify(data, signature)
    except BadSignatureError:
        sys.exit("HANDTEKENING KLOPT NIET")
    print("Handtekening klopt")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (("keygen", keygen), ("sign", sign), ("verify", verify)):
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        if name != "keygen":
            p.add_argument("file")
        if name != "verify":
            p.add_argument("--key", help="Pad naar de privésleutel")
        else:
            p.add_argument("--pub", required=True, help="Publieke sleutel (base64)")
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
