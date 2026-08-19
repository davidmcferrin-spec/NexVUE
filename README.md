# NexVUE — Edge Node (Phase 1)

**NexVUE** — self-hosted SDI-to-WebRTC return-feed and remote-monitoring
gateway (sibling of NexAlert). One edge node per station:
DeckLink capture card (4 or 8x 3G-SDI in) -> GStreamer (deinterlace +
Quick Sync H.264 + Opus) -> MediaMTX -> WHEP (WebRTC) to any browser.

Supported capture cards (channel count set by station-wide `MAX_DEVICES` in
`/etc/nexvue/nexvue.env`):
DeckLink Quad 2 (8 ch), Duo 2 / Duo 2 Mini (4 ch), original Duo (2 ch). The
code is card-agnostic — you enable one `nexvue-encode@N` service per input and
stop; a Duo 2 uses instances 0-3.

Phase 1 scope: single node, LAN only, no TLS, no auth. Proves ingest,
encode stability, and latency numbers before the portal (Phase 2) and DMZ
exposure (Phase 3) are built.

```
SDI 1080i59.94 (4 or 8) --> [DeckLink card]
                        |  per channel (systemd template unit):
                        |  decklinkvideosrc -> deinterlace -> vah264enc (QSV)
                        |  decklinkaudiosrc -> opusenc
                        v
                     RTSP publish (loopback only)
                        v
                    [MediaMTX] --WHEP--> browser  (~200-400 ms glass-to-glass)
```

## Hardware (vetted reference BOM, ~$2,600/station)

- HP ProDesk 4 Tower G1i (Core Ultra 5 235, Q870) — SKU C57BMUT#ABA or any
  G1i Tower sibling; **Tower, not SFF** (the card is full height)
- **Second 16GB DIMM** — the stock 1x16GB is single-channel; the iGPU media
  engine shares that bandwidth with 8-channel deinterlace. Cheap insurance.
- Blackmagic DeckLink Quad 2 in the PCIe 4.0 x16 slot (card is Gen2 x8).
  **Duo 2 (4 ch)** works identically — set `MAX_DEVICES=4` in
  `/etc/nexvue/nexvue.env` — `setup.sh` then enables only `@0..3` and disables
  `@4..7`. The Duo 2 Mini
  (low-profile) is the pick if the chassis only takes half-height cards
  (e.g. an SFF box).
- DIN 1.0/2.3-to-BNC breakout cables, one per channel (8 for Quad 2, 4 for
  Duo 2) — the cards have mini connectors, NOT full-size BNC; easy to leave
  off the PO, painful to be missing
- Ubuntu 24.04 LTS Server
- Optional: HP Care Pack to 3yr for unattended remote sites (base is 1/1/1)

Capacity guidance: 8x 1080p59.94 HI encodes (plus optional LO tees on any
channel) is near the practical ceiling for
the Arrow Lake media engine. SRT channels add decode load on the same
Video engine — prefer HI-only for add-on SRT slots. Channel slots default
to `MAX_CHANNELS=8` (0–7), matching Quad 2 `MAX_DEVICES`. Duo 2 stations
still use `MAX_DEVICES=4` and enable only `@0..3`.
Run motion-critical channels (program, director) at 59.94p (`DEINT_FIELDS=all`)
and monitoring channels (multiview, prompter) at 29.97p (`DEINT_FIELDS=top`) to
cut encode load and stay comfortably inside the media-engine budget.
Deinterlace quality is per-channel via `DEINT_METHOD` (default `yadif`; prior
hard-coded choice was `greedyh`). Soft options `vfir` / `linear` hide combing
at the cost of vertical detail.

## Install

**Preferred:** from the repo root as root, `sudo ./setup.sh` installs packages,
MediaMTX, systemd units, ops sudo wrappers + `/etc/sudoers.d/nexvue-ops`,
seeds missing `/etc/nexvue/channels/N.env` for each `MAX_CHANNELS` slot,
creates self-signed TLS under `/etc/nexvue/tls/` when absent (Apache HTTPS +
MediaMTX WHEP/API share the same PEMs), and `enable --now`s `mediamtx`,
`nexvue-status`, `nexvue-metrics`, `nexvue-decklink-configure`, and
`nexvue-encode@0..(MAX_CHANNELS-1)` (default 8 → `@0`–`@7`; Duo: set
`MAX_CHANNELS=4` in `nexvue.env`). Empty SDI ports auto-park. When
`/var/www/html` exists, it also installs the Apache path UI (`/login`,
`/player`, …). Use `sudo ./setup.sh --check` after a reboot.
`setup.sh` also enables `apache2` + `ssh`, keeps Apache HTTP `:80` open as a
redirect-only front door (`Listen` + `000-default` ensured, real content
stays HTTPS-only), allows OpenSSH / 80 / 443 / 8889 / 8189 in ufw, and
enables ufw. `--firewall` is a legacy alias for that ufw step.

Manual steps below match what `setup.sh` does if you prefer to run them by hand.

### 1. OS packages

**Arrow Lake (Core Ultra 200S) requires the HWE kernel** — the 24.04 GA
kernel/media stack predates the platform:

```bash
sudo apt update && sudo apt install -y linux-generic-hwe-24.04
sudo reboot
```

Then:

```bash
sudo apt update
sudo apt install -y \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  gstreamer1.0-rtsp \
  intel-media-va-driver-non-free vainfo \
  apache2 libapache2-mod-php php-cli php-sqlite3
```

(`setup.sh` installs these automatically — including `gstreamer1.0-rtsp`
for `rtspclientsink`; Apache + PHP for login/auth, metrics, and ops;
`php-sqlite3` for SQLite readers.)

Verify Quick Sync is visible (expect H264 encode entrypoints under iHD driver):

```bash
vainfo | grep -i h264
```

If vainfo shows no encode entrypoints on Arrow Lake, the repo media driver is
too old — install `intel-media-va-driver-non-free` from Intel's own apt
repository (or the kisak PPA) and re-check. `nexvue-encode.py` fails loudly
with a pointer to this if `vah264enc` is missing.

### 2. Blackmagic Desktop Video

Download "Desktop Video" for Linux from Blackmagic's support site (deb package),
then:

```bash
sudo dpkg -i desktopvideo_*.deb && sudo apt -f install -y
sudo reboot
BlackmagicFirmwareUpdater status   # update if prompted, reboot again
```

The GStreamer `decklink*` elements load only when `libDeckLinkAPI.so` from this
package is present. Confirm: `gst-inspect-1.0 decklinkvideosrc`

### 3. MediaMTX

Grab the latest linux_amd64 release from
<https://github.com/bluenviron/mediamtx/releases> and:

```bash
sudo tar -C /usr/local/bin -xzf mediamtx_*_linux_amd64.tar.gz mediamtx
```

### 4. This package (encoder + status + metrics collector)

```bash
sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin nexvue

sudo mkdir -p /etc/nexvue/channels
sudo cp mediamtx.yml /etc/nexvue/
sudo cp nexvue-encode.sh nexvue-encode.py nexvue-encode-auto-park.sh nexvue-supervisor.py /usr/local/bin/ && sudo chmod 755 /usr/local/bin/nexvue-encode.sh /usr/local/bin/nexvue-encode.py /usr/local/bin/nexvue-encode-auto-park.sh /usr/local/bin/nexvue-supervisor.py
sudo cp nexvue-status-server.py /usr/local/bin/ && sudo chmod 755 /usr/local/bin/nexvue-status-server.py
sudo cp nexvue-captions-decode.py nexvue-captions-probe.sh /usr/local/bin/ && sudo chmod 755 /usr/local/bin/nexvue-captions-decode.py /usr/local/bin/nexvue-captions-probe.sh
sudo cp nexvue-metrics-server.py /usr/local/bin/ && sudo chmod 755 /usr/local/bin/nexvue-metrics-server.py
sudo cp mediamtx.service nexvue-encode@.service \
       nexvue-status.service nexvue-metrics.service /etc/systemd/system/

# Station-wide card size (install only if absent — setup.sh also migrates):
sudo cp -n nexvue-example.env /etc/nexvue/nexvue.env   # MAX_DEVICES=8; MAX_CHANNELS=8

# One env file per encode slot (setup.sh seeds these from channels-example.env):
sudo cp channels-example.env /etc/nexvue/channels/0.env
sudo nano /etc/nexvue/channels/0.env
#   (inline '# comments' and whitespace in the env file are fine —
#    the unit sources it through a shell)   # set DEVICE_NUMBER=0, CHANNEL_PATH=ch0

sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx nexvue-status nexvue-metrics
# Quad 2 default (MAX_CHANNELS=8): enable all slots; empty ports auto-park.
for n in $(seq 0 7); do sudo systemctl enable --now nexvue-encode@$n; done
# Duo 2: only @0..3 (and set MAX_CHANNELS=4 / MAX_DEVICES=4 in nexvue.env).
```

`setup.sh` performs the enable loop from `MAX_CHANNELS` automatically.

The metrics **collector** has no listening port — it only writes SQLite.
Reading it back is Apache + PHP (next step). See "Usage Metrics Dashboard".

### 5. Apache web UI (player / multiviewer / metrics / ops)

The node's web UI (pages, JS assets, PHP APIs) lives under [`web-node/`](web-node/)
in this repo — a separate [`web-portal/`](web-portal/) holds the central
portal's own web app, installed with `sudo ./setup.sh --portal` instead.

Drop the UI files into Apache's docroot (HTTPS `:443` — HTTP `:80` stays
open only to 301-redirect to `:443`, never to serve real content). Metrics
and ops PHP scripts must sit next to the HTML so relative `fetch()` paths
resolve:

Prefer `sudo ./setup.sh` — it installs Apache + mod_php, copies the full
web UI (including `login.html` / `nexvue-auth*.php`), bootstraps the auth
store as `www-data`, and installs ops sudoers. Manual docroot copy is only
for atypical layouts (`NEXVUE_WEBROOT`):

```bash
cd web-node
sudo cp index.html multiview.html metrics.html login.html users.html \
        nexvue-metrics.php nexvue-status.php nexvue-mediamtx-api.php \
        nexvue-captions.php \
        nexvue-auth.php nexvue-auth-lib.php nexvue-jwks.php nexvue-auth-gate.js \
        nexvue-captions.js nexvue-qr.js nexvue-ui.js nexvue-vu.js \
        nexvue-logo.php chart.umd.min.js \
        services.html channels.html nexvue-ops.php /var/www/html/
sudo systemctl restart apache2
```

Then open `https://<edge-ip>/login` (default bootstrap `admin` /
`password` — forced change on first login). Top nav → Player / Multiview /
Metrics / Services / Settings / Users. **Roles gate ops pages** (admin /
operator / sharer / viewer + named share links). Remove any Apache Basic Auth /
`.htaccess` AuthType — the app login replaces it. MediaMTX API (`:9997`) and
status (`:9998`) bind to loopback; Player/Multiview use same-origin PHP
proxies (`nexvue-mediamtx-api.php`, `nexvue-status.php`).

### Local authentication (edge)

- **Store:** `/var/lib/nexvue/auth/auth.db` + keypair/JWKS in the same
  directory (www-data owned — SQLite WAL sidecars must live there; legacy
  `/var/lib/nexvue/auth.db` is migrated by `setup.sh`). Login error
  `auth store unavailable` almost always means ownership/path — re-run
  `sudo ./setup.sh`.
- **Front door (2.0):** Apache `DocumentRoot` is `{webroot}/public`.
  `setup.sh` enables `mod_rewrite`, installs `nexvue-web.conf` (`RewriteRule`
  → `index.php` + `FallbackResource`), patches site `DocumentRoot` to
  `…/public`, reloads Apache, and smokes `/login` + `/api/version`. HTML is
  served only after a **server-side** session/share check
  (`nexvue-web-router.php`); pages live under `pages/` (not web-enumerable).
  Path UI: `/player`, `/multiview`, `/metrics`, `/settings`, `/services`,
  `/users`, `/login`. APIs: `/api/auth`, `/api/ops`, `/api/metrics`,
  `/api/status`, `/api/mediamtx`, `/api/captions`, `/api/logo`, `/api/version`.
  Legacy `*.html` / `nexvue-*.php` URLs 301/307 to the new paths.
  `nexvue-auth-gate.js` remains for role chrome + WHEP JWT. WHEP JWTs still
  enforce media access. Share links use `/player?t=` / `/multiview?t=` (or
  `/s/<token>`).
- **Station API key:** set `NEXVUE_API_KEY` (or legacy `NEXVUE_SYNC_KEY`) in
  `/etc/nexvue/nexvue.env`. Callers send `Authorization: Bearer <key>` or
  `X-NexVUE-Key: <key>` for `users_export` / `shares_export` / import and
  `api_ping` (portal discovery).
- **Roles:** `admin` (Users, Services, Settings, Metrics, watch, all shares),
  `operator` (Settings, Metrics, watch), `sharer` (UI label **Viewer+Share** —
  watch + create/revoke own shares from Player/Multiview Share), `viewer`
  (Player/Multiview only).
- **Share links:** Users page (admin) or Player/Multiview **Share**
  (`nexvue-share-ui.js`, admin/sharer). Named, one or more channels (Multiview
  shares capped at **4**, matching dual/quad panes), absolute expiry **or**
  duration (always required; no open-ended), revocable. Admin sees all tokens
  + creator; sharer sees own. URL form `/player?t=<token>` or
  `/multiview?t=<token>` (also `/s/<token>`) — raw token is stored so the
  **same URL** can be re-copied (or emailed) anytime while active; Create also
  auto-copies to the clipboard. Share viewers see remaining time next to
  `share:<name>` in the top nav. Opening a Multiview share **auto-tunes**
  panes from the token's channel list (Dual for 1–2, Quad for 3–4; order
  preserved from create). Admin can **Edit** name/channels/expiry (token URL
  unchanged) and **Delete** revoked/expired rows; sharers can Delete their own
  revoked/expired shares. Rows whose `expires_at` is more than **7 days** in
  the past are hard-deleted opportunistically on `shares_list` (no cron).
  Email uses `share_email` / `mail()` when an MTA is available
  (`NEXVUE_MAIL_FROM`), else falls back to `mailto:`.
- **Player audio defaults (first visit):** volume 20%, muted, VU meters off
  (`nexvue-vu.js` localStorage). Existing prefs unchanged.
- **LO defaults (new channel / factory):** `LO_ENABLE=true`, `LO_PRESET=360p`
  (existing station `.env` files unchanged until rewritten).
- **MediaMTX:** `authMethod: jwt`, JWKS at
  `http://127.0.0.1:9080/nexvue-jwks.php` (localhost-only Apache vhost from
  `setup.sh` — avoids HTTPS redirects breaking JWKS), short-lived viewer JWTs
  on WHEP (`?jwt=`), long-lived `NEXVUE_PUBLISH_JWT` for encoders. Control
  API remains JWT-excluded for same-box callers (ops kick, metrics,
  `nexvue-mediamtx-api.php`) and is bound to `127.0.0.1:9997`.
- **Sync-ready API** (no portal client yet): `/api/auth` actions
  `users_export` / `users_import` / `shares_export` / `shares_import` /
  `api_ping` with optional `since=` cursor; admin session **or**
  `Authorization: Bearer` / `X-NexVUE-Key` matching `NEXVUE_API_KEY`
  (or legacy `NEXVUE_SYNC_KEY`) in `/etc/nexvue/nexvue.env`.
- **Forgot password:** creates a one-time reset; emails via `mail()` when the
  user has an email; admins can always copy a reset link from Users.
- **Tests:** `python3 test/test_nexvue_auth.py`

### 6. Input status helper (signal/reference display in the player)

Requires the Blackmagic **DeckLink SDK** (separate download from Desktop
Video — "Desktop Video SDK" on the same support page). Use the **same major
version** as the installed Desktop Video driver (e.g. DV 16 + SDK 16 — DV 16
is current and preferred on the HWE kernel). Then:

```bash
make DECKLINK_SDK=/path/to/Blackmagic_DeckLink_SDK_16.x
sudo make install                       # -> /usr/local/bin/decklink-{status,audio-probe,configure}
/usr/local/bin/decklink-status          # sanity: JSON with your 8 inputs
sudo /usr/local/bin/decklink-configure --apply-inputs   # Duo/Quad BNCs → half-duplex
```

(`setup.sh` builds this automatically when the SDK is at `/opt/decklink-sdk`.
The status **daemon** unit itself is already installed in step 4.)

Optional but recommended — the player degrades gracefully (gray dots,
SDI input = `status unreachable`) if the status daemon isn't reachable
via `nexvue-status.php`.

## Firewall (ufw)

Ubuntu's `ufw` is default-deny once enabled, so the service ports must be
opened explicitly. Port/protocol map:

| Port      | Proto     | Scope        | Purpose                                   |
|-----------|-----------|--------------|-------------------------------------------|
| 22        | TCP       | operators    | SSH (`OpenSSH` ufw profile)               |
| 443       | TCP       | viewers      | Apache HTTPS UI (`/login`, `/player`, …)  |
| 8889      | TCP       | viewers      | WHEP signaling (HTTPS POST/PATCH/DELETE)  |
| 8189      | UDP + TCP | viewers      | WebRTC media (UDP) + ICE-TCP fallback     |
| 80        | TCP       | viewers      | Apache HTTP — redirect-only (301 to the same URL under `https://`); real content is never served here |
| 9997      | TCP       | **loopback** | MediaMTX API — do NOT open; Player uses `nexvue-mediamtx-api.php` |
| 9998      | TCP       | **loopback** | Status daemon — do NOT open; Player uses `nexvue-status.php` |
| —         | —         | (none)       | Metrics dashboard has NO port at all — the collector doesn't listen on anything; PHP reads its SQLite file directly and Apache serves it on 443. See Usage Metrics Dashboard section. |
| 8554      | —         | **loopback** | RTSP ingest — do NOT open (127.0.0.1 only)|

Two things people get wrong here: the WebRTC **media** port (8189) needs
**both UDP and TCP** — UDP carries the media, TCP is the fallback for
viewers on UDP-hostile networks — and it is a *different* port from the WHEP
signaling port (8889). Opening only 8889 gets you a session that negotiates
then plays nothing.

### Viewer ports (LAN or DMZ)

`apiAddress` is `127.0.0.1:9997` and `nexvue-status` binds `127.0.0.1:9998`
by default — do **not** open those in ufw. Player/Multiview stats and signal
dots go through Apache PHP proxies on 443. `setup.sh` applies these rules
and enables ufw (SSH first so you are not locked out). `--firewall` re-applies
the same step.

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp comment 'NexVUE Apache HTTP (redirects to HTTPS)'
sudo ufw allow 443/tcp comment 'NexVUE Apache HTTPS'
sudo ufw allow 8889/tcp comment 'NexVUE WHEP signaling'
sudo ufw allow 8189 comment 'NexVUE WebRTC media (UDP+TCP)'
sudo ufw --force enable
# Metrics has no port to open at all — the collector doesn't listen on
# anything; PHP reads its SQLite file directly, served by Apache on 443
# alongside everything else. See "Usage Metrics Dashboard".
# 9997/9998 are loopback-only — never allow them through the firewall.
# Port 80 only ever serves a 301 redirect to :443 — never real content.
sudo ufw status verbose
```

(`ufw allow 8189` with no proto opens both UDP and TCP, which is what the
media port needs.)

If an older install still has ufw rules for 9997/9998, delete them
(`sudo ufw status numbered` then `sudo ufw delete N`).

## Verify

```bash
systemctl status mediamtx nexvue-encode@0
journalctl -fu nexvue-encode@0
```

Then from a LAN machine:

- **Built-in player:** `https://<edge-ip>:8889/ch0`
- **Test player with stats:** open `/player` via Apache (top nav → Player),
  click a channel. Session tiles (resolution/fps, bitrate, RTT, loss, SDI
  input, …) live in a bottom drawer — click **Session metrics** to expand
  (collapsed by default). Hover a tile title for ~2s for a short explainer.
  Click the **NexVUE** brand for a QR code of the page URL (phone scan).
  **CC** toggles a selectable closed-caption overlay (CEA-608/CC1 side
  channel — not burned into video; preference in `localStorage`).
- **Multiviewer:** open `/multiview` (top nav → Multiview). Dual or quad
  layout with a channel dropdown per pane; defaults to LO with a global HI/LO
  toggle; click a pane for audio (one unmuted at a time) and to focus the
  **Session metrics** bottom drawer on that pane. Same NexVUE brand → QR
  share. Global **CC** toggle matches the player preference key.
  **⛶ Fullscreen** hides nav, control bar, pane borders, and per-pane
  channel selectors for a frameless wall; the session-metrics drawer stays
  hidden unless it was already open (Esc / button to exit).
- **Usage metrics:** top nav → Metrics (`/metrics` + `/api/metrics`
  in Apache docroot — no separate port).
- **Services:** top nav → Services — unit status + poll-based journal viewer
  (Follow / Clear view) plus **Clear journal…** for the selected unit
  (`journal_clear` watermark via `nexvue-ops-journal.sh` — hides prior lines
  for that unit only in the ops UI; not a host-wide vacuum).
  **Support bundle** (hours dropdown + **Download zip…**) builds a redacted
  zip of journals, `/etc/nexvue` channel/MediaMTX config, metrics samples, and
  small `/run/nexvue` state via `nexvue-ops.php?action=support_bundle` →
  `nexvue-ops-support-bundle.sh` → `nexvue-support-bundle.py` (zips land under
  `/var/lib/nexvue/support`, pruned after 24h).   **Update from repo…** fetches
  `origin/<branch>`, hard-resets the clone (`/etc/nexvue/repo.path`), and runs
  `setup.sh` (`nexvue-ops-update.sh` + sudoers). Status shows
  `vX.Y.Z · up to date` or `vX.Y.Z → vA.B.C · update available`; confirming an
  update lists the commit subjects between the running clone and origin.
  First enable requires one SSH `sudo ./setup.sh` from the clone so the helper
  is installed; after that the UI can self-update. Does not restart encoders.
  Top-nav shows **vX.Y.Z** from the `VERSION` file (`nexvue-version.php`).
  LAN-trust ops.
- **Settings:** top nav → Settings — optional station **logo** (Branding
  panel: upload/delete PNG/WebP/JPEG, stored under `/var/lib/nexvue/branding`,
  shown in the top nav next to **NexVUE** when present) plus channel list
  (LO column: yes/no; **Restart all encoders** for enabled slots);
  click a row (or use Bulk edit) to open a modal editor for
  `/etc/nexvue/channels/<N>.env`. Editor shows only live encode/player knobs
  (human labels): display name, HI video, audio on/off + player role + embed
  VU checkboxes + **Detect audio…**, LO on/off + preset, and a collapsed
  Advanced block. Unused supervisor/SRT/legacy keys are hidden (still wiped by
  **Factory defaults…**). Audio-on and LO-on reveal their dependent fields.
  Encode always publishes all 8 embeds when audio is on. Hover a field label
  ~2s for an explainer. Path stays `chN`. Save asks before restarting encoders.
  **Factory defaults…** writes concrete built-in defaults for every setting
  (alias cleared, LO off, audio on with 5.1+SAP role and all embeds, etc. —
  no blank/unset values in the form); `DEVICE_NUMBER` / `CHANNEL_PATH` /
  `RTSP_URL` are identity and never touched. Takes effect on the encoder
  restart offered after.
  Every page has a **Light/Dark** toggle (`localStorage.nexvue-theme`,
  default dark); shared helpers live in `nexvue-ui.js`.

### Closed captions (selectable overlay)

Browsers do not render CEA-608/708 carried inside WHEP H.264, and MediaMTX
does not convert captions to a text track. NexVUE therefore extracts captions
inside the existing encode pipeline and delivers timed text over Apache:

1. `decklinkvideosrc output-cc=true` → `ccextractor` → `ccconverter` → raw
   CEA-608 pairs on a FIFO under `/run/nexvue/captions/`. The `filesink`
   feeding the FIFO runs `buffer-mode=unbuffered` — its default mode holds
   ~64KB before flushing, and 608 trickles in at ~60–120 bytes/s, so a
   buffered sink starves the decoder for 10+ minutes and the overlay never
   shows anything.
2. `nexvue-captions-decode.py` (CC1) writes `<channel>.json` atomically.
   It emits at most **2 lines, newest at the bottom** (standard 608 roll-up
   presentation): the roll-up window is tracked per CEA-608 §8.4 (a PAC
   naming a new base row moves the window and erases rows left behind;
   entering roll-up from pop-on/paint-on erases the display), so stale
   broadcaster rows can never freeze on screen. The overlay CSS reserves a
   constant two-line box so the caption background doesn't resize per cue.
3. `nexvue-captions.php` streams cues as Server-Sent Events (same origin; no
   new port). `?once=1` returns a JSON snapshot for debugging.

Reliability/latency behavior:

- The decoder never exits on bad caption data (a dead FIFO reader would
  EPIPE `filesink` and restart the whole encode pipeline, video included) —
  malformed pairs are logged and skipped.
- **Idle erase**: after ~16s without caption data (null-pad pairs don't
  count), the display is erased — standard CEA-608 receiver behavior, and
  it clears the last caption when a station stops captioning. Tune with
  `NEXVUE_CAPTIONS_IDLE_ERASE_S` in the channel `.env` (0 disables).
- **Dead-writer guard**: `nexvue-captions.php` serves a non-empty state
  file as cleared once its mtime is older than `NEXVUE_CAPTIONS_STALE_S`
  (Apache SetEnv, default 60s) — a crashed decoder can't freeze its last
  words on every viewer's screen.
- SSE hardening: `mod_deflate` is disabled per-response (compression
  buffering would batch live events), clients get `retry: 1000` for ~1s
  reconnects, and the state poll runs at 50ms (worst-case added latency
  ~50ms; negligible next to live-captioning typing lag).
4. Player / Multiview draw a CSS overlay; **CC** persists via
   `localStorage.nexvue-captions-on`.

Not burned into pixels; no parallel video streams. Disable per channel with
`CAPTIONS_ENABLE=false` in the channel `.env`. Probe a live SDI feed (stop
the encoder on that device first):

```bash
sudo systemctl stop nexvue-encode@0
sudo -u nexvue nexvue-captions-probe.sh 0
sudo systemctl start nexvue-encode@0
```

v1 decodes **CEA-608 / CC1** (including 608 compatibility bytes inside 708
CDP). Native 708-only services/windows are out of scope until a decoder
dependency is approved.

### Latency measurement

**Preferred (glass-to-glass):** point a channel's SDI source at a burnt-in
timecode or a clock, put the WHEP player next to the source monitor,
photograph both in one frame, subtract. Repeat at 59.94p and 29.97p, and with
`ENABLE_AUDIO` on and off. Target: **~200 ms** on LAN with the tuning below;
treat >300 ms as a bug.

**Current deployment (remote datacenter):** the edge and SDI sources are in a
rack with no co-located source monitor and no observable clock feed, so the
photo method is not available. Phase 1 records an **RTT-based working
estimate** instead — observed player RTT ~80–140 ms plus the tuned pipeline
budget below puts glass-to-glass near the ~200 ms target. True glass-to-glass
measurement is deferred until on-site or bench access and does **not** block
Phase 1 closeout.

| Mode | `DEINT_FIELDS` | `ENABLE_AUDIO` | Measured (ms) | Date | Notes |
|------|----------------|----------------|---------------|------|-------|
| (remote) | — | — | ~200 est. | 2026-07 | Player RTT ~80–140 ms; glass-to-glass photo deferred |
| 59.94p | all | true | _deferred_ | | On-site/bench when available |
| 59.94p | all | false | _deferred_ | | |
| 29.97p | top | true | _deferred_ | | |
| 29.97p | top | false | _deferred_ | | |

### Latency budget & tuning

Approximate steady-state budget on LAN, 1080i59.94 source, tuned defaults:

| Stage                              | Cost      | Knob                                   |
|------------------------------------|-----------|----------------------------------------|
| SDI frame capture (interlaced)     | ~33 ms    | physics — none                         |
| DeckLink driver queue              | ~33-50 ms | `DECKLINK_BUFFER_FRAMES=3` (default; raised from 2 for SDI/driver-jitter headroom — each buffer step is ~33 ms at 29.97fps interlaced capture) |
| Deinterlace + HW encode            | ~20-25 ms | `target-usage=7`, `b-frames=0` (set)   |
| RTSP -> MediaMTX -> WHEP (LAN)     | <5 ms     | —                                      |
| Browser jitter buffer              | ~10-30 ms | `playoutDelayHint`/`jitterBufferTarget` = 0 (set in player) — 50-100 ms if unset |
| A/V sync wait (audio channels)     | 0-50 ms   | `ENABLE_AUDIO=false` removes entirely; `AUDIO_FRAME_MS=10` reduces |
| Decode + render (60 Hz display)    | ~20-30 ms | —                                      |

Practical floors: **~130-180 ms** for silent channels (prompter, multiview),
**~180-230 ms** with audio (talent return, director). The interlaced source
sets a hard floor around 120 ms; chasing below that is wasted effort.

Rules of thumb per use case:
- Prompter / multiview: `ENABLE_AUDIO=false`, `DEINT_FIELDS=top` is fine
  (29.97p adds one field-time but halves encode load).
- Director / program return: `DEINT_FIELDS=all`, audio on, `AUDIO_FRAME_MS=10`.
- `GOP_FRAMES` does NOT affect steady-state latency — only how long a new
  viewer waits for the first picture. Set 30 for snappier channel-switching
  if the slight bitrate efficiency cost is acceptable.
- On lossy external paths, the browser grows its jitter buffer regardless of
  the hint — that added delay is the network's fault, not the edge's; the
  hints set the floor, not a ceiling.

Note for Phase 2: the portal player must set the same `jitterBufferTarget`/
`playoutDelayHint` receiver hints, or external users will report 100 ms more
latency than the test player shows.

### 72-hour soak

Leave **only populated** channels running for 72h before calling Phase 1 done.
**Prefer disabling `nexvue-encode@N` on empty Quad ports rather than leaving
it running against nothing** — leave empty BNCs disabled (Services
Enable/Disable or `systemctl disable --now`). Phase 1.5 slate was rolled
back; an enabled encode on an unlocked port restart-loops briefly, then
**auto-parks** after `AUTO_PARK_UNLOCK_CYCLES` consecutive unlocked starts
(default **5** ≈ 25s with `RestartSec=5`; set `0` in `/etc/nexvue/nexvue.env`
or the channel `.env` to disable). Re-enable from Services when patched.
To park immediately without waiting for the streak:

```bash
# example: devices 4–7 unlocked / unpatched
sudo systemctl disable --now nexvue-encode@{4,5,6,7}
sudo systemctl reset-failed 'nexvue-encode@*'   # clear stale red "failed"
```

Or use the Services page Enable/Disable toggle, which does both steps per
encoder unit (see the ops-pages note under Operational notes).

On the edge box, from the repo root (or `/usr/local/bin` after `setup.sh`):

```bash
# Deploy / Temperature verify (schema + API + docroot + encode ExecStart):
sudo ./setup.sh
sudo systemctl restart nexvue-metrics
sudo nexvue-phase1-deploy-verify.sh
# then soak / storm classification:
sudo nexvue-phase1-closeout.sh --since 1h
sudo nexvue-phase1-closeout.sh --since 24h
sudo nexvue-phase1-closeout.sh              # default 72h window
sudo nexvue-encode-storm-diagnose.sh        # if Started looks high on locked channels
```

The closeout script counts `Started` **per** `nexvue-encode@N` for the
requested window **and** the last hour. A high long-window count with a quiet
last hour is a **WARN** (historical pollution from empty-port storms /
bring-up), not a FAIL. A live storm (last hour above threshold) fails so you
can run `nexvue-encode-storm-diagnose.sh`. Unlocked-but-enabled ports still
get a `disable --now` hint. After remediating a real storm, prefer
`--since 1h` until a clean 24h/72h window accumulates.

Watch for iGPU thermal throttling (`intel_gpu_top`) with all *intended*
channels hot (up to 8 on Quad 2). Confirm HI/LO both play and CC overlay stays
live on a captioned feed for the soak window.

### Phase 1 closeout checklist

Do these on the edge before calling Phase 1 done (card config + soak, not
more code). Current hardware: **DeckLink Quad 2** (already installed at
`dcwasof2nexvue01`).

| Gate | Status |
|------|--------|
| Quad 2 Input connectors + `MAX_DEVICES=8` | Done on edge (4/8 locked feeds typical; park `@4..7` when empty) |
| RTT-based latency estimate (~200 ms) | Recorded above; glass-to-glass photo deferred |
| Deploy UI + Temperature metrics | Re-run `setup.sh` + `nexvue-phase1-deploy-verify.sh` after each pull |
| 72h soak (clean Started window) | Operator — start after deploy-verify; use `--since 1h` until journal is clean |
| Captions probe + Player CC | Operator on a captioned feed |
| Phase 1.5 supervisor assumptions | Rolled back (slate/selector). Production encode is `nexvue-encode.py` (persistent publish, disposable capture) |

1. **Quad 2 connectors → half-duplex** for every intended capture BNC
   (`sudo decklink-configure --apply-inputs`, or Desktop Video Setup). Confirm with `decklink-status` (lock +
   mode per device; order is not guaranteed sequential). Set
   `MAX_DEVICES=8` in `/etc/nexvue/nexvue.env`; enable `nexvue-encode@N`
   **only** for patched inputs (empty BNCs auto-park after consecutive
   unlocks, but still waste a short restart streak — prefer leave disabled).
2. **Latency:** RTT-based estimate recorded above (~200 ms). Glass-to-glass
   photo deferred (remote rack) — not a Phase 1 blocker.
3. **72h soak** with intended (locked) `nexvue-encode@N` up; closeout script
   green (or only historical WARN on long windows); locked channels show
   Started ≤ 2 in a clean window after any storm remediation.
4. **Deploy current web UI** — `sudo ./setup.sh`, then
   `sudo systemctl restart nexvue-metrics` and
   `sudo nexvue-phase1-deploy-verify.sh`. Confirm Metrics Temperature chart
   (CPU °C + 95 °C line) in the browser.
5. **Captions**: probe at least one live feed with `nexvue-captions-probe.sh`;
   Player/Multiview **CC** toggles overlay.

### Phase 1.5 gate (assumptions confirmed from hardware)

Hardware results at the Quad 2 edge gated the supervisor before code landed
(and the implementation is now in-tree):

- DeckLink exclusive-open and empty-port restart storms proved a persistent
  RTSP session with slate is required (not a second encode process).
- Locked `@0..3` at 1080i59.94 validated the normalize-constant-caps path;
  hiccups must ride as black frames → `SIGNAL_LOSS_DEBOUNCE_S=15`.
- Status daemon already holds devices open via encoders → lock source is
  `decklinkvideosrc` `signal` + non-GAP buffer, not a second SDK probe.
- Apt GI stack approved (no pip). Caption side channel must survive
  LIVE↔SLATE (`CLEAR` FIFO) and must not fatal the encode unit.

## TLS / HTTPS (WHEP / API / status — metrics rides on Apache)

If Apache serving the player page is put behind TLS (including by IT-security
mandate), **every service the page talks to must also be TLS**, or the
browser blocks the mismatched requests. This is not optional once the page is
HTTPS — browsers refuse ALL plain-HTTP fetches from an HTTPS page (mixed
content), and separately, each `scheme://host:port` is checked independently,
so getting one port wrong throws a different, confusing error than the others.

There are effectively THREE independent TLS switches to flip — a common
mistake is enabling some and assuming the rest inherited it. They did not:

| Service | Port | Config key(s) | Symptom if forgotten |
|---|---|---|---|
| MediaMTX WHEP (viewers)   | 8889 | `webrtcEncryption`, `webrtcServerKey/Cert` | Mixed-content block (if page is HTTPS) |
| MediaMTX Control API      | 9997 | Loopback (`apiAddress: 127.0.0.1:9997`). `apiEncryption` + cert/key for same-box HTTPS clients. Browser never hits this port — `nexvue-mediamtx-api.php` proxies `/v3/paths/list`. | Player viewers/egress = `n/a` if proxy/API down |
| NexVUE status daemon      | 9998 | Loopback (`NEXVUE_STATUS_BIND=127.0.0.1`). TLS optional for direct loopback clients. | Gray dots if `nexvue-status.php` cannot reach daemon |
| NexVUE metrics dashboard  | —    | N/A — no port, no TLS needed for this piece at all. The collector has no listener; PHP reads SQLite directly and Apache (already TLS) serves the result. | N/A |
| Player input-status dots  | —    | `nexvue-status.php` on Apache (same-origin). Proxies to loopback `:9998` over HTTP or HTTPS. | Gray dots + SDI input line = `status unreachable` (not “daemon down”) |
| Player API stats          | —    | `nexvue-mediamtx-api.php` on Apache (same-origin). Proxies loopback `:9997` `/v3/paths/list`. | Viewers / egress tiles show `n/a` |

`ERR_SSL_PROTOCOL_ERROR` specifically means the browser tried a TLS
handshake against a server that's still answering plain HTTP — i.e. that
particular switch wasn't actually flipped (or the unit wasn't reloaded after
editing). A generic mixed-content *console warning* (not a network error)
means the page is HTTPS and a request is plain HTTP with no TLS attempted at
all.

**Player signal dots:** the browser fetches `nexvue-status.php` on the same
origin as the page (Apache), which talks to `nexvue-status` on loopback. That
avoids mixed content and a second per-port cert trust click for `:9998`. If
dots stay gray and **SDI input** shows `status unreachable`, check that
`nexvue-status.php` is in the docroot, PHP can reach `127.0.0.1:9998`, and
`nexvue-status` is running. A `stale` suffix means the daemon answered but
`decklink-status` is lagging — not a fetch failure.

### Steps

1. **`sudo ./setup.sh`** creates `/etc/nexvue/tls/{fullchain,privkey}.pem`
   when missing (self-signed, 825 days, SAN = hostname + primary IPs),
   sets ownership `root:ssl-cert` (`privkey` 640 / `fullchain` 644), adds
   `nexvue` + `www-data` to `ssl-cert`, points MediaMTX
   (`webrtcServer*` / `apiServer*`) and Apache (`SSLCertificate*` +
   `nexvue-ssl-certs.conf`) at those paths, enables `mod_ssl` / `default-ssl`
   when needed, **keeps Apache HTTP `:80` open redirect-only** (`a2ensite
   000-default` + `Listen 80` ensured; `nexvue_web_https_redirect_target()`
   301s every non-loopback hit to the same URL under `https://` — real
   content is never served over `:80`), enables `apache2` + `ssh`, then
   starts services.
   Existing PEM files are never overwritten — drop a real cert there before
   or after setup.
2. **Replace the self-signed pair when you have a real cert** (same paths;
   keep `root:ssl-cert` + modes above), then:
   ```bash
   sudo systemctl restart mediamtx apache2
   ```
3. **Status daemon TLS** is optional (PHP tries HTTP then HTTPS on loopback).
   To enable direct HTTPS on `:9998`, uncomment the two
   `Environment=NEXVUE_STATUS_TLS_*` lines in the live unit
   (`sudo systemctl edit --full nexvue-status`) pointing at the same
   `/etc/nexvue/tls/` files, then `daemon-reload` + restart.
4. **Self-signed click-through:** visiting `https://<ip>/` does **not** trust
   `https://<ip>:8889/` — browsers treat each port separately. Open
   `https://<edge-ip>:8889/` once and proceed past the warning, or install a
   real cert (internal CA / Let's Encrypt) before wider users.

## DeckLink Duo 2 / Quad 2 connector mapping (read this before patching)

**BNCs are bidirectional.** Full-duplex ("SDI N & N+1") parks half the
sub-devices; NexVUE needs **half-duplex** so every connector is an independent
capture path. An output-/full-duplex-misconfigured connector cannot capture —
GStreamer fails immediately with `streaming stopped, reason not-negotiated
(-4)` before a single frame, which is a distinctly different failure than "no
signal" (which still negotiates and emits black). If you see `not-negotiated`
at pipeline start, check this first, before assuming a bad cable or a card
fault.

**Preferred (headless):** `decklink-configure` (DeckLink SDK; built by
`make install` / `setup.sh`). Activates `bmdProfileTwoSubDevicesHalfDuplex`
and persists via Desktop Video preferences — no GUI required.

```bash
sudo systemctl stop 'nexvue-encode@*'
sudo decklink-configure --status          # JSON: profile, duplex, capture_ready
sudo decklink-configure --apply-inputs    # set half-duplex on all groups
sudo systemctl start nexvue-encode@0      # etc.
```

`setup.sh` runs `--apply-inputs` after building helpers, and enables
`nexvue-decklink-configure.service` so mapping is applied at boot before
encoders (`nexvue-encode@.service` Wants/After that oneshot).

**GUI fallback:** `BlackmagicDesktopVideoSetup` → each sub-device → Connector
Mapping → single connector / half-duplex (not "SDI N & N+1").

**The physical-connector-to-`device-number` order is not guaranteed
sequential.** `BlackmagicFirmwareUpdater status` may show device paths like
`io0, io2, io1, io3` — confirm which logical index (0-3) corresponds to which
physical BNC empirically (patch a source, run `decklink-status`, see which
index locks) rather than assuming connector 1 = device 0.

**Diagnosing "which input has signal" reliably** (used throughout this
project's own bring-up):
```bash
# stop any encoders first so status can actively probe idle inputs
sudo systemctl stop nexvue-encode@0 nexvue-encode@1   # etc.
/usr/local/bin/decklink-status | jq '.devices[] | {index, input_locked, input_mode}'
```
The `input_locked: true` entry with a real `input_mode` (e.g. `1080i59.94`)
is your live input's real `device-number`. `decklink-status` actively enables
each idle input to detect signal (see its file header for why — the DeckLink
Status API does not report lock on an idle, unenabled input by default).

## Usage Metrics Dashboard

**This is usage/analytics history, not health/uptime monitoring** — it
answers "how much bandwidth did channel 2 use in the last hour," "which IP
was watching what, when," and "was this input locked all night," not "is
the service up right now." Health/alerting is a Phase 4 portal concern
(outbound edge heartbeats — the DMZ edge cannot pull from TRUSTED
monitoring). This dashboard is separate and needs no portal dependency.

### Architecture: collector writes, PHP reads, nothing new to open

Two independent pieces, split deliberately so the read side needs **no
firewall rule, no reverse proxy, no WebSocket, no new port of any kind**:

```
nexvue-metrics-server.py  --writes-->  SQLite  <--reads--  nexvue-metrics.php
   (background collector,                              (runs inside Apache,
    no network listener,                                 already-open 443,
    polls MediaMTX + status)                             already trusted)
```

- **`nexvue-metrics-server.py`** polls the MediaMTX API and the
  `nexvue-status` daemon every 15s (configurable) and writes time-series
  samples into SQLite (stdlib `sqlite3` — no pip). It does not listen on any
  port, does not serve HTTP, does not need a firewall rule of any kind —
  there's genuinely nothing on this side for a security review to look at.
- **`nexvue-metrics.php`** opens that same SQLite file **read-only**
  (`SQLITE3_OPEN_READONLY` — verified: even a deliberate write attempt is
  rejected at the SQLite engine level, not just by convention) and serves
  JSON. Apache runs it like any other PHP script on the site you already
  have open — no `mod_proxy`, no new listener, nothing to add to a firewall
  rule anywhere.
- **`metrics.html`** (top nav → Metrics) fetches from `nexvue-metrics.php`
  sitting next to it. Plain `fetch()`, never a WebSocket. Paths are relative
  to wherever the files load from.

### Install

Already covered by `sudo ./setup.sh` (collector unit + `php-sqlite3` + copy
into `/var/www/html` when that directory exists). Manually:

**1. Collector** (background poller, no networking):
```bash
sudo install -m 755 nexvue-metrics-server.py /usr/local/bin/
sudo install -m 644 nexvue-metrics.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nexvue-metrics
```

**2. PHP + dashboard** (prefer `sudo ./setup.sh`, which installs Apache +
mod_php and copies the UI). Manual:
```bash
cd web-node
sudo cp nexvue-metrics.php metrics.html index.html multiview.html /var/www/html/
sudo systemctl restart apache2
```

**3. Permissions — the one real setup step.** The collector runs as the
`nexvue` user; PHP runs as Apache's user (commonly `www-data`). PHP needs to
*read* the SQLite file the collector writes. Rather than fuss with group
membership, the collector unit sets `StateDirectoryMode=0755` and the
script itself `chmod`s the database (and its WAL-mode sidecar files) to
`0644` after every write cycle — so any user on the box can read it, which
is fine since this data (bandwidth, viewer IPs/channels) is explicitly meant
to be served publicly via Apache anyway. Nothing to configure here as long
as both pieces are running with their shipped units/script — just know
*why* it works if you're auditing permissions later.

Open `https://<edge-ip>/metrics` (top nav → Metrics).

### Views (`nexvue-metrics.php?view=...&range=...` or `&from=&to=`)

| `view` | Returns |
|---|---|
| `totals` | System-wide time series: bandwidth, viewer count, active-stream count — one row per poll cycle. Powers the three top-line charts. |
| `channels` | **Per-channel breakdown**, aggregated over the range: avg/peak bandwidth, avg/peak viewers, % of the window the channel was `ready`. "How much bandwidth did ch0 use in the last hour" as one row. |
| `viewers` | Per-viewer session drill-down: IP, channel, user (MediaMTX JWT `sub` when present), first/last seen, duration, bytes served, live/ended. Add `&channel=chN` for an exact channel. Optional column filters (`filter_status`, `filter_ip`, `filter_channel`, `filter_duration`, `filter_data`, `filter_client`) — see below. Response includes `session_total` / `session_count` / `filters`. |
| `inputs` | Per-DeckLink-input lock/format history as a time series. Powers the input-lock chart. |
| `weekday_hours` | Mon–Sun × hour-of-day analytical heatmap. Each cell is the equal-weight average of observed calendar dates in the range for that weekday/hour (missing telemetry excluded; `date_count` is the denominator, `sample_count` is diagnostic). Also returns peaks. Timezone: `America/New_York` by default (`NEXVUE_METRICS_TZ` override). |
| `host` | Host CPU %, memory used/total, load1, CPU package °C / iGPU °C (when sysfs exposes them), and (when available) iGPU Video/Render/VideoEnhance busy % + GPU freq — capacity correlation on the Metrics page, **not** uptime/alerting. Engine % requires `intel-gpu-tools` / `intel_gpu_top` + CAP_PERFMON (see setup). The dashboard charts CPU, memory, iGPU Video engine, and a **Temperature** panel (CPU/GPU °C plus a dashed 95 °C sustained-operation limit line — `TEMP_LIMIT_CPU_C` / `TEMP_LIMIT_GPU_C` in `metrics.html`). Render % is still collected/served but not charted. |

`range` accepts `15m`, `1h`, `6h`, `24h`, `7d`, `30d` — matching the
dashboard's preset buttons. For a specific day or window, pass Unix epoch
seconds as `from` and `to` instead (both required; max span 30 days, matching
retention). The dashboard exposes datetime-local From/To + Apply/Clear.

Example: `nexvue-metrics.php?view=channels&range=24h` — bandwidth/viewer
breakdown per channel over the last day, system-wide (omit `channel=`) or
`nexvue-metrics.php?view=viewers&range=15m&channel=ch0` for who's watching
channel 0 right now. Custom:
`nexvue-metrics.php?view=totals&from=1710000000&to=1710086400`.

After upgrading the collector, restart `nexvue-metrics` so it creates the
`host_samples` table / new columns (`sudo systemctl restart nexvue-metrics`).

**iGPU (Quick Sync) charts.** The collector keeps ONE persistent
`intel_gpu_top -J -s 1000` child running and continuously reads its JSON
stream on a background thread, storing the newest sample each poll. It is
deliberately NOT a run-and-kill one-shot: `intel_gpu_top` block-buffers
stdout when writing to a pipe, so a short-lived run is routinely killed
before the first buffer flush — empty stdout and empty iGPU charts on real
hardware, even though the same command shows live numbers interactively.
The child is restarted automatically (30s backoff) if it exits, with its
stderr tail logged for diagnosis. Video engine % is the primary encode-load
signal; Render/3D and VideoEnhance are also stored. If the tool is missing,
lacks PMU permission, or the kernel uses `xe` without a working
`intel_gpu_top`, those series stay empty (CPU/memory still collect).
`setup.sh` installs `intel-gpu-tools`, `setcap`s `intel_gpu_top` when
possible, and the unit grants
`AmbientCapabilities=CAP_PERFMON CAP_SYS_ADMIN`.

**Temperature chart.** Each poll also samples package/GPU temperatures from
sysfs (no extra package): CPU from `coretemp` hwmon (`temp1_input`), with
fallback to `thermal_zone*/type == x86_pkg_temp`; GPU from `i915` or `xe`
hwmon when present. Values land in `host_samples.cpu_temp_c` /
`gpu_temp_c` and the Metrics **Temperature** panel. If the iGPU driver
exposes no temperature node (common on some iGPUs), the GPU series stays
empty — never invent a value. After upgrading, restart the collector so
the new columns migrate:
`sudo systemctl restart nexvue-metrics`.

### Configuration

**Collector** (systemd `Environment=` lines on `nexvue-metrics.service`, all optional):

| Variable | Default | Purpose |
|---|---|---|
| `NEXVUE_MEDIAMTX_API_URL` | `https://127.0.0.1:9997` | Where to poll for bandwidth/viewers/streams/sessions |
| `NEXVUE_STATUS_URL` | *(unset)* → `http://` then `https://` on `:9998` | Where to poll for input lock/format. When unset, tries plain HTTP then HTTPS (status TLS is optional). Set explicitly to pin one scheme. |
| `NEXVUE_METRICS_POLL_INTERVAL_S` | `15` | Seconds between polls |
| `NEXVUE_METRICS_RETENTION_DAYS` | `30` | Samples/sessions older than this are pruned hourly |
| `NEXVUE_METRICS_DB` | `/var/lib/nexvue/metrics.db` | SQLite file path (auto-created via `StateDirectory=`) |
| `NEXVUE_INTEL_GPU_TOP` | `intel_gpu_top` | Binary path for iGPU sampling |
| `NEXVUE_INTEL_GPU_TOP_PERIOD_MS` | `1000` | `-s` sample period (ms) for the persistent `intel_gpu_top -J` stream (replaces the old `NEXVUE_INTEL_GPU_TOP_TIMEOUT_S` one-shot timeout) |

If MediaMTX is still plain HTTP (TLS not yet configured — see the TLS
section above), set `NEXVUE_MEDIAMTX_API_URL` to `http://127.0.0.1:9997`.
Status needs no override when TLS is off: the collector already tries HTTP
first. To force HTTPS-only (or a non-loopback URL), set `NEXVUE_STATUS_URL`.

**Live-host note:** if an older collector build is still defaulting to
`https://127.0.0.1:9998` against a plain-HTTP status daemon, you will see
`SSL: WRONG_VERSION_NUMBER` in the journal every poll. Until the updated
collector is deployed, pin HTTP with a drop-in:

```bash
sudo systemctl edit nexvue-metrics
# [Service]
# Environment=NEXVUE_STATUS_URL=http://127.0.0.1:9998
sudo systemctl restart nexvue-metrics
```

**PHP** (set via Apache vhost `SetEnv`, or edit the default in the script):

| Variable | Default | Purpose |
|---|---|---|
| `NEXVUE_METRICS_DB` | `/var/lib/nexvue/metrics.db` | Must match the collector's DB path |
| `NEXVUE_METRICS_TZ` | `America/New_York` | Timezone for heatmap bucketing and (via API) dashboard clock labels / custom From–To. Override only if this edge should report in another zone. |

### Viewer drill-down: how it works

`/v3/paths/list` only gives a reader *count* per path — no IP, no per-session
detail. The actual client address, connection start time, and byte counts
live on a separate MediaMTX endpoint, `/v3/webrtcsessions/list`, which the
collector also polls each cycle. Each WebRTC session there is tagged
`state: "read"` (a viewer) or `state: "publish"` (one of *our own* encoders
publishing into MediaMTX) — only `"read"` sessions are stored, so an
encoder's own connection never shows up in the viewer table (verified: a
mock session with `state: "publish"` is confirmed excluded from the stored
rows).

Sessions are stored as one row per `session_id`, upserted each poll:
`first_seen` is set once and never overwritten; `last_seen`, bytes served,
and `user` (once Phase 2 auth issues one) advance every cycle the session is
still active. That gives a clean per-viewer lifecycle record without one row
per poll cycle per viewer bloating the table. A session reads as "live" if
seen within the PHP script's active-session window (45s — about 3 poll
cycles); otherwise "ended."

**Column filters (Metrics UI + API).** The viewer table has a filter row under
Status, IP address, Channel, Duration, Data served, and Client. Filters are
applied server-side in `nexvue-metrics.php` (so a 30-day window is trimmed
before JSON leaves Apache). Syntax:

| Form | Example | Notes |
|---|---|---|
| Plain text | `Chrome`, `203.0.113`, `live` | Case-insensitive substring |
| Regex | `/^ch\d+$/i`, `/firefox/i` | PCRE `/pattern/flags`; invalid → HTTP 400 |
| Duration compare | `>=10m`, `<2h`, `=90s` | Against raw seconds (`s`/`m`/`h`) |
| Data compare | `>500MB`, `<=1.5GB` | Against raw bytes (SI: B/KB/MB/GB) |

Duration/Data also accept text/regex against the same display strings the
table shows (`10m`, `1.5h`, `50.0 MB`). Exact `channel=chN` still works and
ANDs with `filter_channel`. Max expression length 128. The UI debounces
input, shows `showing N of M`, and keeps filters across the 30s refresh.

The table paginates client-side below the rows: page size 50 (default) /
100 / 250 / 500 / all, with Prev/Next and a `start–end of total` readout.
Sorting stays global (whole filtered set, not per page); changing filters,
the channel selector, or the page size returns to page 1. Pagination is
display-only — the API still returns the full filtered window.

Examples:

```
nexvue-metrics.php?view=viewers&range=24h&filter_status=live&filter_duration=%3E%3D10m
nexvue-metrics.php?view=viewers&range=7d&filter_ip=203.0.113&filter_data=%3E500MB
nexvue-metrics.php?view=viewers&range=1h&channel=ch0&filter_client=/Chrome/i
```

**Kick (live sessions only).** The Metrics viewer table has a Kick button on
live rows. It POSTs `kick_viewer` (optional `reason`) to `nexvue-ops.php`,
which looks up the session on MediaMTX, calls
`POST /v3/webrtcsessions/kick/{session_id}` on loopback, and records the
session in a short-lived kick registry (temp JSON, ~10 min TTL). Metrics PHP
stays read-only. Player / Multiview capture the WHEP `ID` response
header (MediaMTX API session UUID — not the `Location` WHEP secret) and call
`kick_check` before self-healing reconnect — kicked viewers see a disconnect
message and stop auto-retry. Matching is by MediaMTX WebRTC session UUID only
(safe when many viewers share a NAT IP or the same channel). Manual rejoin
(pick a channel again) still works unless their session/share is revoked or
expired; JWT auth is the lasting gate.

### Notes

- The collector calls MediaMTX/status over loopback with certificate
  verification disabled — safe specifically because that traffic never
  leaves 127.0.0.1; see the comment on `_unverified_ssl_context()` in the
  script if extending this pattern elsewhere.
- `nexvue-metrics.php` rejects the `channel` query parameter outright
  (alphanumeric-only) rather than escaping it — there's no legitimate reason
  for a channel name to contain anything else, and this keeps the SQL
  trivially safe from injection without relying on remembering to escape
  correctly everywhere.
- 30 days of 15-second samples across a handful of channels is a few tens of
  thousands of rows — trivial for SQLite; no performance tuning needed at
  this scale.
- `remote_addr` includes the port (e.g. `203.0.113.7:54321`); the dashboard
  table strips it for readability, but it's stored as-is in `viewer_sessions`
  if you need it for something else (e.g. correlating with firewall logs).

## Operational notes

- **Channel env files tolerate inline `#` comments and whitespace.** The
  encoder unit sources `/etc/nexvue/channels/<N>.env` through a shell
  (`nexvue-encode@.service`'s `ExecStart`), not systemd's native
  `EnvironmentFile=` parser — the latter does NOT strip inline comments, so a
  line like `MAX_DEVICES=4   # note` would otherwise pass the comment through
  as part of the value and break arithmetic checks. `nexvue-encode.py`
  also strips inline comments when reading env, so this is safe even if
  the encoder is invoked outside the unit.
- **Values with spaces must be shell-quoted** for the same reason (the file is
  sourced by bash): an unquoted `CHANNEL_ALIAS=TVU 35` runs `35` as a command
  and silently truncates the alias to `TVU` (journal tell:
  `<N>.env: line NN: 35: command not found`). Write
  `CHANNEL_ALIAS="TVU 35"`. The Settings page writer
  (`nexvue-ops-env-update.py`) quotes such values automatically and reads
  quoted values back correctly.
- **`index.html` / `multiview.html` auto-discover the edge host** from
  `location.hostname` — load them via Apache at any address. WHEP always uses
  `https://<host>:8889` (MediaMTX `webrtcEncryption: yes`); it does **not**
  inherit a leftover `http:` page scheme (that POSTs plaintext at TLS `:8889`
  and the browser shows `ERR_CONNECTION_RESET` / a fake CORS error). The
  front door 301s non-loopback HTTP to HTTPS — Apache keeps `:80` open
  specifically for that redirect, but real UI content is HTTPS-only; still
  load `https://<edge>/player` and click-through
  `https://<edge>:8889/` once if the cert is self-signed (trust on `:443`
  does not extend to other ports). Top nav brand **NexVUE** (click for
  page-URL QR) /
  Player / Multiview / Metrics / Services / Settings. Player and Multiview
  session metrics sit in a collapsed bottom drawer (`Session metrics`);
  Multiview focuses the audio-active pane. Hover a tile ~2s for
  an explainer. **VU meters** (right edge) follow channel `AUDIO_LAYOUT`
  (stereo / 5.1 / stereo+SAP / 5.1+SAP). Toolbar: **Main**/**SAP**,
  **St** (5.1→stereo fold) / **5.1** (discrete surround to the PC), plus
  engineering solo — all **this browser only** (`nexvue-vu.js` localStorage).
  Transport is discrete Opus only (no Dolby).
- **Channel aliases:** optional `CHANNEL_ALIAS=` in each channel `.env` (see
  `channels-example.env`). Player and Multiview show the alias when set;
  WHEP still uses `CHANNEL_PATH` (`ch0`, …). Edit aliases on the Settings page.
  Encode **always** opens DeckLink 8ch and publishes **8ch positioned Opus**
  (default `AUDIO_BITRATE_BPS=384000`) to HI and LO (one Opus encode, tee'd).
  Per-branch mono `channel-mask` through deinterleave/interleave is required:
  decklinkaudiosrc emits `channel-mask=0`, and unpositioned multichannel
  makes opusenc emit mapping family 255 — which has **no RTP payloader**.
  Positioned input encodes family 1 / MULTIOPUS. `AUDIO_LAYOUT`
  (`stereo`|`51`|`stereo_sap`|`51_sap`) is a **player role preset only**
  (Main/SAP/5.1 UI). `AUDIO_EMBEDS` (Settings checkboxes, e.g. `1,2,7,8`)
  chooses which embeds the browser VU offers — metadata only; encode still
  publishes all eight. No 16ch path. Player / Multiview munge the WHEP SDP
  offer to add `multiopus` (`nexvue-vu.js`, same as MediaMTX `reader.js`) —
  Chrome does not advertise it in `createOffer()`, and without that munge
  MediaMTX returns WHEP **400**. Use Chrome/Edge for multichannel Opus.
  (An experimental Safari stereo companion path in 1.13.0 was rolled back in
  1.13.1 — it broke playback broadly.)
- **Ops pages (Services / Settings)** call `nexvue-ops.php`, which uses
  allowlisted sudo wrappers under `/usr/local/bin/nexvue-ops-*` (sudoers drop-in
  `/etc/sudoers.d/nexvue-ops`). Channel saves write env files only; restart is an
  explicit confirm. **Restart all encoders** (`restart_encoders`) restarts every
  systemd-enabled `nexvue-encode@N` (parked/disabled slots stay parked) from
  Settings or Services. Logo actions (`logo_get` / `logo_put` / `logo_delete`) write
  `/var/lib/nexvue/branding/{logo.bin,logo.json}` as www-data (no sudo);
  `nexvue-logo.php` streams the image for the nav. Phase 1 LAN-trust — do not
  DMZ-expose without auth.
  The Services page also shows each unit's systemd enable state
  (`nexvue-ops-status.sh` prints `<is-active> <is-enabled>`) and offers two
  toggles for **encoder units only** (`nexvue-encode@0-7`, via
  `nexvue-ops-enable.sh`): Enable/Disable (`set_enabled`, boot config +
  immediate `--now` effect — parking an unpatched Quad port from the UI
  instead of SSH) and Start/Stop (`set_running`, runtime only — boot config
  untouched). Empty unlocked slots also **auto-park** via
  `nexvue-encode-auto-park.sh` after `AUTO_PARK_UNLOCK_CYCLES` consecutive
  unlocked starts (default 5; Settings → Advanced, or `/etc/nexvue/nexvue.env`).
  Core units (mediamtx, nexvue-status, nexvue-metrics) can be
  restarted but never disabled or stopped from the page. Disable and Stop
  both run `reset-failed` after acting, so a previously restart-looping
  encoder stops showing a stale red `failed` after being parked. Any
  encoder that is disabled and not running — including one still carrying a
  stale `failed` from before it was parked (e.g. disabled over SSH without
  `reset-failed`) — renders as neutral "disabled", never failure-red, on
  both Services and Settings; `failed` stays red only while the unit is
  enabled, where it is a live fault. Support zip download uses the same
  sudoers allowlist (`nexvue-ops-support-bundle.sh`); URL userinfo and
  password/passphrase/token values are redacted before zip.
- **Multiviewer defaults to LO** with a global HI/LO toggle (quad = up to four
  simultaneous WHEP sessions). Only one pane is unmuted at a time — click a
  pane to select audio. Switching Dual↔Quad tears down hidden panes so unused
  sessions do not linger. Multiview share links (`?t=`) are limited to four
  channels and auto-tune panes on open; Fullscreen is near-frameless (chrome
  + pane bars hidden).
- **Mirror/flip/rotate persist through fullscreen.** Applied as an inline
  `transform` on the video (not a CSS class), and the dedicated "⛶
  Fullscreen" button fullscreens the wrapper `<div>`, not the `<video>`
  element itself — fullscreening the video directly (e.g. via its native
  player-bar control) lets the browser override the transform. Use the ⛶
  button, not the native control, if mirror/flip/rotate need to survive
  fullscreen. Player **⟲ 90° / 90° ⟳** rotate the view in 90° steps (CW from
  upright) for portrait sources or a tilted monitor
  (`localStorage.nexvue-video-rotate`). 90°/270° swap the video layout box
  so the frame still fits. Multiview has no rotate controls.
- **Closed captions are a side channel**, not MediaMTX tracks. Encode writes
  `/run/nexvue/captions/<path>.json`; Apache serves SSE via
  `nexvue-captions.php`. HI/LO reconnect keeps the same channel subscription.
  The encode capture pipeline keeps `output-cc` / `ccextractor` on DeckLink.
  Capture rebuilds close the FIFO (decoder exits on EOF); encode respawns
  the decoder on the next open. Idle-erase still clears stale overlay text
  when VANC stops.
- **Input status & reference:** the `nexvue-status` daemon (port 9998) polls the
  DeckLink Status API via the `decklink-status` helper and serves JSON
  (per-input signal lock + detected format, genlock reference lock + mode).
  The test player fetches it through same-origin `nexvue-status.php` (Apache
  → loopback), showing green/red dots per channel plus SDI input and Reference
  tiles. Build the helper first: download the Blackmagic DeckLink SDK, then
  `make DECKLINK_SDK=/path/to/sdk && sudo make install`, and
  `systemctl enable --now nexvue-status`. Status queries coexist safely with an
  active capture.
- **LO renditions (adaptive bandwidth):** default `LO_ENABLE=true` /
  `LO_PRESET=360p` for new channel envs and Factory defaults (override per
  channel). Publishes `<path>lo` alongside HI — one live source, two QSV
  encodes via tee (`LO_TARGET_USAGE=7`, deeper LO queue, `qos=false` on LO
  videorate/scale). Watch iGPU load when many LO tees run. Settings only
  offers curated `LO_FPS` / `LO_TARGET_USAGE` / `LO_QUEUE_BUFFERS` values;
  ops also map legacy aliases (`60`/`30`/`15`, `59.94`/`29.97`) to GStreamer
  fractions — bare integers used to become `framerate=(int)N` and break the
  LO pipeline. Viewers on bad links switch to LO in the player. Tune
  `LO_BITRATE_KBPS` / `LO_PRESET` / `LO_TARGET_USAGE` / `LO_QUEUE_BUFFERS`
  in Settings if LO still looks choppy — under multi-channel load keep usage
  at 7 or use a lower preset.
- **SRT inputs:** deferred with the Phase 1.5 rollback — production encode
  is DeckLink-only (`nexvue-encode.py`). `INPUT_TYPE=srt` remains in Settings
  for a future redesign; do not enable it on live units today.
- **Self-healing model:** `nexvue-encode.py` runs two GStreamer pipelines in
  one process. **Publish** (appsrc → HI/LO encode → `rtspclientsink`) stays
  up and keeps pushing last-frame / black + silence so MediaMTX / WHEP
  sessions survive SDI flaps. **Capture** (DeckLink → normalize → appsink)
  is torn down and reopened on `not-negotiated`, exclusive-open races, or
  EOS — never via `input-selector` / slate (that stormed in Phase 1.5).
  Capture appsinks pull preroll (`new-preroll` + `async=false`) so a live
  DeckLink pipeline is not stuck PAUSED. The open gate fails only on ERROR
  or no video frame after `DECKLINK_OPEN_GATE_S` (10s) — PAUSED-with-frames
  is a successful open. Retry / auto-park probe runs only after capture
  reaches NULL (off-thread) so a still-held card is not reported busy;
  shutdown waits out an in-flight async NULL. RTSP sink death rebuilds
  publish only. Once a channel
  has been live, unlocks retry in-process and do not increment auto-park.
  Empty ports that never lock still cycle (`RestartSec=5`) for up to
  `AUTO_PARK_UNLOCK_CYCLES` (default 5), then `nexvue-encode-auto-park.sh`
  disables that `encode@N` (exit 75 + `ExecStopPost`). `WATCHDOG_MS`
  defaults to **0** (off) so a 3s SDI hitch is hold/black, not a unit bounce.
  `SIGNAL_LOSS_HOLD_S` (default 15) holds the last frame, then black.
- **Signal-present / encoder-alive alarming** belongs in Phase 4 portal ops
  via **outbound** edge heartbeats (DMZ cannot reach TRUSTED monitors).
  Status daemon JSON + MediaMTX `/v3/paths/list` remain the on-box data
  sources the heartbeat will summarize.
- **Format changes:** normalized away — output caps are constant per channel.
- **No Docker, no Node** — two binaries, two scripts, systemd.

## Phase 1.5 supervisor — rolled back (superseded by split-pipeline encode)

**Status (2026-08-14):** production ExecStart is `nexvue-encode.sh` →
`nexvue-encode.py`. The Phase 1.5 `input-selector` / NO SIGNAL slate path
was pulled after LIVE↔SLATE flaps and DeckLink `not-negotiated` /
`error (-5)` storms. `nexvue-supervisor.py` remains in the tree unused.
The 2.4 encoder keeps the 1.5 *goal* (persistent RTSP, disposable DeckLink)
without selector pad surgery: two pipelines, appsrc/appsink, last-frame /
black hold.

Historical notes below describe the rolled-back design.

Decisions taken (the three "open decisions" below were resolved this way):

1. **Switch mechanism:** a persistent `input-selector` (video) + `input-selector`
   (audio) pair, each with a permanent "slate" sink pad and a **dynamically
   added/removed** "DeckLink" sink pad. Both branches feed size-matched
   progressive NV12 into the selector; **framerate is locked after**
   `input-selector` (`identity single-segment=true ! videorate ! … caps`)
   so LIVE↔SLATE never renegotiates the encoder or drops the RTSP/WHEP
   session, and pad switches do not hand `vah264enc` a new segment
   timeline (that was posting basesrc `error (-5)` / `not-negotiated`).
   Selectors use `sync-streams=false` (with sync on, the element
   paces like a sink and the LO tee branch starved to ~1–2 fps). While
   LIVE, slate `videotestsrc`/`textoverlay`/`audiotestsrc` are **PAUSED**
   (deferred ~2s after the pad switch) so they do not keep rendering 1080p
   behind the inactive pad. The DeckLink capture branch itself is torn
   down and rebuilt (not just re-selected) only on a hard GStreamer
   ERROR/EOS — with exponential reopen backoff until LIVE is stable —
   normal signal loss/acquire never touches the pipeline graph, only
   `active-pad` (plus slate pause/resume).
2. **Lock signal source:** `decklinkvideosrc`'s read-only `signal` GObject
   property (via `notify::signal`), corroborated by a buffer pad probe that
   requires at least one real (non-`GAP`) buffer before promoting to LIVE —
   a parameter lock alone is not proof frames are flowing. The status
   daemon's existing "fast status-flag fallback for inputs held by a
   running encoder" already coexists with the supervisor holding the
   device open; no changes needed there.
3. **Apt GI stack:** `python3-gi gir1.2-glib-2.0 gir1.2-gstreamer-1.0
   gir1.2-gst-plugins-base-1.0` (added to `setup.sh` step 1). Still zero
   pip, per project policy — these are apt-only GObject Introspection
   bindings.

Testable without hardware or GI installed: `load_config()` (env validation)
and `StateMachine` (LIVE/SLATE/RECOVERING, injectable clock) are pure
Python behind a try/import GI guard — `test/test_nexvue_supervisor.py`
covers both; `test/test_nexvue_captions.py` covers the new captions
control-FIFO CLEAR command and `Cea608Cc1.reset()`.

**Hiccup tolerance:** `SIGNAL_LOSS_DEBOUNCE_S` defaults to **15 seconds**.
Brief unlocks stay on the DeckLink pad (black frames, same as Phase 1) and
do not flash the NO SIGNAL slate. Tune per channel in Settings if needed.
`WATCHDOG_MS` defaults to **0** (off); a short Gst watchdog would tear down
the DeckLink bin before debounce could ride out a hiccup. Caption
`filesink`/decoder EPIPE is logged and non-fatal so the side channel cannot
systemd-restart the encode unit.

### Phase 1.5 hardware acceptance (Quad 2)

Run on the edge after `sudo ./setup.sh` (GI + `input-selector` /
`videotestsrc` / `valve` present). Prefer parking empty ports; enabling all
eight is optional capacity soak.

1. **Boot empty ports** — with a channel unlocked, supervisor stays up and
   publishes NO SIGNAL slate (WHEP plays slate; unit does not restart-loop).
2. **Insert / remove cable** — LIVE within ~1s of lock+frames; after
   `SIGNAL_LOSS_DEBOUNCE_S` (15s) of unlock, picture returns to slate without
   dropping the RTSP/WHEP session.
3. **Format change** — switch SDI mode on a live feed; normalized HI caps
   hold; viewers do not renegotiate.
4. **Signal flap** — brief unlocks under 15s stay on DeckLink (black frames),
   no slate flash.
5. **HI / LO + audio** — both renditions play; audio continuous across
   LIVE↔SLATE (silent slate when `ENABLE_AUDIO=true`).
6. **Captions** — on a 608 feed, CC clears on SLATE and resumes on LIVE;
   overlay does not stick stale lines.
7. **WHEP continuity** — leave a Player/Multiview session connected through
   steps 2–6; session UUID stays up (picture changes, connection does not).
8. **Soak** — intended channels (or all eight) for 72h; then
   `sudo nexvue-phase1-closeout.sh` (compare `--since 1h` if the 72h journal
   is polluted by earlier bring-up).

<details>
<summary>Original specification (historical reference)</summary>


**Goal:** eliminate the no-signal-at-boot restart loop. Today
`nexvue-encode@N` fails (or spins) when DeckLink has no lock at start.
Phase 1.5 replaces the bare `gst-launch` ExecStart with a **Python
supervisor** that keeps a persistent RTSP publish into MediaMTX and switches
the *input* between DeckLink and a generated **NO SIGNAL** slate without
dropping the RTSP/WHEP session (viewers stay up; picture changes).

**Non-goals:** ABR/SFU, portal auth, DMZ bind changes, native 708 decode,
burning captions into video.

### Architecture

```
nexvue-encode@.service
        |
        v
nexvue-supervisor.py  (per channel; reads /etc/nexvue/channels/N.env)
        |
        +-- gst pipeline (appsrc/input-selector OR rebuild-safe branch)
        |      DeckLink video/audio  <-->  slate videotestsrc + audiotestsrc
        |      deinterlace/normalize -> vah264enc (+ LO tee) -> RTSP
        |      output-cc -> ccextractor -> FIFO -> nexvue-captions-decode.py
        v
MediaMTX (unchanged H.264+Opus paths)
```

One supervisor process per channel (same systemd template model). MediaMTX
and the DeckLink card remain the shared components. Stdlib Python only
(no pip); GStreamer via `gi` / PyGObject from apt (`python3-gi`,
`gir1.2-gstreamer-1.0`) — approve apt deps in `setup.sh` before coding.

### State machine

| State | Input | RTSP | Captions JSON |
|-------|-------|------|---------------|
| `LIVE` | DeckLink | publishing | extract CC1 as today |
| `SLATE` | generated slate | publishing (same path/caps) | clear cue (empty text) |
| `RECOVERING` | probing DeckLink | keep last picture or slate | unchanged until decision |

Transitions:

1. **Boot with lock** → `LIVE`.
2. **Boot without lock** → `SLATE` immediately (no restart loop).
3. **`LIVE` → loss of lock** (debounce **T_loss**, default 2s) → `SLATE`;
   write cleared caption state once.
4. **`SLATE` → lock acquired** (debounce **T_acquire**, default 1s) →
   `LIVE`; resume CC extract (idle-erase / stale PHP still apply).
5. **Pipeline error** → log, attempt in-process recovery; if unrecoverable,
   exit non-zero so systemd `Restart=` still heals the process (last resort).

Output caps stay **normalized** (constant raster/rate/bitrate per channel)
so DeckLink ↔ slate never renegotiates encoder or WHEP.

### Slate

- Video: black (or dark gray) 1920×1080 progressive at the channel's output
  rate; centered burn-in text `NO SIGNAL` (+ optional `CHANNEL_ALIAS` /
  `CHANNEL_PATH`).
- Audio: silence (or low-level tone only if needed for A/V sync testing —
  default silence when `ENABLE_AUDIO=true`).
- Same HI encode path; LO tee unchanged when `LO_ENABLE=true`.

### Captions contract

- While `LIVE`: keep `output-cc` → FIFO → `nexvue-captions-decode.py`
  (unbuffered `filesink` remains mandatory).
- On enter `SLATE`: stop feeding pairs **or** keep reader alive and emit a
  single clear (`text=""`, `clear=true`) so overlays blank; never leave a
  dead FIFO reader (EPIPE kills encode).
- Decoder crash-proofing and idle erase remain as today.
- Probe tooling (`nexvue-captions-probe.sh`) stays DeckLink-oriented for
  bring-up; supervisor does not replace it.

### Systemd / packaging

- `nexvue-encode@.service` ExecStart → `/usr/local/bin/nexvue-supervisor.py`
  (or thin `nexvue-encode.sh` wrapper that exec's the supervisor).
- Env file sourcing unchanged (`EnvironmentFile=-` / bash source pattern as
  today — keep quoting rules for `CHANNEL_ALIAS`).
- `setup.sh` installs supervisor + apt GI packages; units reloaded.
- Ops UI restart still restarts `nexvue-encode@N`.

### Tests (required with implementation)

- Unit: state machine debounce (loss/acquire), slate-enter clears captions,
  sanitize/env load.
- Integration-ish (no card): mock lock signals → LIVE/SLATE transitions;
  ensure clear JSON written.
- No live DeckLink required in CI; hardware soak remains the real proof.

### Open decisions (owner before code)

1. **Switch mechanism:** GStreamer `input-selector` in one long-lived
   pipeline vs tear-down/rebuild of the capture branch only (RTSP sink held).
   Prefer selector if caps stay identical; rebuild if DeckLink open/close is
   cleaner on this SDK.
2. **Lock signal source:** poll `decklink-status` / Status API vs pad probes /
   element messages from `decklinkvideosrc`. Prefer Status API for boot
   (matches player dots); confirm coexistence when supervisor holds the
   device open.
3. **Apt GI stack:** confirm `python3-gi` + GStreamer typelibs on the
   Arrow Lake image before writing code.

~~Do not implement until the three decisions above are confirmed.~~ Resolved
and implemented — see the decisions list above the collapsed spec.

</details>

## Phase roadmap (agreed architecture)

| Phase | Scope |
|---|---|
| 1 (this) | Single edge, LAN WHEP, no auth. Prove stability + latency. |
| 1.5 | **Rolled back** (slate/selector). Split-pipeline encode (`nexvue-encode.py`) is the production healer. See "Phase 1.5 supervisor" below. |
| 2 | **Edge local auth landed** (bcrypt users + roles, share links, MediaMTX JWT/JWKS, sync-shaped export/import). Central PHP portal (catalog + fleet sync client) still future; Entra OIDC remains Phase 3. |
| 3 | DMZ exposure: TLS on 443, `webrtcAdditionalHosts` = public FQDN, single UDP 8189 rule + ICE-TCP fallback; MediaMTX API + status daemon already loopback-bound (`nexvue-mediamtx-api.php` / `nexvue-status.php`). Remaining: Entra ID OIDC at portal, CORS validation portal-origin -> edge. |
| 4 | **First slice landed.** Cloud portal (`web-portal/`, `sudo ./setup.sh --portal` on a separate box) — multi-tenant catalog + identity front door. Portal becomes the viewer-JWT issuer for an adopted station via a merged JWKS on the edge (`nexvue-jwks.php`), so MediaMTX config never changes and a portal outage never breaks local login/share links/publish. Enrollment + heartbeat are edge-initiated outbound only. See CLAUDE.md's Phase 4 entry for the full design. Remaining: fleet health dashboards, cross-site Multiview, Entra ID OIDC. |

## Cloud portal (Phase 4)

A NexVUE **portal** is a separate box from any edge node — install it with
`sudo ./setup.sh --portal` (Apache + PHP + its own SQLite `portal.db`; no
DeckLink/GStreamer/MediaMTX). Multi-tenant: orgs → portal users
(`org_admin`/`org_operator`/`org_viewer`) → adopted stations → catalog ACL.

**Adopt a station**: on the portal, `/stations` (org_admin) generates a
one-time enrollment code. Paste it into the edge's own Settings → Adopt this
station — the edge calls the portal *outbound* to complete enrollment; the
portal never calls an edge directly, either then or afterward (the recurring
heartbeat is edge-initiated too). Once adopted, the edge keeps working
exactly as before if the portal ever goes down — local admin login, local
share links, and the encoder's own publish credential are untouched by
adoption. Portal viewers browse `/catalog` and watch via `/watch`, which
connects **directly** to the edge's own WHEP endpoint with a portal-minted,
90-second-TTL JWT — video never transits the portal.

The repo is split accordingly: `web-node/` is the edge's own web UI (moved
from repo root, deployed layout on the box unchanged), `web-portal/` is the
portal's, and they never share code at runtime — each is a standalone
deployable.

## Known limitations (accepted for Phase 1)

- No ABR/simulcast: WebRTC congestion control degrades quality on a bad link
  rather than buffering. If field complaints warrant it, publish a second
  low-bitrate rendition per channel (`ch0lo`) and add a quality toggle in the
  portal player.
- WebRTC delivers progressive only — 1080i59.94 sources are deinterlaced at
  ingest (`DEINT_FIELDS` for 59.94p vs 29.97p, `DEINT_METHOD` for algorithm;
  there is no interlaced passthrough).
- JWT-in-query-param (Phase 2) will appear in edge access logs; keep DMZ log
  retention short and TTLs at 60–120 s.
