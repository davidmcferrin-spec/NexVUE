# AGENTS.md — NexVUE

Project context, architecture, and conventions live in `CLAUDE.md` and
`README.md`. Read those first. The section below is specific to running this
repo inside a Cursor Cloud VM.

## Cursor Cloud specific instructions

### What can and cannot run here

NexVUE is a hardware-bound edge gateway: the real product path is
DeckLink SDI capture (or SRT ingest) → GStreamer + Intel Quick Sync encode →
MediaMTX → WebRTC/WHEP. The cloud VM has **no DeckLink card, no Intel iGPU,
and MediaMTX/GStreamer are not installed**, so the live video pipeline
(`nexvue-encode.sh`, `nexvue-status-server.py`, `nexvue-metrics-server.py`,
the `decklink-*` C++ helpers, `mediamtx`) cannot actually stream here. Don't
try to run the encode pipeline against real hardware or expect video to play.
What *is* fully exercisable: the automated test suite, the PHP auth/web layer,
and the PHP web front door served by a dev server.

The C++ helpers (`make`) need the license-gated Blackmagic DeckLink SDK at
`/opt/decklink-sdk`, which is not present — `make` is expected to fail with
"DeckLinkAPI.h not found". This is normal in the cloud VM.

### Tests

- Python (stdlib only — **no pip, ever**; see `CLAUDE.md` conventions):
  `python3 test/test_<name>.py`. Run them all with a simple loop over
  `test/test_*.py`.
- Bash: `bash test/test-*.sh` (pipeline-assembly stubs GStreamer via
  `test/stubbin/` on `PATH` — no real `gst-launch` needed).
- Several Python tests shell out to the `php` CLI and **skip** when it is
  absent; with `php` installed they run for real. The update script installs
  `php-cli` + `php-sqlite3` so they don't silently skip.
- Known pre-existing failures on `main` (stale tests referencing rolled-back /
  older behavior, **not** environment problems — do not "fix" the environment
  for these): `test/test_nexvue_global_env.py` (expects
  `nexvue-supervisor.py` in the `nexvue-encode@.service` ExecStart, but Phase
  1.5 was rolled back to `nexvue-encode.sh`), `test/test_nexvue_status_bind.py`
  and `test/test_nexvue_auth.py::test_share_token_persisted_same_url` (both
  assert legacy `multiview.html`/`?t=` behavior superseded by the 2.0 path
  front door).

### Lint

No dedicated linter config. Use the language syntax checkers:
`php -l <file>` for `*.php`, `python3 -m py_compile <files>` for `*.py`,
`shellcheck -S error *.sh test/*.sh` for shell.

### Running the web front door (dev)

Production serves the UI through Apache + mod_php via `setup.sh` (DocumentRoot
`{webroot}/public`, pages under `pages/`, assets under `public/assets/`).
`setup.sh` targets a real station (systemd, sudoers, `/etc/nexvue`,
`/var/lib/nexvue`) and should **not** be run in the cloud VM. To exercise the
web UI here, mirror that layout in a scratch dir and use PHP's built-in server:

- Copy `public/index.php`, `nexvue-web-router.php`, the `nexvue-*.php` handlers
  + `nexvue-auth-lib.php` + `VERSION` into an app root; the `*.html` pages into
  `pages/`; the browser `*.js` + `chart.umd.min.js` into `public/assets/`.
- Point the auth store at writable paths via env: `NEXVUE_AUTH_DIR`,
  `NEXVUE_AUTH_DB`, and `NEXVUE_STATION_ENV` (a touchable file — otherwise the
  publish-JWT persist step warns about `/etc/nexvue/nexvue.env`). Then
  `php nexvue-auth-bootstrap.php` seeds the SQLite store, keypair, and JWKS.
- Serve with `php -S 127.0.0.1:<port> -t <approot>/public <approot>/public/index.php`.
- **Gotcha:** under PHP's built-in server the router (`index.php`) receives
  *every* request, including static `/assets/*.js`, and returns 404 for them
  (in production Apache serves those files directly). For a dev run, the copied
  `public/index.php` must short-circuit static files, e.g. when
  `php_sapi_name() === 'cli-server'` and the path matches a static extension,
  `return false;` so the built-in server serves the file as-is. Do **not** make
  this change in the repo's `public/index.php` — it is only for the dev copy.
- First login: user `admin`, password `password`, `must_change_password` is set
  so the first action must be `change_password` (min 8 chars, not `password`).
