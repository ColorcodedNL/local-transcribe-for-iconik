# Local Transcribe for Iconik

> [!IMPORTANT]
> **New in 0.3.2: several Macs can share the queue, and a fix for jobs that hang after sleep.** Install this update if you use remote jobs.
>
> - **Several Macs:** if a Mac goes silent for four minutes, another Mac takes its asset over. Tested with two Macs.
> - **A whole collection at once:** select it in Iconik, choose Update Metadata and set the transcription field to Requested.
> - **Queue values are now English.** Values from 0.3.1 are converted automatically.
> - **Fixed:** after a Mac went to sleep and the connection or storage dropped, a job could stay on "busy" for ever and that Mac took no new work. A stalled job is now handed back to the queue and the app restarts itself.
>
> Installed apps offer the update in the banner. [Release notes](../../releases/tag/v0.3.2)

A Mac app that transcribes assets from [Iconik](https://www.iconik.io) on your own machine and writes the result back to Iconik as native transcriptions, with word timing and speaker labels.

You start it from Iconik itself: pick an asset, a collection or a selection, and choose **Local Transcribe**. The app reads the source file from your mounted storage, transcribes it locally and sends the transcript back. Nothing goes to a cloud transcription service.

![The app window with a finished job and its audio tracks](docs/images/app-jobs.png)

## Features

- **Native Iconik transcripts.** Written through the Iconik transcription API, so they show up under Transcriptions with speakers and word timing, like any other transcript in Iconik. No `.srt` sidecars.
- **Started from Iconik.** The app adds a "Local Transcribe" Custom Action to your Iconik environment when you connect it, for assets, collections and multi-selections.
- **Local models.** Whisper large-v3 (via MLX on Apple Silicon) for speech, pyannote community-1 for speaker detection. The speaker model is bundled; the Whisper model (about 3 GB) is downloaded once on first use.
- **Smart track selection.** For camera and multi-track files the app looks at every audio track: it skips silence, timecode and duplicate tracks, and when speakers each have their own microphone, it reads the speaker from the loudest track.
- **Language per asset.** Detected from the loudest parts of the recording, not just the first 30 seconds. Tested with Dutch and English.
- **Filters.** Removes Whisper hallucinations and repetition loops, and skips files without intelligible speech instead of writing nonsense.
- **Safe replacement.** Running it again replaces only the transcript this app made earlier. Transcripts from other sources are never touched, and the old transcript is only removed once the new one succeeded.
- **Efficient on collections.** While one asset is being transcribed, the audio of the next one is already read from the network storage.
- **Remote jobs (optional).** Request a transcript from the Iconik browser and let a Mac that is on in the background do it, without an extra Iconik login. Off by default. See [Remote jobs](#remote-jobs).
- **Updates from inside the app.** A banner offers new versions; most are a one-click install of a few dozen KB, signed and verified before anything is written.

![Local Transcribe in the Iconik asset menu](docs/images/iconik-menu.png)

![The transcript with speakers in Iconik](docs/images/iconik-transcript.png)

## Requirements

- A Mac with Apple Silicon, macOS 13 or newer
- An Iconik environment on `app.iconik.io`, and an Iconik admin for the one-time setup
- The Iconik storage (ISG) mounted on the Mac that runs the app

## Installation

1. Download the latest `.dmg` from [Releases](../../releases).
2. Drag **Local Transcribe for Iconik** to Applications.
3. The app is not signed with an Apple Developer ID yet, so macOS blocks it on first launch. Open **System Settings, Privacy & Security** and click **Open Anyway**.

The `.dmg` contains a step-by-step guide with screenshots, in English and Dutch. The same guides are in [`docs/`](docs/).

### One-time setup in Iconik

1. An Iconik admin creates an application under **Admin, Application tokens, New application**: type **Browser or device app**, redirect URI `http://localhost:8765/oauth/callback`, scopes `assets:read`, `assets:write`, `collections:read`, `user_impersonation`.
2. Enter the resulting Client ID in the app and sign in to Iconik.
3. Click **Add the button to Iconik** when the app asks. This creates the Custom Actions for everyone in that environment.

Colleagues skip steps 1 and 3: they install the app, enter the same Client ID and sign in with their own account.

## Updates

The app checks GitHub Releases every six hours. When a newer version exists, a banner appears at the top of the window.

- **Code updates** (most releases) are a small signed zip with the app's Python code. Click **Install**; the app downloads it, verifies an Ed25519 signature and a checksum per file, waits until no job is running, and restarts itself.
- **Full updates** (a new Python, new packages, a new ffmpeg or models) need the new `.dmg`. The banner then offers the download page instead.

Code updates are stored next to the app (`~/Library/Application Support/Local Transcribe for Iconik/code/`), never inside it, so the app's own code signature stays valid. If an update fails to start, the app notes that version as bad and starts again with the code that came in the app bundle. Nothing is installed without a valid signature from the key whose public half is built into the app (`app_shell.py`).

## Remote jobs

Not everyone has a Mac with enough memory for fast transcripts. With **Allow remote jobs** (Preferences) a Mac picks up requests made in the Iconik browser, from any location, and processes them in the background. The normal workflow, started from the Custom Action, does not change.

How it works: Iconik is the queue. The app creates two metadata fields, `local_transcribe_status` (values `requested`, `in_progress`, `done`, `failed`) and `local_transcribe_info`, and a metadata view called "Local Transcribe". Add the two fields to a metadata view your team uses. To request a transcript, set the status to Requested on an asset. For a whole collection, select it in Iconik, choose Update Metadata and set the transcription field to Requested: that applies to every item in it. The Mac checks once a minute, claims the asset, runs the usual engine and writes the outcome back.

The stored values and field names are always English, so Iconik support can read them. What people see are labels, in Dutch (Transcriptie: Aangevraagd, Bezig, Klaar, Fout) or English (Transcription: Requested, In progress, Done, Failed). Choose under **Language of the fields in Iconik** in Preferences (it applies to everyone in that Iconik environment and needs admin rights), or rename the labels in Iconik under Admin, Metadata Fields. A view that already contains the field keeps its own copy of the labels, so add the field to that view again. Version 0.3.1 stored Dutch values; those are converted when the app starts, and old requests are still picked up. The details text each Mac writes follows the language of its own app.

- It uses the Iconik account that is signed in on that Mac, so no extra licence is needed. It only sees assets that account can see, and transcripts appear under that account.
- Problems with the Mac (storage not mounted, not signed in, no internet) put the asset back in the queue, up to three attempts, then Failed with the reason. The Mac then pauses for ten minutes. Problems with the asset itself are Failed straight away. A Mac that is working refreshes a "last sign" every minute; if it stays silent for four minutes, another Mac takes the asset over. The last sign only refreshes while the job makes progress: a job that reports nothing for 15 minutes (longer for long audio), for example after the Mac slept and the storage dropped, is given back to the queue and the app restarts. The Mac that failed leaves that asset to another Mac for ten minutes.
- Claiming is a write, a short wait and a read back, because Iconik has no atomic operation. Tested with two Macs on one Iconik account: they split the queue without double work, and a Mac that is force-quit, goes to sleep or loses its storage hands its asset to the other. More Macs and separate Iconik accounts are not tested yet.
- With remote jobs on, the app lives in the menu bar and closing the window hides it. **Open at login** (separate, off by default) starts it hidden at login.
- The storage (ISG) must be mounted on that Mac.

## Privacy

The app connects to:

- **Iconik**, to read asset information and write transcripts, as the signed-in user
- **Hugging Face**, once, to download the Whisper model
- **GitHub**, to check whether a newer version is available and, when you click Install, to download it

Audio, transcripts and file names never leave your Mac except as the transcript written to your own Iconik environment. Usage telemetry of the bundled libraries (pyannote, Hugging Face) is switched off.

## How it works

```
Iconik (browser)  --Custom Action-->  iconiktranscribe:// link
                                              |
                                              v
                           Local Transcribe for Iconik (one app, one process)
                           - receiver on 127.0.0.1:8765, job queue
                           - finds the file on the mounted storage
                           - ffmpeg: reads and analyses all audio tracks
                           - Whisper large-v3 (MLX) + pyannote (PyTorch, Metal)
                                              |
                                              v
                           Iconik transcription API (properties + segments)
```

The app is Python with a small native launcher, so it runs as a single Mac app with its own name and icon in the Dock and in Activity Monitor. The window is a WebKit view (pywebview) on the local receiver.

## Building from source

```
bash scripts/build_python.sh    # relocatable Python 3.12 with all packages in build/python (needs uv)
bash scripts/build_ffmpeg.sh    # LGPL build of ffmpeg and ffprobe in build/ffmpeg-out
bash scripts/build_bundle.sh    # build/Local Transcribe for Iconik.app
bash scripts/build_dmg.sh       # dist/Local Transcribe for Iconik.dmg with the guides
bash scripts/build_update.sh    # dist/code-<version>-shell<N>.zip and .sig, signed with your update key
```

`scripts/build_bundle.sh` expects the pyannote speaker model in `~/Library/Application Support/Local Transcribe for Iconik/models/speaker-diarization-community-1`. Download it from [Hugging Face](https://huggingface.co/pyannote/speaker-diarization-community-1) (you need to accept its terms there).

| File | Purpose |
| --- | --- |
| `main.py` | Boot: picks the bundled code or the newest verified code update, falls back if it fails. Rarely changes |
| `app_shell.py` | Fixed layer: shell version and the public update key. Rarely changes |
| `app_main.py` | The app: receiver, job queue, window, menu bar, Iconik links, quitting, restart after an update |
| `updater.py` | Finds, downloads, verifies and installs code updates |
| `serve.py` | Local receiver, job queue, sign-in, Custom Action check, remote-jobs switch, open at login |
| `worker.py` | Remote jobs: the Iconik queue (fields, claim, retries, pause) |
| `transcribe.py` | The engine: audio tracks, Whisper, speakers, filters, writing to Iconik. Also usable from the command line |
| `ui.html` | The window (jobs and preferences), in English and Dutch |
| `scripts/` | Build scripts, and `launcher.c`, the native launcher that loads the bundled Python in the app process |
| `docs/` | The guides (PDF), `RELEASING.md` and `THIRD_PARTY_NOTICES.md` |
| `setup_actions.py` | Create the Custom Actions from the command line |
| `tools/` | Measuring tools for audio tracks and assets, update signing (`sign_update.py`), `make_manifest.py`, `local_update_test.py` |
| `tests/` | Tests for the boot layer, the updater, the update state in `serve.py`, the remote-jobs queue and its switch, stalled jobs, the saved job list |

Logs: `~/Library/Logs/Local Transcribe for Iconik/serve.log`. Each job logs time and memory per stage.

## Roadmap

- Test the shared queue with more than two Macs and with separate Iconik accounts, so a whole archive can be transcribed in parallel
- Keep the history of a taken-over asset in the details field, and let a Mac check that the asset is still its own before it writes a result
- Keep the list of jobs across restarts
- A one-click button in Iconik for remote jobs (needs an endpoint Iconik can reach)
- Sign and notarize the app, which would allow fully automatic updates (Sparkle)
- Native Mac interface in Swift
- Configurable Iconik host for environments other than `app.iconik.io`

## Credits and licenses

This project is released under the [MIT license](LICENSE). It builds on Whisper (OpenAI), MLX and mlx-whisper (Apple), pyannote (pyannoteAI), PyTorch, ffmpeg and pywebview. See [THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md).

Not affiliated with or endorsed by Iconik or Backlight. Iconik is a trademark of its owner.

Made by [Bastiaan van Oorde](https://github.com/ColorcodedNL), developed with AI assistance.
