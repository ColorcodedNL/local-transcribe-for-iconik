# Working on Local Transcribe for Iconik

Notes for an AI coding session in this repo. The README says what the app does; `docs/RELEASING.md` is the release procedure; `docs/SBOM.md` lists what ships.

## Layout

- `app/` is the app. Everything in `CODE_FILES` in `tools/make_manifest.py` can go out as a small signed code update. `main.py` and `app_shell.py` are the fixed shell: they start the app and verify updates, and only change with a new dmg and a higher `SHELL_VERSION`. Touch them only when there is no other way.
  - `transcribe.py`: the engine (track analysis, Whisper, speakers, filters) and, for now, also the Iconik login, the client and the config
  - `iconik.py`: the Iconik connector (open, find media, write, read). `worker.py`: the queue in Iconik for remote jobs and auto transcribe
  - `serve.py`: the local receiver on 127.0.0.1:8765 and the job list. `ui.html`, `refine.html`: the window. `refine.py`: the editor's backend. `transcript.py`: the own transcript model. `subtitles.py`: subtitles as a second layer that someone makes on purpose in Refine (dividing by rules, fitting, warnings, .srt and .vtt files)
  - `app_main.py`: the Mac app around it (window, menu bar, links from Iconik). `updater.py`: code updates
- `scripts/`: build scripts and probes. `tools/`: signing, manifest, test helpers. `tests/`: one script per area, each prints OK or FOUT lines and exits non-zero on a failure.
- Code comments, log lines and test names are Dutch. Docs, commit messages and release notes are English. Every text a user sees exists in Dutch and English through `transcribe.L(dutch, english)` or the `TEXT` table in the html files.

## Rules that came out of earlier work

- New files in `app/` that the app needs go into `CODE_FILES`, or a code update leaves them out. Code updates only know flat file names, no folders.
- Code in a code update also runs on apps with an older shell. Reach for something new in `app_shell` with `getattr` and keep a fallback.
- Everything on the receiver needs a session (cookie of the app's own window, or the listener key). A route that changes something goes into `PROTECTED` and is called as a POST with the `X-Local-Transcribe` header. New window behaviour gets a step in `tests/test_e2e.py`, or in `tests/test_refine_window.py` for Refine.
- Never overwrite a transcript from another source or a correction made in Refine.
- The line break and the warning level of a subtitle are calculated twice with the same choices: `subtitles.layout` and `subtitles.warnings` (the file and the counts) and `layoutLines` and `check` in `refine.html` (the screen). Change one, change the other; `tests/test_refine_window.py` compares them.
- Subtitles only exist when someone makes them (+ Subtitles in Refine). Nothing writes a subtitle file by itself, and a file the app did not write, or that changed afterwards, is only replaced when someone picked that path in the save panel.
- No own machine names, paths or account names in the app, tests, messages or screenshots: use generic terms such as "opslag (ISG)".
- Tests never touch the real keychain, config or support folder.
- No special builds for the developer's Mac: test builds are ad hoc signed like releases, so they behave like what users get.
- Measure on real material before building on an assumption.

## Tests

- All: `bash scripts/run_tests.sh` (needs `uv`; the browser test needs Chromium once: `uv run --no-project --with playwright playwright install chromium`, or `LT_CHROMIUM=/path/to/chrome`).
- One: `uv run --no-project --with pynacl --with requests --with numpy --with huggingface_hub python -B tests/test_worker.py`
- The tests replace MLX, torch and the keychain, so they run on Linux too. What they cannot show: the real window (WebKit), the menu bar, the build scripts and a real job. Those need a build on a Mac: `bash scripts/build_bundle.sh && open "build/Local Transcribe for Iconik.app"`, log in `~/Library/Logs/Local Transcribe for Iconik/serve.log`.

## Working through a remote session

- When the repo is reached through a bridge that cannot delete files, edit in place (read, replace, write) and leave git commits, merges and pushes to the owner in their own terminal: git needs to remove its lock files.
- Building, signing and releasing happen on the owner's Mac. Publishing is always the owner's own step.
