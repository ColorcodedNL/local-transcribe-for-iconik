# Software Bill of Materials

What is inside the app bundle of Local Transcribe for Iconik, and the known vulnerabilities that affect it. This lists what actually ships in the macOS bundle, not everything in `uv.lock` (which also resolves CUDA, Linux and Windows packages that are never installed on a Mac).

- App version: 0.4.0 (shell as in `app/app_shell.py`), macOS 13.0 or later
- Built from commit: 880b5be
- Last updated: 2026-10-23
- Last vulnerability check: 2026-10-23

A machine-readable version (CycloneDX 1.6) is attached to every release from 0.4.0 on as `sbom-<version>.cdx.json`, made by `scripts/build_sbom.py`. Scan it with for example `grype sbom:sbom-0.4.0.cdx.json`.

Found something we missed? Open an issue, or for anything sensitive use a private security advisory on this repository.

## Native components

| Component | Version | Source | Notes |
| --- | --- | --- | --- |
| CPython | 3.12.15 | uv-managed Python, copied by `scripts/build_python.sh` | |
| OpenSSL | 3.5.9 | linked into CPython | TLS to the Iconik API, Hugging Face and GitHub (updates) |
| Expat | 2.9.0 | linked into CPython | |
| FFmpeg (ffmpeg, ffprobe) | 9.0.2 | built from the official tarball by `scripts/build_ffmpeg.sh`, sha256 checked | `--disable-gpl --disable-nonfree --disable-autodetect --disable-network --enable-zlib`. Runs as a separate process per file |
| zlib | 1.3.2 | bundled in the torchcodec wheel | |
| zlib-ng (compat) | 1.3.1 | bundled in the Pillow wheel | |
| WebKit | system | macOS, through pywebview | updated with macOS, not by this app |

## Models

| Model | Revision | How | Notes |
| --- | --- | --- | --- |
| pyannote/speaker-diarization-community-1 | bundled | `Resources/models/`, loaded locally | |
| mlx-community/whisper-large-v3-mlx | not pinned | downloaded from Hugging Face on first use | |

## Python packages (104)

```
anyio==4.15.1
asteroid-filterbanks==0.4.0
bottle==0.13.4
certifi==2026.10.19
cffi==2.1.1
charset-normalizer==3.5.2
click==8.5.0
cloudpickle==3.1.2
colorlog==6.12.0
contourpy==1.4.0
cycler==0.12.1
einops==0.8.2
filelock==4.0.12
fonttools==4.66.1
fsspec==2026.9.0
googleapis-common-protos==1.75.5
h11==0.16.0
hf-xet==1.7.0
httpcore2==2.13.1
httpx2==2.13.1
huggingface-hub==2.2.0
idna==3.20
jaraco.classes==3.4.0
jaraco-context==6.1.2
jaraco-functools==4.6.0
jinja2==3.1.6
joblib==1.6.0
julius==0.2.8
keyring==25.7.0
kiwisolver==1.5.1
lightning==2.6.6
lightning-utilities==0.15.3
llvmlite==0.50.0
markdown-it-py==4.2.0
markupsafe==3.0.4
matplotlib==3.11.2
mdurl==0.1.2
mlx==0.32.3
mlx-metal==0.32.3
mlx-whisper==0.4.3
more-itertools==11.1.0
mpmath==1.3.0
narwhals==2.26.0
networkx==3.7
numba==0.68.0
numpy==2.5.3
opentelemetry-api==1.45.1
opentelemetry-exporter-http-transport==0.66b1
opentelemetry-exporter-otlp==1.45.1
opentelemetry-exporter-otlp-common==0.66b1
opentelemetry-exporter-otlp-proto-common==1.45.1
opentelemetry-exporter-otlp-proto-http==1.45.1
opentelemetry-proto==1.45.1
opentelemetry-sdk==1.45.1
opentelemetry-semantic-conventions==0.66b1
optuna==5.0.0
packaging==26.3
pandas==3.0.6
pillow==13.0.0
primePy==1.3
protobuf==7.36.2
proxy-tools==0.1.0
pyannote-audio==4.0.7
pyannote-core==6.0.1
pyannote-database==6.1.1
pyannote-metrics==4.1
pyannote-pipeline==4.0.0
pyannoteai-sdk==0.4.0
pycparser==3.1
pynacl==1.6.2
pyobjc-core==12.2.2
pyobjc-framework-cocoa==12.2.2
pyobjc-framework-quartz==12.2.2
pyobjc-framework-security==12.2.2
pyobjc-framework-uniformtypeidentifiers==12.2.2
pyobjc-framework-webkit==12.2.2
pyparsing==3.3.3
python-dateutil==2.9.0.post0
pytorch-lightning==2.6.6
pytorch-metric-learning==2.9.0
pywebview==6.2.1
pyyaml==6.0.3
regex==2026.9.29
requests==2.34.2
rich==15.0.0
safetensors==0.8.0
scikit-learn==1.9.1
scipy==1.18.1
setuptools==84.0.0
six==1.17.0
sortedcontainers==2.4.0
sympy==1.14.0
threadpoolctl==3.7.0
tiktoken==0.14.0
torch==2.14.1
torch-audiomentations==0.12.0
torch-pitch-shift==1.2.5
torchaudio==2.11.0
torchcodec==0.17.0
torchmetrics==1.9.0
tqdm==4.70.1
truststore==0.10.4
typing-extensions==4.16.0
urllib3==2.8.0
```

Left out of the bundle on purpose (`LEAVE_OUT` in `scripts/build_python.sh`): grpcio, opentelemetry-exporter-otlp-proto-grpc, sqlalchemy, alembic, mako, pygments, aiohttp and its dependencies.

## Exposure

Where a vulnerability matters for this app, most relevant first:

1. **FFmpeg**: parses media files from the archive. Demuxer and decoder bugs apply. Network protocols, libssh and other external libraries are not built in.
2. **Network clients**: requests, urllib3, certifi, OpenSSL, huggingface-hub, hf-xet. Outgoing only, to Iconik, Hugging Face and GitHub.
3. **Local web server** (`app/serve.py`, bottle through pywebview): listens on 127.0.0.1 only and checks the Host header against DNS rebinding.
4. **Updates**: signed with Ed25519 (pynacl), see `docs/RELEASING.md`.
5. **Model loading**: torch, safetensors, pyyaml.

Vulnerabilities in distributed training, CUDA, server use of torch, lightning or optuna, and Windows or Linux only issues do not apply.

## Vulnerability status

| Date | Advisory | Component | Status | Reason |
| --- | --- | --- | --- | --- |
| 2026-10-23 | pip-audit, all 104 packages | Python packages | Not affected | No known vulnerabilities |
| 2026-10-23 | OpenSSL advisory of 29 Sep 2026 | OpenSSL 3.5.9 | Fixed | 3.5.9 is the fixed release |
| 2026-10-23 | Python 3.12.15 security release | CPython 3.12.15 | Fixed | Latest 3.12 security release |
| 2026-10-23 | libexpat 2.9.0 security release | Expat 2.9.0 | Fixed | 2.9.0 is the fixed release |
| 2026-10-23 | CVE-2026-8461 and others fixed in 9.0 | FFmpeg 9.0.2 | Fixed | Fixed in 9.0 |
| 2026-10-23 | CVE-2026-107678 | FFmpeg 9.0.2, MOV demuxer | Affected, no fix released | A crafted MP4 crashes ffprobe. One file fails, the app keeps running. Update to 9.0.3 when released |
| 2026-10-23 | CVE-2026-107677 | FFmpeg 9.0.2 | Under investigation | Denial of service, component not yet clear. Update to 9.0.3 when released |
| 2026-10-23 | CVE-2026-107676 | FFmpeg 9.0.2, HDR10+ serializer | Low | Applies to remuxing HDR metadata. The app only extracts audio |
| 2026-10-23 | CVE-2026-107696 | FFmpeg 9.0.2, RTSP/RTP | Not affected | Built with `--disable-network` |
| 2026-10-23 | CVE-2026-107675 | FFmpeg 9.0.2, libssh | Not affected | libssh not linked |
| 2026-10-23 | Supply chain | Whisper model | Open | Not pinned to a revision |

## Updating this file

After building the bundle, `python3 scripts/build_sbom.py` prints the versions it finds. By hand:

```
cd "build/Local Transcribe for Iconik.app/Contents/Resources"
find . -maxdepth 7 -type d -name "*.dist-info" | sed 's#.*/##; s/\.dist-info$//' | sort
strings python/lib/libpython3.12.dylib | grep -m1 -E "OpenSSL [0-9]"
bin/ffmpeg -version | head -1
```

Python packages: `uvx pip-audit -r <list> --no-deps --disable-pip`. Native components: ffmpeg.org/security.html, the Debian security tracker for ffmpeg, python.org release notes, openssl-library.org/news/vulnerabilities and oss-security for Expat.
