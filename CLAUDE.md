# Working on Local Transcribe for Iconik

Notes for an AI coding session in this repo. The README says what the app does; `docs/RELEASING.md` is the release procedure; `docs/SBOM.md` lists what ships.

## Layout

- `app/` is the app. Everything in `CODE_FILES` in `tools/make_manifest.py` can go out as a small signed code update. `main.py` and `app_shell.py` are the fixed shell: they start the app and verify updates, and only change with a new dmg and a higher `SHELL_VERSION`. Touch them only when there is no other way.
  - `transcribe.py`: the fixed entrance to the engine parts below, and the command line. Holds `APP_VERSION` (the build scripts read it there). Reading `transcribe.X` gives the live value from the part where X lives; `transcribe.X = ...` sets it in every part that knows X, so tests and `serve.py` keep working. New code imports the part itself
  - `base.py` (names, paths, log, progress, language, errors, config), `iconik_api.py` (login, client, ISG mounts, find the media file), `audio.py` (ffmpeg, track analysis, waveforms), `speakers.py`, `filters.py`, `whisper_mlx.py` (the first engine: Whisper on MLX, pinned model), `engine.py` (the list of engines and the fixed form every engine has; nothing outside `whisper_mlx.py` touches MLX), `pipeline.py` (one job from start to end). A part only imports from parts earlier in this order; `tests/test_split.py` checks that
  - `system.py`: what differs per operating system, under fixed names (paths, process memory, the room check for auto transcribe, mounted volumes, a folder picker, keeping awake, opening a link, start at login, the computer name). `platform_mac.py` is the only implementation; a Windows or Linux one gets the same names and a line in `IMPLEMENTATIONS`. Outside `platform_mac.py` and the Mac shell (`app_main.py`, `main.py`, `app_shell.py`) nothing calls macOS directly; `tests/test_split.py` checks that
  - `iconik.py`: the Iconik connector (open, find media, write, read). `worker.py`: the queue in Iconik for remote jobs and auto transcribe
  - `serve.py`: the local receiver on 127.0.0.1:8765 and the job list. `ui.html`, `refine.html`: the window. `refine.py`: the editor's backend. `transcript.py`: the own transcript model. `subtitles.py`: subtitles as a second layer that someone makes on purpose in Refine (dividing by rules, fitting, warnings, .srt and .vtt files)
  - `app_main.py`: the Mac app around it (window, menu bar, links from Iconik). `updater.py`: code updates
- `scripts/`: build scripts and probes. `tools/`: signing, manifest, test helpers. `tests/`: one script per area, each prints OK or FOUT lines and exits non-zero on a failure.
- Code comments, log lines and test names are Dutch. Docs, commit messages and release notes are English. Every text a user sees exists in Dutch and English through `transcribe.L(dutch, english)` or the `TEXT` table in the html files.

## Rules that came out of earlier work

- New files in `app/` that the app needs go into `CODE_FILES` (and a new engine part into `PARTS` in `transcribe.py`), or a code update leaves them out. Code updates only know flat file names, no folders.
- Code in a code update also runs on apps with an older shell. Reach for something new in `app_shell` with `getattr` and keep a fallback.
- Everything on the receiver needs a session (cookie of the app's own window, or the listener key). A route that changes something goes into `PROTECTED` and is called as a POST with the `X-Local-Transcribe` header. New window behaviour gets a step in `tests/test_e2e.py`, or in `tests/test_refine_window.py` for Refine.
- Never overwrite a transcript from another source or a correction made in Refine.
- The line break and the warning level of a subtitle are calculated twice with the same choices: `subtitles.layout` and `subtitles.warnings` (the file and the counts) and `layoutLines` and `check` in `refine.html` (the screen). Change one, change the other; `tests/test_refine_window.py` compares them.
- Subtitles only exist when someone makes them (+ Subtitles in Refine). Nothing writes a subtitle file by itself, and a file the app did not write, or that changed afterwards, is only replaced when someone picked that path in the save panel.
- No own machine names, paths or account names in the app, tests, messages or screenshots: use generic terms such as "opslag (ISG)".
- Tests never touch the real keychain, config or support folder. Code that tests replace by name stays reachable under that name (for example `resources.memory`, `transcribe.mount_reachable`), also after it moves.
- No special builds for the developer's Mac: test builds are ad hoc signed like releases, so they behave like what users get.
- Measure on real material before building on an assumption.

## Tests

- All: `bash scripts/run_tests.sh` (needs `uv`; the browser test needs Chromium once: `uv run --no-project --with playwright playwright install chromium`, or `LT_CHROMIUM=/path/to/chrome`).
- One: `uv run --no-project --with pynacl --with requests --with numpy --with huggingface_hub python -B tests/test_worker.py`
- The tests replace MLX, torch and the keychain, so they run on Linux too. What they cannot show: the real window (WebKit), the menu bar, the build scripts and a real job. Those need a build on a Mac: `bash scripts/build_bundle.sh && open "build/Local Transcribe for Iconik.app"`, log in `~/Library/Logs/Local Transcribe for Iconik/serve.log`.

## Working through a remote session

- When the repo is reached through a bridge that cannot delete files, edit in place (read, replace, write) and leave git commits, merges and pushes to the owner in their own terminal: git needs to remove its lock files.
- Building, signing and releasing happen on the owner's Mac. Publishing is always the owner's own step.
