# Third-party notices

The source code in this repository is MIT licensed (see `LICENSE`). The app bundle and the `.dmg` in Releases also contain the following third-party software and models, each under its own license.

| Component | Used for | License |
| --- | --- | --- |
| [Whisper large-v3](https://github.com/openai/whisper) (OpenAI), MLX conversion by [mlx-community](https://huggingface.co/mlx-community/whisper-large-v3-mlx) | Speech recognition (downloaded on first use) | MIT |
| [MLX](https://github.com/ml-explore/mlx) and [mlx-whisper](https://github.com/ml-explore/mlx-examples) (Apple) | Running Whisper on Apple Silicon | MIT |
| [pyannote.audio](https://github.com/pyannote/pyannote-audio) | Speaker detection | MIT |
| [pyannote speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) (pyannoteAI) | Speaker detection model, bundled | CC-BY-4.0 |
| [PyTorch](https://pytorch.org) | Running the speaker model | BSD-3-Clause |
| [FFmpeg](https://ffmpeg.org) 9.0.2 (ffmpeg, ffprobe) | Reading audio tracks | LGPL 2.1 or later. Built unmodified from the official source with `scripts/build_ffmpeg.sh`; license text in `licenses/` (next to this file) |
| [pywebview](https://pywebview.flowrl.com) | The app window | BSD-3-Clause |
| [PyObjC](https://github.com/ronaldoussoren/pyobjc) | macOS integration | MIT |
| [keyring](https://github.com/jaraco/keyring) | Storing the Iconik sign-in in the macOS keychain | MIT |
| [Requests](https://requests.readthedocs.io) | Iconik API | Apache-2.0 |
| [Python](https://www.python.org) 3.12 | Runtime | PSF License |

Other Python packages in the bundle are dependencies of the above; their license files are included in the bundle under `Contents/Resources/python/lib/python3.12/site-packages/*.dist-info`.

The speaker model is used under CC-BY-4.0: "pyannote speaker-diarization-community-1" by pyannoteAI. The app shows this attribution under Preferences, Models.
