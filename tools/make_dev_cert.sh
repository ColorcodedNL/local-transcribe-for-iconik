#!/bin/bash
# Eenmalig: een eigen ondertekeningscertificaat "Local Transcribe Dev" in de login-sleutelhanger.
# scripts/build_bundle.sh tekent daarna elke build ermee, zodat macOS de app herkent als dezelfde en
# "Altijd toestaan" voor de sleutelhanger niet bij elke build opnieuw gevraagd wordt.
# Alleen voor deze Mac: het certificaat is door niemand anders uitgegeven en zegt buiten deze Mac niets.
# macOS vraagt één keer je wachtwoord om het certificaat te vertrouwen voor ondertekenen.
set -e
NAME="${LT_SIGN_IDENTITY:-Local Transcribe Dev}"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
if security find-identity -v -p codesigning | grep -q "\"$NAME\""; then
  echo "Het certificaat \"$NAME\" staat er al."; exit 0
fi
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
cat > "$WORK/cert.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
CNF
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$WORK/cert.cnf" \
  -keyout "$WORK/key.pem" -out "$WORK/cert.pem" 2>/dev/null
PASS="$(openssl rand -hex 12)"
openssl pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/cert.pem" -name "$NAME" \
  -out "$WORK/dev.p12" -passout "pass:$PASS" 2>/dev/null
security import "$WORK/dev.p12" -k "$KEYCHAIN" -P "$PASS" -T /usr/bin/codesign >/dev/null
security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$WORK/cert.pem"
security find-identity -v -p codesigning | grep "\"$NAME\"" && echo "Klaar. Bouw opnieuw met scripts/build_bundle.sh."
