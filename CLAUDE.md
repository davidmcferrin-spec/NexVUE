# CLAUDE.md — NexVUE project context

Working context for AI-assisted development on this repo. Keep this file and
README.md current as the project progresses.

## What this is

**NexVUE** — self-hosted SDI-to-WebRTC gateway replacing Dejero CuePoint.
Per-station edge nodes capture 3G-SDI (DeckLink Quad 2 = 8ch, Duo 2 = 4ch;
card-agnostic via MAX_DEVICES), encode with Intel
Quick Sync, and serve sub-250ms WebRTC (WHEP) to browsers. A future central
portal (Phase 2) provides the channel catalog and auth; **video never
transits the portal** — viewers connect directly to edge nodes. Sibling
product to NexAlert.

Packet analysis of a real CuePoint confirmed it is standard WebRTC
(ICE/STUN -> DTLS-SRTP, single muxed UDP port, cloud signaling + local
media) — NexVUE mirrors that architecture, self-hosted.

A `nexvue-metrics` component provides usage/analytics history (bandwidth,
viewers, active streams, input lock/format, per-viewer IP/channel
drill-down, Mon–Fri hour-of-day heatmap, host CPU/memory for capacity
correlation) — explicitly NOT the health/uptime monitoring planned for
CheckMK in Phase 4. Split deliberately across two pieces with no shared
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
  player-side loss-driven switching. True simulcast/SFU (Ant Media, Janus)
  is the deliberate back-pocket option, not the plan.
- Latency target ~200ms glass-to-glass on LAN; ~120ms is the physics floor
  for 1080i sources. Receiver hints (jitterBufferTarget/playoutDelayHint=0)
  are mandatory in any player.

## Phase status

- **Phase 1: hardware-validated on real DeckLink Duo 2 + Core Ultra 5 235,
  latency measurement still pending.** Confirmed working end-to-end: SDK 16
  compile, active input detection (decklink-status), Quick Sync H.264 encode
  on Arrow Lake, full SDI -> encode -> MediaMTX -> WHEP -> browser chain with
  real video on device-number 2. TLS enabled across WHEP/API/status (three
  ports) to satisfy an IT-mandated HTTPS-only Apache front end; the metrics
  component needed no such change since it was redesigned to have no port at
  all (collector writes SQLite, PHP-in-Apache reads it directly).
  Usage-metrics dashboard (bandwidth/viewers/streams/input-lock/per-viewer
  IP-channel drill-down, custom from/to ranges, weekday heatmap, host
  CPU/memory, `nexvue-metrics` + `nexvue-metrics.php`) landed
  ahead of schedule — separate from and not a
  substitute for the Phase 4 CheckMK health-monitoring plan below.
  Remaining before Phase 1 is formally "done": burnt-in-clock latency
  measurement, flip the two Duo 2 connectors still set to Output back to
  Input (see README "DeckLink Duo 2 connector direction"), 72h soak.
- **Phase 1.5 (next): Python supervisor** — persistent RTSP session with
  DeckLink/slate input switching ("NO SIGNAL" burn-in) so no-signal-at-boot
  serves a slate instead of a restart loop. Spec review before code.
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

- Two of four Duo 2 connectors are still configured as Output, not Input —
  card config task, not code. See README "DeckLink Duo 2 connector direction".
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
  Top-nav **NexVUE** brand opens a QR of the page URL; player session tiles
  live in a collapsed bottom drawer. Player **Cast** uses a custom WHEP
  receiver (`cast-receiver.html`) — Chromecast cannot cast WebRTC
  `srcObject` directly.
  Top nav: Player / Multiview / Metrics / Services / Channels.
- Ops pages (`services.html`, `channels.html`) use `nexvue-ops.php` +
  allowlisted sudo wrappers. Phase 1 LAN-trust — not for DMZ without auth.
- Production-ready code only: no placeholders, no TODOs. Unit tests for new
  or changed logic (`test/`). Complete file rewrites over accumulated diffs.
- Architecture decisions confirmed with the owner before code.
- Keep README.md and this file updated with every meaningful change.
