# CLAUDE.md — NexVUE project context

Working context for AI-assisted development on this repo. Keep this file and
README.md current as the project progresses.

## What this is

**NexVUE** — self-hosted SDI-to-WebRTC gateway replacing Dejero CuePoint.
Per-station edge nodes capture 3G-SDI (DeckLink Quad 2 = 8ch, Duo 2 = 4ch;
card-agnostic via MAX_DEVICES) and/or ingest SRT from Haivision (or other)
encoders (`INPUT_TYPE=srt`, always decode+re-encode), encode with Intel
Quick Sync, and serve sub-250ms WebRTC (WHEP) to browsers. Edge local auth
(bcrypt users, share links, MediaMTX JWT) is on-box today. A future central
portal provides the channel catalog and fleet sync; **video never
transits the portal** — viewers connect directly to edge nodes. Sibling
product to NexAlert.

Packet analysis of a real CuePoint confirmed it is standard WebRTC
(ICE/STUN -> DTLS-SRTP, single muxed UDP port, cloud signaling + local
media) — NexVUE mirrors that architecture, self-hosted.

A `nexvue-metrics` component provides usage/analytics history (bandwidth,
viewers, active streams, input lock/format, per-viewer IP/channel
drill-down, Mon–Sun day-and-hour usage heatmap (equal-date averages), host CPU/memory, CPU/GPU package
temperatures from sysfs hwmon, and Intel iGPU Video/Render engine busy %
for capacity correlation) — explicitly NOT health/uptime alerting (Phase 4
portal ops via outbound heartbeats; DMZ edge cannot pull TRUSTED monitors).
Split deliberately across two pieces with no shared network surface:
`nexvue-metrics-server.py` is a background collector with NO listening port
at all (writes to SQLite only); `nexvue-metrics.php` (runs inside Apache)
reads that SQLite file directly, read-only, and serves JSON. No reverse
proxy, no WebSocket, no new firewall rule — chosen specifically because
this box can't get additional ports opened.

## Architecture (agreed, do not relitigate casually)

- One encode per rendition at the edge; MediaMTX repackages RTSP->WHEP with
  NO transcoding. Codecs are H.264 + Opus because they pass through to
  browsers untouched.
- Per-channel systemd template instances (`nexvue-encode@N`) for independent
  self-healing; MediaMTX and the DeckLink card are the shared components.
- Output caps are NORMALIZED (constant raster/rate per channel) so input
  format changes never renegotiate the encoder or drop viewer sessions.
- Adaptive bandwidth = per-channel LO rendition (tee in the same pipeline —
  DeckLink sub-devices are exclusive-open, never a second process) plus
  player-side loss-driven switching. Defaults: `LO_ENABLE=true`,
  `LO_PRESET=360p` (per-channel override still supported). True simulcast/SFU
  (Ant Media, Janus) is the deliberate back-pocket option, not the plan.
- Channel slots `MAX_CHANNELS` (default 8, ids 0–7) match Quad 2 DeckLink
  `MAX_DEVICES`. Duo 2 uses `MAX_DEVICES=4` and parks unused `@N`. SRT can
  still be hand-configured on any slot via `INPUT_TYPE=srt` in the channel
  `.env` (Settings UI is DeckLink-oriented today).
- Latency target ~200ms glass-to-glass on LAN; ~120ms is the physics floor
  for 1080i sources. Receiver hints (jitterBufferTarget/playoutDelayHint=0)
  are mandatory in any player.
- Closed captions are a **side channel**, not burn-in and not a second video
  stream: extract CEA-608/CC1 in `nexvue-encode` (`output-cc` +
  `ccextractor`/`ccconverter` → FIFO → `nexvue-captions-decode.py` →
  `/run/nexvue/captions/<path>.json`), serve SSE via same-origin
  `nexvue-captions.php`, overlay in Player / Multiview. MediaMTX stays
  H.264+Opus only. A future Phase 1.5 redesign must preserve extraction
  across DeckLink/slate switches if slate returns.

## Phase status

- **Phase 1: hardware-validated on DeckLink Duo 2 (bench) then Quad 2
  (datacenter) + Core Ultra 5 235.** Glass-to-glass latency photo deferred
  (remote rack — no source monitor); working estimate from player RTT
  (~80–140 ms) plus the tuned pipeline budget ≈ ~200 ms target. Confirmed
  working end-to-end: SDK 16 compile, active input detection
  (decklink-status), Quick Sync H.264 encode on Arrow Lake, full SDI ->
  encode -> MediaMTX -> WHEP -> browser chain. TLS enabled across
  WHEP/API/status (three ports) to satisfy an IT-mandated HTTPS-only Apache
  front end; the metrics component needed no such change since it was
  redesigned to have no port at all (collector writes SQLite, PHP-in-Apache
  reads it directly).
  Usage-metrics dashboard (bandwidth/viewers/streams/input-lock/per-viewer
  IP-channel drill-down with column filters — Status/IP/Channel/Duration/
  Data/Client via plain text, `/regex/`, or `>`/`<` comparisons —
  custom from/to ranges, Mon–Sun day-and-hour usage heatmap
  (equal-date averages of observed dates in range; missing telemetry
  excluded), host CPU/memory + Temperature chart (CPU/GPU °C with 95 °C
  limit lines) + iGPU Video engine % (Render % collected but not charted),
  `nexvue-metrics` + `nexvue-metrics.php`) landed
  ahead of schedule — separate from and not a substitute for Phase 4
  portal health via outbound heartbeats.
  Metrics reporting timezone defaults to America/New_York (heatmap buckets,
  chart labels, custom From/To); override with `NEXVUE_METRICS_TZ` only if needed.
  Metrics Kick writes a short-lived registry via `nexvue-ops.php`
  (`kick_viewer` + `kick_check`); Player / Multiview read the WHEP
  `ID` header (API session UUID, not Location secret), suppress self-healing,
  and show an admin disconnect message. Not a rejoin ban — Phase 2 auth owns
  enforcement.
  Selectable CC overlay (CEA-608/CC1 side channel) landed —
  `nexvue-captions-decode.py` + `nexvue-captions.php` + player **CC** toggle
  (`localStorage.nexvue-captions-on`).
  Probe feeds with `nexvue-captions-probe.sh` before assuming 608-in-708.
 Caption display contract: decoder emits ≤2 lines, newest at the bottom
 (608 roll-up presentation); roll-up window tracked per CEA-608 §8.4 —
 PAC base-row moves relocate the window and erase abandoned rows, and
 entering roll-up erases pop-on leftovers, so no stale line can stick.
 Overlay CSS reserves a constant two-line box (no resize jitter) in
 Player / Multiview.
 Caption reliability: decoder is crash-proof per pair; encode treats
 caption `filesink`/EPIPE bus ERROR as non-fatal so a dead FIFO reader
 never systemd-restarts encode; ~16s idle erase
 (`NEXVUE_CAPTIONS_IDLE_ERASE_S`, non-null pairs only) matches CEA-608
 receivers and clears stale text; PHP serves stale-mtime non-empty state
 as cleared (`NEXVUE_CAPTIONS_STALE_S`, 60s);
 SSE disables mod_deflate per-response, sends `retry: 1000`, polls at 50ms.
 The FIFO `filesink` MUST be `buffer-mode=unbuffered`: the default mode
 accumulates ~64KB before flushing and raw 608 arrives at ~60-120 B/s, so
 buffered output starved `nexvue-captions-decode.py` and the browser CC
 overlay stayed empty (same block-buffering class of bug as the
 intel_gpu_top one-shot below).
  iGPU sampling reads a PERSISTENT `intel_gpu_top -J` child (background
  reader thread keeps newest sample, 30s restart backoff, stderr tail
  logged) — never a run-and-kill one-shot: the tool block-buffers stdout on
  a pipe, so short runs died before their first flush and the iGPU charts
  stayed empty on real hardware even though interactive `intel_gpu_top`
  worked. `NEXVUE_INTEL_GPU_TOP_PERIOD_MS` (default 1000) replaced the old
  `NEXVUE_INTEL_GPU_TOP_TIMEOUT_S` knob.
  Remaining before Phase 1 soak is formally "done" (hardware/operator on
  `dcwasof2nexvue01`): re-deploy (`setup.sh` + `nexvue-phase1-deploy-verify.sh`
  for Temperature schema/API/chart), then a clean 72h closeout window.
  Station-wide `MAX_DEVICES` / `MAX_CHANNELS` live in `/etc/nexvue/nexvue.env`.
  Glass-to-glass latency photos remain deferred until on-site/bench access.
- **Phase 1.5: rolled back (slate/selector)** — production ExecStart is
  `nexvue-encode.sh` → `nexvue-encode.py`: persistent MediaMTX publish
  (appsrc → HI/LO encode → RTSP) + disposable DeckLink capture (appsink).
  SDI/`not-negotiated`/exclusive-open races tear down capture only; publish
  holds last-frame then black (`SIGNAL_LOSS_HOLD_S`, default 15s) so WHEP
  stays up. No `input-selector` / slate (`nexvue-supervisor.py` unused).
  Empty ports still auto-park after consecutive never-live unlocks.
  Captions/LO/metrics/ops UI remain. Tests: `test/test_nexvue_encode.py`,
  `test/test-pipeline-assembly.sh`.
  Publish is fed by a self-clocked appsrc pump (`_video_tick`/`_audio_tick`,
  GLib timers) that pushes whatever `_last_video`/`_last_audio` currently
  holds at a fixed cadence — this is *why* WHEP survives capture teardown
  (deliberate; see `SIGNAL_LOSS_HOLD_S` above), but it means the pump's
  declared per-push PTS/duration MUST match how often capture actually
  refreshes that value, or every push mislabels its real sample/frame
  count and the downstream clock quietly drifts — no GStreamer error, just
  steady degradation. Video is fine because `videorate` in the capture
  chain retimes to `output_fps` before the appsink, so one frame arrives
  per output period by construction. Audio was NOT: `_a_dur` (the pump's
  cadence) was wired to `AUDIO_FRAME_MS`, but that setting only sizes
  `opusenc`'s own internal encode frame (its `frame-size` property) — it
  has nothing to do with how capture delivers raw PCM, and nothing in the
  capture audio chain rechunks to a fixed duration (`audiorate` fixes
  sample *rate*, not buffer size; DeckLink delivers embedded audio per
  video-frame callback, not per `AUDIO_FRAME_MS`). Real symptom (all
  channels, both LAN and remote, zero journal errors — nothing crashes,
  the clock just warps): audio/video "stuttering, sounds sluggish" vs. the
  same content on the pre-split-pipeline v1.12 build. Fixed by decoupling
  `_a_dur` from `AUDIO_FRAME_MS` entirely — it now equals `_v_dur`
  (nanosecond-precise, `make_silence_s16_ns`) — `AUDIO_FRAME_MS` still only
  controls `opusenc`'s frame size as documented; GStreamer re-chunks
  arbitrary buffer boundaries internally regardless of how the appsrc
  pushed them, so this doesn't affect the "10ms low-latency Opus frame"
  behavior at all.
- **Phase 2: edge local auth landed** — bcrypt users (admin/operator/sharer/viewer),
  per-user channel ACL, named revocable share links with mandatory expiry
  (Users admin UI + Player/Multiview Share for admin/sharer; sharer sees own
  tokens only; Multiview shares ≤4 channels and auto-tune panes on open;
  Multiview Fullscreen is frameless (html:fullscreen hides chrome/borders);
  raw share token stored for re-copy/email of the same URL; share viewers see
  time-left in the top nav; admin edit + delete revoked/expired;
  expired rows purged 7d after expires_at), MediaMTX JWT + local
  JWKS, long-lived publish JWT in `nexvue.env`, sync-shaped export/import
  on `nexvue-auth.php` for a future portal. Auth SQLite + keys live under
  `/var/lib/nexvue/auth/` (www-data RW; `auth.db` inside that dir so WAL
  works under Apache — `setup.sh` installs Apache/mod_php, bootstraps the
  store, migrates legacy `/var/lib/nexvue/auth.db`, and smokes as www-data).
  MediaMTX JWKS is served on loopback `:9080` (`nexvue-jwks-loopback.conf`)
  so HTTPS redirects cannot break publish/WHEP JWT validation; setup patches
  `mediamtx.yml` and restarts mediamtx + enabled encoders. Auth hot path:
  session snapshot cache (omits `password_hash`) + `session_write_close`
  after checks (captions SSE must not hold the session lock);
  `change_password` forces a DB reload so first-login verify works and
  clears `must_change_password` in session. Aliases read channel `.env`
  without sudo. Player/Multiview start WHEP before waiting on aliases.
  **2.0 front door:**
  Apache DocumentRoot `{webroot}/public` → `index.php` /
  `nexvue-web-router.php` (server-side session/share gate); pages in
  `pages/`; JSON under `/api/*`; station key `NEXVUE_API_KEY` (alias
  `NEXVUE_SYNC_KEY`) for Bearer/`X-NexVUE-Key` sync + `api_ping`. Client gate
  JS remains for nav roles + WHEP JWT. Central catalog portal + Entra OIDC
  remain later. Real (non-self-signed) TLS for Apache + WHEP `:8889` still
  recommended before wider users. UI content is HTTPS-only; WHEP is always
  `https://…:8889` regardless of page scheme (`NexVueAuth.whepUrl()`, not
  `location.protocol`) since MediaMTX's `:8889` listener is TLS-only —
  before that fix, a stray `http://` bookmark inherited `http:` for the WHEP
  fetch too and just got `ERR_CONNECTION_RESET` (Chrome often labels the
  OPTIONS preflight as CORS, which is a red herring). Apache still listens
  on `:80` — `nexvue_web_https_redirect_target()` 301s any non-loopback HTTP
  hit to the same URL under `https://` before any other routing runs; the
  JWKS loopback vhost (`127.0.0.1:9080`) is excluded and stays plain HTTP
  forever. Real incident: `nynycof1nexvue01`, 2026-08-19 — a browser reached
  the player over `http://` and every WHEP POST failed this way.
- **Phase 3: DMZ** — MediaMTX API (`127.0.0.1:9997`) and status
  (`NEXVUE_STATUS_BIND=127.0.0.1:9998`) are loopback-bound; Player uses
  `nexvue-mediamtx-api.php` + `nexvue-status.php`. Remaining: Entra OIDC,
  CORS, portal relays. (TLS landed early — see README TLS section.)
- **Phase 4: fleet / cloud portal — first slice landed.** Repo split:
  `web-node/` holds everything from Phases 1-3 (moved as a group, same
  flat relative layout, zero code changes — `setup.sh` source paths
  updated, deployed box layout unchanged); `web-portal/` is a brand-new,
  fully self-contained app (own SQLite `portal.db`, own RSA signing
  keypair, own bcrypt sessions — never `require_once`s anything from
  `web-node/`) installed on a **separate** box via `sudo ./setup.sh --portal`
  (Apache+PHP only — no DeckLink/GStreamer/MediaMTX/encoder anything).
  Multi-tenant from day one: `orgs` → `portal_users` (`org_admin` /
  `org_operator` / `org_viewer`) → `stations` → `station_channels` →
  `catalog_acl` (NULL channel = all, same convention as edge
  `users.channels`), all org-scoped by construction.
  **Portal becomes the viewer-JWT issuer once a station is adopted** —
  `nexvue-jwks.php` on the edge now serves a **merged** JWKS (local key,
  always present, plus a locally-cached portal key once adopted) via
  `auth_merged_jwks()`; `mediamtx.yml`'s `authJWTJWKS` never changes, no
  network call happens at JWT-verification time, so a portal outage never
  breaks local login, local share links, or the encoder's own
  `NEXVUE_PUBLISH_JWT` (deliberately kept local-only, unaffected by
  adoption). Portal mints viewer JWTs **independently** from its own
  synced catalog/ACL — no live call to the edge per stream-open.
  Enrollment (`portal_enroll` action on `nexvue-auth.php`, admin-gated) and
  the recurring heartbeat (`nexvue-portal-heartbeat.php` +
  `nexvue-portal-heartbeat.timer`, 300s default, runs as plain `www-data` —
  no root needed) are both **edge-initiated outbound only**; no
  portal-to-edge inbound call exists anywhere. Heartbeat pushes station
  status + channel catalog, portal echoes back its current JWKS each time
  (key rotation propagates for free, no separate endpoint). Login page
  shows a non-blocking "sign in via portal" nudge when adopted+reachable
  (`portal_status`, public action) — local sign-in and `/s/<token>` share
  links are never affected either way.
  Portal UI: `/login`, `/catalog` (role-filtered stream list), `/watch`
  (single-stream WHEP viewer — includes the multiopus SDP-munge fix
  extracted into `nexvue-portal-whep.js` since every edge publishes 8ch
  positioned Opus; full VU/CC/stats deferred), `/stations` + `/users`
  (`org_admin`: enrollment-token issuance, portal user + catalog ACL
  management).
  Explicit non-goals for this slice: fleet health dashboards, cross-site
  multi-pane Multiview, org billing/self-serve signup, portal viewers
  landing on the edge's real Player UI (deferred — would need
  `nexvue-web-router.php`'s session gate to accept a portal JWT), instant
  key-rotation/revocation push (bounded by heartbeat interval instead),
  multi-org membership per portal user.

## Known open items / risks

- Empty Quad ports with `nexvue-encode@N` still enabled used to restart-loop
  after Phase 1.5 slate rollback. **Auto-park** disables the slot after
  `AUTO_PARK_UNLOCK_CYCLES` consecutive unlocked starts (default 5;
  `RestartSec=5`). `nexvue-encode.py` retries capture in-process after
  `not-negotiated` while publish stays up. Capture appsinks pull preroll
  (`async=false`); the open gate fails only on ERROR or no frame after
  `DECKLINK_OPEN_GATE_S`, not on a healthy PAUSED preroll.
  Hung `set_state(NULL)` after `DECKLINK_HANG_KILL_S` exits for systemd.
  Capture retry / auto-park probe runs only after capture reaches NULL
  (off-thread); shutdown waits out an in-flight async NULL so the next
  start does not lose the exclusive-open race. The async NULL poll
  (`null_poll_action`) only treats the pipeline as safely down on an actual
  `Gst.State.NULL` — a `Gst.StateChangeReturn.FAILURE` from `get_state()`
  (routine right after a `not-negotiated`/ERROR bus message) re-issues
  `set_state(NULL)` and keeps waiting instead of being treated as "done".
  Getting this wrong wedged a real station (`nynycof1nexvue01`, 2026-08-19):
  the very first not-negotiated race after a restart dropped every
  reference to a not-actually-NULL pipeline, and every subsequent open
  attempt failed `set_state PLAYING` immediately — forever, since a
  locked/busy probe never triggers auto-park (only `unlocked` does).
  `setup.sh` seeds channel `.env` stubs and `enable --now`s
  `mediamtx` / `nexvue-status` / `nexvue-metrics` /
  `nexvue-decklink-configure` / `nexvue-encode@0..(MAX_CHANNELS-1)` (default
  8; Duo: `MAX_CHANNELS=4` disables `@4..7`). Empty ports rely on auto-park;
  Services Enable/Disable still parks/unparks by hand.
- Glass-to-glass latency still unmeasured with a burnt-in clock (datacenter
  deployment — no co-located source monitor). RTT-based estimate recorded in
  README; re-measure on bench when possible. Duo 2 connector-direction notes
  in README remain useful reference if a Duo is ever reinstalled.
- `setup.sh` ensures `/etc/nexvue/tls/{fullchain,privkey}.pem` (creates a
  self-signed pair if missing; never overwrites existing), points Apache
  HTTPS + MediaMTX WHEP/API at those paths (`root:ssl-cert`, key 640).
  Apache HTTP `:80` stays open redirect-only (`000-default` + `Listen 80`
  ensured, not disabled — `nexvue-apache-http-on.py` is the idempotent
  ensure-step, replacing the old `nexvue-apache-http-off.py`); UI content
  itself is HTTPS-only, `nexvue_web_https_redirect_target()` 301s the rest.
  `setup.sh` `enable --now`s `apache2` and `ssh`, allows OpenSSH/80/443/8889/8189
  in ufw, and enables ufw. Self-signed still needs a one-time
  per-browser click-through on `:8889` (trust on `:443` does not extend to
  other ports). Replace the PEMs with a real cert before wider users. Player
  stats/dots use same-origin proxies (`nexvue-mediamtx-api.php`,
  `nexvue-status.php`); `:9997`/`:9998` are loopback-only.
- `decklink-status.cpp`'s active-detection probe takes ~0.7s per IDLE input
  it has to open and test; status daemon poll interval was raised to 5s
  (from 2s) to accommodate, and `STALE_AFTER_S` is set above the helper
  timeout so mid-poll lag does not blank player signal dots. Inputs held by
  a running encoder use the fast status-flag fallback instead, so production
  (encoders running) stays quick.
- `vah264enc` property names confirmed working on this deployment's
  GStreamer/driver combo (Arrow Lake, Ubuntu 24.04 HWE) — `gst-inspect-1.0
  vah264enc` is still the source of truth if a different box rejects a
  property.
- MediaMTX API (:9997) and status daemon (:9998) bind loopback; do not
  open them in ufw. `setup.sh` allows OpenSSH + 80/443/8889/8189
  and enables ufw (`--firewall` re-applies); :80 is redirect-only, never
  content.
- Auto-switch thresholds in `index.html` are conservative first guesses;
  tune from field data.

## Conventions (owner: David McFerrin, davidmcferrin-spec)

- Stacks: bash/Python/PHP + vanilla JS. GNU C++ only where required
  (DeckLink SDK: `decklink-status`, `decklink-audio-probe`,
  `decklink-configure` for Duo/Quad half-duplex BNC mapping — oneshot
  `nexvue-decklink-configure.service` before encode). **No Docker, no Node,
  no frontend frameworks, no Composer.**
- **No pip.** Python is stdlib-only today; if a dependency ever becomes
  necessary, it comes from apt (`python3-<package>`), never pip.
- `setup.sh` is the canonical installer — keep it in sync with any new
  package, file, or unit added to the project. It also brings the station
  up: shared units + encode slots for `MAX_CHANNELS` (see Known open items).
- Dark monospace UI aesthetic (see `index.html` palette) — consistent
  across the tool family (player, multiviewer, metrics, services, channels).
  Light theme via `html[data-theme]` + `localStorage.nexvue-theme` (default
  `dark`); shared `nexvue-ui.js` applies theme before paint and wires the
  nav Light/Dark toggle. Metrics Chart.js colors follow the active theme.
  Top-nav **NexVUE** brand opens a QR of the page URL; optional station logo
  (Settings → Branding) sits to its right when uploaded
  (`/var/lib/nexvue/branding`, served by `nexvue-logo.php`). Player and
  Multiview session metric tiles live in a collapsed bottom drawer
  (Multiview shows the audio-focused pane).
  Top nav: Player / Multiview / Metrics / Services / Settings / Users.
  Login at `/login` (session cookie); share links use `/player?t=` /
  `/multiview?t=` or `/s/<token>` (Multiview shares ≤4 channels, auto-tune
  panes; Fullscreen near-frameless; admin edit name/channels/expiry; delete
  after revoke/expiry; purge 7d post-expiry). Roles: admin
  (Users+Services+Settings+Metrics+all shares), operator (Settings+Metrics),
  sharer / UI **Viewer+Share** (watch + own share links via Player/Multiview
  Share), viewer (watch). Per-user channel ACL on Users (`users.channels`;
  null = all). MediaMTX JWT via local JWKS; encoders use `NEXVUE_PUBLISH_JWT`.
  Player/Multiview **CC** uses `nexvue-captions.js` + SSE (not WHEP text
  tracks). Player/Multiview **VU / audio program** use `nexvue-vu.js`
  (Web Audio on the WHEP MediaStream). Top-bar **VU** toggle (like CC)
  shows/hides the meter overlay (`localStorage.nexvue-vu-on`, default off).
  First-visit audio defaults: volume 20%, muted. Encode always opens DeckLink
  8ch and publishes 8ch positioned Opus
  (default `AUDIO_BITRATE_BPS=384000`) tee'd to HI+LO. No 16ch path.
  `AUDIO_LAYOUT` is a player role preset only; `AUDIO_EMBEDS` (Settings
  checkboxes) gates which embeds the browser VU offers — metadata only.
  Positioned channels are mandatory: decklinkaudiosrc emits channel-mask=0,
  unpositioned multichannel encodes Opus family 255 (no RTP payloader).
  Fix is per-branch mono channel-masks on deinterleave/interleave → family 1
  / MULTIOPUS. Player / Multiview WHEP offers are SDP-munged (`nexvue-vu.js`
  `mungeWhepOfferSdp`) for multiopus 3–8. Settings **Detect audio…**
  (`audio_probe` → `decklink-audio-probe`) suggests role + embeds for
  operator confirm. Per-browser Main↔SAP and 5.1 fold (`localStorage`) —
  never changes encode or other viewers.
- Ops pages (`services.html`, `channels.html`) use `nexvue-ops.php` +
 allowlisted sudo wrappers. Logo upload/delete is www-data direct write
 (no sudo). Settings channel editor (and bulk edit) shows only live encode/player
 knobs with human labels; audio-on / LO-on reveal dependent fields;
 Advanced is collapsed. Per-channel `DEINT_METHOD` (default yadif) sits next
 to `DEINT_FIELDS` for 1080i→p quality. Field labels show a ~2s hover/focus tip
 (`#field-tip`) with purpose, recommended range, and blank semantics —
  same delay pattern as Player `#stat-tip`. Requires admin/operator session
  (not share links). Services is admin-only.
 Services shows systemd enable state (`nexvue-ops-status.sh` prints
 `<is-active> <is-enabled>`) plus Enable/Disable (`set_enabled`, --now) and
 Start/Stop (`set_running`, runtime-only) toggles for `nexvue-encode@0-7`
 ONLY (`nexvue-ops-enable.sh` verbs enable|disable|start|stop) — never the
 shared units. **Clear journal…** (`journal_clear`) records a per-unit
 watermark via `nexvue-ops-journal.sh clear` so the Services journal view
 hides prior lines for the selected unit only (systemd cannot purge one
 unit from the binary journal; host-wide vacuum was removed). **Download
 zip…** (`support_bundle`) builds a redacted journals+config+metrics+state
 zip via `nexvue-ops-support-bundle.sh` → `nexvue-support-bundle.py`
 (hours 1|6|12|24|48|72; output `/var/lib/nexvue/support`, 24h retention).
 **Update from repo…** (`update_status` / `update_repo`) runs
 `nexvue-ops-update.sh`: fetch + hard-reset to `origin/$NEXVUE_UPDATE_BRANCH`
 (default `main`) using `/etc/nexvue/repo.path`, then `setup.sh`. Status line
 is `vX.Y.Z · up to date` or `vX.Y.Z → vA.B.C · update available` (no SHA /
 dirty); confirm dialog shows `remote_version` + commit-subject changelog
 (`HEAD..origin/<branch>`). Semver lives in repo `VERSION`; top-nav badge via
 `nexvue-version.php` + `/var/lib/nexvue/version.json`.
 Settings Channel list shows LO yes/no and
 **Restart all encoders** (`restart_encoders`: systemd-enabled encode slots
 only); Services has the same bulk restart. Channel editor **Factory
 defaults…** writes concrete built-in defaults via `channel_put` (no
 blank/unset in the form; identity keys untouched). Disable and Stop both run
 `reset-failed` so a parked encoder
 doesn't show stale red "failed"; any disabled + not-running unit (even
 with a stale `failed` from an SSH-side disable) renders neutral
 "disabled", not red, on Services and Settings — `failed` is red only when
 enabled.
- Channel `.env` files are SOURCED by bash (`nexvue-encode@.service`
 ExecStart), so values with spaces MUST be double-quoted —
 `CHANNEL_ALIAS=TVU 35` unquoted runs `35` as a command and truncates the
 alias to `TVU` (journal tell: `N.env: line NN: 35: command not found`).
 `nexvue-ops-env-update.py` quotes on write and unquotes on read; non-alias
 values reject quote characters so the quoting can't be broken.
- Production-ready code only: no placeholders, no TODOs. Unit tests for new
  or changed logic (`test/`). Complete file rewrites over accumulated diffs.
- Architecture decisions confirmed with the owner before code.
- Keep README.md and this file updated with every meaningful change.
- **Bump `VERSION` (semver) with every meaningful change** — top-nav badge and
  Update-from-repo stamp; see `.cursor/rules/version-bump.mdc`.
