# Releasing

Two kinds of release, depending on what changed.

## Code release (most of them)

Changes in `app/app_main.py`, `serve.py`, `transcribe.py`, `updater.py`, `worker.py`, `ui.html` or `setup_actions.py` (all in `app/`).

1. Raise `APP_VERSION` in `app/transcribe.py`, and describe the changes in the release notes. The highlight block at the top of `README.md` only changes when a release changes what it says.
2. Do the checks below.
3. Commit.
4. `bash scripts/build_update.sh` runs all tests first, then makes `dist/code-<version>-shell<N>.zip` and `.sig`. It refuses to build when a test fails, and refuses to sign with a key that does not match `PUBLIC_KEY` in `app/app_shell.py`.
5. Write `dist/notes-<version>.md`.
6. Build the dmg as well, so new users get the latest from the release page: `bash scripts/build_bundle.sh && bash scripts/build_dmg.sh`. Start the app once from `build/` before publishing. The dmg comes out as `dist/Local Transcribe for Iconik.dmg`; copy it to `dist/Local-Transcribe-for-Iconik-<version>.dmg`.
7. `gh release create v<version> dist/Local-Transcribe-for-Iconik-<version>.dmg dist/code-<version>-shell<N>.zip dist/code-<version>-shell<N>.zip.sig --notes-file dist/notes-<version>.md`

Installed apps offer the update within six hours, or at the next start.

## Full release

Needed when something outside the code zip changes: the bundled Python or packages, ffmpeg, the models, `scripts/launcher.c`, `app/main.py` or `app/app_shell.py`.

1. Do the steps above and also raise `SHELL_VERSION` in `app/app_shell.py`. Then the code zip is named `code-<version>-shell<N>.zip` and apps with an older shell are offered the dmg instead.
2. Say in the release notes that a code update does not carry packages, so older apps need the dmg.

Apps with a lower shell version only see a link to the release page, not an Install button. Raise `SHELL_VERSION` only when you must: every raise sends all users to the dmg.

## Checks before every release

- Tests: `bash scripts/run_tests.sh`. `build_dmg.sh` and `build_update.sh` run it themselves and refuse to build when a test fails (`SKIP_TESTS=1` only in an emergency). GitHub Actions runs the same tests on every push.
- New behaviour in the window (a link, a button, a route) gets a step in `tests/test_e2e.py`, the browser test against a fake Iconik. The login bug of 0.3.1 to 0.3.4 slipped through because no test clicked the login link.
- Dependencies: `uv export --no-hashes --no-emit-project -o /tmp/req.txt && uvx pip-audit -r /tmp/req.txt --no-deps`, and upgrade what it finds with `uv lock --upgrade-package <name>`. pip-audit only covers PyPI advisories, not the bundled ffmpeg or the OpenSSL in the bundled Python.
- certifi: the app uses its own CA list, so a browser blocking a CA does not protect it. After a CA incident, grep `cacert.pem` in the built bundle.
- ffmpeg: compare the bundled version with the newest tag, `git ls-remote --tags --refs https://git.ffmpeg.org/ffmpeg.git 'n9.0*' | sed 's|.*refs/tags/||' | sort -V | tail -3`. A newer 9.0.x means rebuilding with `scripts/build_ffmpeg.sh`, updating the version in the `ffmpeg-README.txt` text in `build_bundle.sh`, and a full release.
- After building: version and SHELL_VERSION in the bundle, the packages that matter, the dmg checksum, and a quick start of the app from `build/`.

## Tests

`bash scripts/run_tests.sh` runs every `tests/test_*.py`. Once per Mac, for the browser test: `uv run --no-project --with playwright playwright install chromium`. A single test:

```
uv run --no-project --with pynacl --with requests --with numpy --with playwright python -B tests/test_e2e.py
```

On a Mac, `uv run tools/local_update_test.py good|syntax|crash` installs a locally signed test update, so you can watch the app pick it, or fall back. `reset` removes installed updates.

## Update key

`tools/sign_update.py keygen` makes the key pair once. Keep the private key in a password manager. The public half is in `app/app_shell.py`. Losing the private key means shipping a new dmg with a new public key. If the private key leaks, do the same, and treat every update signed since as suspect.
