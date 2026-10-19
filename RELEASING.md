# Releasing

Two kinds of release, depending on what changed.

## Code release (most of them)

Changes in `app_main.py`, `serve.py`, `transcribe.py`, `updater.py`, `worker.py`, `ui.html` or `setup_actions.py`.

1. Raise `APP_VERSION` in `transcribe.py`, and update the "New in" block at the top of `README.md`.
2. Run the tests (see below).
3. Commit.
4. `bash build_update.sh` makes `dist/code-<version>-shell<N>.zip` and `.sig`. It refuses to sign with a key that does not match `PUBLIC_KEY` in `app_shell.py`.
5. Write `dist/notes-<version>.md`.
6. `gh release create v<version> dist/code-<version>-shell<N>.zip dist/code-<version>-shell<N>.zip.sig --notes-file dist/notes-<version>.md`
   Add the `.dmg` as well (`bash build_bundle.sh && bash build_dmg.sh`) so new users get the latest from the release page.

Installed apps offer the update within six hours, or at the next start.

## Full release

Needed when something outside the code zip changes: the bundled Python or packages, ffmpeg, the models, `launcher.c`, `main.py` or `app_shell.py`.

1. Do the steps above and also raise `SHELL_VERSION` in `app_shell.py`.
2. Build and attach the `.dmg`.

Apps with a lower shell version only see a link to the release page, not an Install button. Raise `SHELL_VERSION` only when you must: every raise sends all users to the dmg.

## Update key

`tools/sign_update.py keygen` makes the key pair once. Keep the private key in a password manager. The public half is in `app_shell.py`. Losing the private key means shipping a new dmg with a new public key. If the private key leaks, do the same, and treat every update signed since as suspect.

## Tests

```
python3 tests/test_boot.py
uv run --with pynacl --with requests python tests/test_updater.py
uv run --with pynacl --with requests python tests/test_serve_update.py
uv run --with pynacl --with requests python tests/test_worker.py
uv run --with pynacl --with requests python tests/test_serve_worker.py
uv run --with pynacl --with requests python tests/test_stall.py
```

On a Mac, `uv run tools/local_update_test.py good|syntax|crash` installs a locally signed test update, so you can watch the app pick it, or fall back. `reset` removes installed updates.
