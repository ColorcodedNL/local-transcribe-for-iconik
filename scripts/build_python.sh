#!/bin/bash
# Maakt build/python: de verplaatsbare Python van uv met alle pakketten uit uv.lock.
# Draai dit eerst, daarna build_ffmpeg.sh en build_bundle.sh.
#
# - De nieuwste Python 3.12 van uv (beveiligingsfixes in Python en de meegebakken OpenSSL).
# - Precies wat in uv.lock staat, met gecontroleerde hashes en zonder opnieuw op te lossen:
#   uv export met hashes, uv pip install met --require-hashes en --no-deps.
# - Pakketten die een job nooit laadt, worden niet geïnstalleerd (gemeten met
#   scripts/probe_imports.py op 23 oktober 2026). De controle onderaan laadt wat een job wel
#   laadt, zodat een te ruim weggelaten pakket bij het bouwen opvalt en niet bij een gebruiker.
set -e
cd "$(dirname "$0")/.."
if [ -e build/python ]; then
  echo "build/python bestaat al. Verwijder die map eerst voor een schone opbouw."
  exit 1
fi
mkdir -p build

# Niet nodig voor de app: telemetrie-export via gRPC (staat uit), de database van optuna,
# kleurtjes in tracebacks van rich, en netwerk voor fsspec. Met wat er alleen aan hangt.
LEAVE_OUT=(grpcio opentelemetry-exporter-otlp-proto-grpc
           sqlalchemy alembic mako
           pygments
           aiohttp aiohappyeyeballs aiosignal attrs frozenlist multidict propcache yarl)

uv python upgrade 3.12 || echo "Let op: uv python upgrade lukte niet, de geïnstalleerde 3.12 wordt gebruikt."
PY=$(uv python find --no-project 3.12)
SRC=$(dirname "$(dirname "$(realpath "$PY")")")
echo "Python: $SRC"
cp -R "$SRC" build/python
build/python/bin/python3.12 -I -c "import ssl, sys; print('Python', sys.version.split()[0], '/', ssl.OPENSSL_VERSION)"

EXCLUDE=()
for p in "${LEAVE_OUT[@]}"; do EXCLUDE+=(--no-emit-package "$p"); done
uv export --quiet --frozen --no-emit-project "${EXCLUDE[@]}" -o build/requirements.txt
uv pip install --python build/python/bin/python3.12 --break-system-packages --require-hashes --no-deps \
  -r build/requirements.txt

for p in "${LEAVE_OUT[@]}"; do
  if build/python/bin/python3.12 -I -c "import importlib.metadata as m, sys; m.distribution(sys.argv[1])" "$p" 2>/dev/null; then
    echo "$p is toch geïnstalleerd"; exit 1
  fi
done

# Wat een job laadt: Whisper, het sprekermodel met alles wat pyannote meeneemt, het venster,
# de sleutelhanger, updates. Faalt dit, dan is er te veel weggelaten.
build/python/bin/python3.12 -I -c "
import mlx_whisper, mlx_whisper.timing, torch, torchaudio, numba
import pyannote.audio
from pyannote.audio import Pipeline
from pyannote.audio.pipelines import SpeakerDiarization
import pyannote.audio.telemetry
import scipy.cluster.hierarchy, sklearn, matplotlib, optuna, lightning, huggingface_hub, hf_xet
import webview, keyring, requests, nacl.signing, certifi
print('imports ok')
"
