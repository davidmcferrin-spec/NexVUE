# CLAUDE.md — NexVUE project context

Working context for AI-assisted development on this repo. Keep this file and
README.md current as the project progresses.

## What this is

**NexVUE** — self-hosted SDI-to-WebRTC gateway replacing Dejero CuePoint.
Per-station edge nodes capture 3G-SDI (DeckLink Quad 2 = 8ch, Duo 2 = 4ch;
card-agnostic via MAX_DEVICES) and/or ingest SRT from Haivision (or other)
encoders (`INPUT_TYPE=srt`, always decode+re-encode), encode with Intel
Quick Sync, and serve sub-250ms WebRTC (WHEP) to browsers. A future central
portal (Phase 2) provides the channel catalog and auth; **video never
transits the portal** — viewers connect directly to edge nodes. Sibling
product to NexAlert.

Packet analysis of a real CuePoint confirmed it is standard WebRTC
(ICE/STUN -> DTLS-SRTP, single muxed UDP port, cloud signaling + local
media) — NexVUE mirrors that architecture, self-hosted.

A `nexvue-metrics` component provides usage/analytics history (bandwidth,
viewers, active streams, input lock/format, per-viewer IP/channel
drill-down, Mon–Sun day-and-hour usage heatmap (equal-date averages), host CPU/memory, CPU/GPU package
temperatures from sysfs hwmon, and Intel iGPU Video/Render engine busy %
for capacity correlation) — explicitly NOT the health/uptime monitoring
planned for CheckMK in Phase 4. Split deliberately across two pieces with
no shared
network surface: `nexvue-metrics-server.py` is a background collector with
NO listening port at all (writes to SQLite only); `nexvue-metrics.php`
(runs inside Apache) reads that SQLite file directly, read-only, and serves
JSON. No reverse proxy, no WebSocket, no new firewall rule — chosen
specifically because this box can't get additional ports opened.

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
  player-side loss-driven switching. Station-wide floating pool
  `MAX_LO_RENDITIONS` (default 6): Settings rejects a 7th `LO_ENABLE=true`;
  supervisor clamps deterministically (ascending channel id among
  requesters). True simulcast/SFU (Ant Media, Janus) is the deliberate
  back-pocket option, not the plan.
- Channel slots `MAX_CHANNELS` (default 10, ids 0–9) are independent of
  DeckLink `MAX_DEVICES` — SRT-only channels can use `@8`/`@9` without a
  fake connector index.
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
  ahead of schedule — separate from and not a
  substitute for the Phase 4 CheckMK health-monitoring plan below.
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
  Station-wide `MAX_DEVICES` / `MAX_CHANNELS` /
  `MAX_LO_RENDITIONS` live in `/etc/nexvue/nexvue.env`.
  Glass-to-glass latency photos remain deferred until on-site/bench access.
- **Phase 1.5: rolled back** — `nexvue-encode@.service` ExecStart is again
  `nexvue-encode.sh` (gst-launch DeckLink → MediaMTX), the path that was
  stable in Phase 1. `nexvue-supervisor.py` (slate / `input-selector` /
  SRT) stays in the tree for a future redesign only — systemd does **not**
  run it. Accepted trade-off: unlocked inputs restart or show black instead
  of a NO SIGNAL slate — park empty ports via Services Enable/Disable.
  Captions, LO, metrics, and ops UI are unchanged. Assembly tests:
  `test/test-pipeline-assembly.sh`.
- **Phase 2: PHP portal** — channel catalog, local bcrypt + JWT issuance,
  MediaMTX JWKS auth. Open decision: publisher auth pattern (long-lived
  publish JWT vs authMethod:http with loopback exemption) — see mediamtx.yml.
  Real (non-self-signed) TLS cert for 8889/9997/9998 belongs here too, to
  drop the per-browser self-signed click-through noted in the TLS section.
- **Phase 3: DMZ** — webrtcAdditionalHosts, bind MediaMTX API and status
  daemon back to loopback (portal relays stats), Entra OIDC, CORS. (TLS
  itself landed early, in Phase 1, ahead of schedule — see README TLS section.)
- **Phase 4: fleet** — config mgmt, CheckMK checks (status daemon JSON +
  MediaMTX /v3/paths/list), portal ops dashboard via outbound heartbeats.

## Known open items / risks

- Empty Quad ports with `nexvue-encode@N` still enabled restart-loop
  (`RestartSec=3`) — Phase 1.5 slate was rolled back. Enable encoders only
  for patched Input connectors; closeout script warns and prints
  `disable --now` for unlocked+active channels. Parking is also doable
  from the Services page Enable/Disable toggle (encoder units only).
- Glass-to-glass latency still unmeasured with a burnt-in clock (datacenter
  deployment — no co-located source monitor). RTT-based estimate recorded in
  README; re-measure on bench when possible. Duo 2 connector-direction notes
  in README remain useful reference if a Duo is ever reinstalled.
- Self-signed TLS cert (Apache's `ssl-cert-snakeoil` or similar) on
  8889/9997 requires a one-time per-browser click-through — fine for
  bench testing, plan a real cert before this reaches other users.
  Player input-status dots no longer need a separate `:9998` trust click
  (`nexvue-status.php` same-origin proxy); `:9998` TLS remains optional for
  direct daemon clients / metrics collector URL scheme.
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
- MediaMTX API (:9997) and status daemon (:9998) are LAN-trust in Phase 1
  config; MUST be loopback-bound before DMZ exposure (Phase 3).
- Auto-switch thresholds in `index.html` are conservative first guesses;
  tune from field data.

## Conventions (owner: David McFerrin, davidmcferrin-spec)

- Stacks: bash/Python/PHP + vanilla JS. GNU C++ only where required
  (DeckLink SDK). **No Docker, no Node, no frontend frameworks, no Composer.**
- **No pip.** Python is stdlib-only today; if a dependency ever becomes
  necessary, it comes from apt (`python3-<package>`), never pip.
- `setup.sh` is the canonical installer — keep it in sync with any new
  package, file, or unit added to the project.
- Dark monospace UI aesthetic (see `index.html` palette) — consistent
  across the tool family (player, multiviewer, metrics, services, channels).
  Light theme via `html[data-theme]` + `localStorage.nexvue-theme` (default
  `dark`); shared `nexvue-ui.js` applies theme before paint and wires the
  nav Light/Dark toggle. Metrics Chart.js colors follow the active theme.
  Top-nav **NexVUE** brand opens a QR of the page URL; optional station logo
  (Settings → Branding) sits to its right when uploaded
  (`/var/lib/nexvue/branding`, served by `nexvue-logo.php`). Player session
  tiles live in a collapsed bottom drawer.
  Top nav: Player / Multiview / Metrics / Services / Settings.
  Player/Multiview **CC** uses `nexvue-captions.js` + SSE (not WHEP text
  tracks).
- Ops pages (`services.html`, `channels.html`) use `nexvue-ops.php` +
 allowlisted sudo wrappers. Logo upload/delete is www-data direct write
 (no sudo). Settings channel editor (and bulk edit) field labels show a
 ~2s hover/focus tip (`#field-tip`) with purpose, recommended range, and
 blank semantics — same delay pattern as Player `#stat-tip`. Phase 1
 LAN-trust — not for DMZ without auth.
 Services shows systemd enable state (`nexvue-ops-status.sh` prints
 `<is-active> <is-enabled>`) plus Enable/Disable (`set_enabled`, --now) and
 Start/Stop (`set_running`, runtime-only) toggles for `nexvue-encode@0-9`
 ONLY (`nexvue-ops-enable.sh` verbs enable|disable|start|stop) — never the
 shared units. Settings Channel list shows LO yes/no (pool-denied) and
 **Restart all encoders** (`restart_encoders`: systemd-enabled encode slots
 only); Services has the same bulk restart. Disable and Stop both run
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
