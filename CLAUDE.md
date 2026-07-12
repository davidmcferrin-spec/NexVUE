# CLAUDE.md — NexVUE project context

Working context for AI-assisted development on this repo. Keep this file and
README.md current as the project progresses.

## What this is

**NexVUE** — self-hosted SDI-to-WebRTC gateway replacing Dejero CuePoint.
Per-station edge nodes capture 8x 3G-SDI (DeckLink Quad 2), encode with Intel
Quick Sync, and serve sub-250ms WebRTC (WHEP) to browsers. A future central
portal (Phase 2) provides the channel catalog and auth; **video never
transits the portal** — viewers connect directly to edge nodes. Sibling
product to NexAlert.

Packet analysis of a real CuePoint confirmed it is standard WebRTC
(ICE/STUN -> DTLS-SRTP, single muxed UDP port, cloud signaling + local
media) — NexVUE mirrors that architecture, self-hosted.

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

- **Phase 1 (this repo): complete, NOT yet hardware-tested.** Exit criteria:
  measured <=~200ms via burnt-in clock method, 72h zero-restart soak.
- **Phase 1.5 (next): Python supervisor** — persistent RTSP session with
  DeckLink/slate input switching ("NO SIGNAL" burn-in) so no-signal-at-boot
  serves a slate instead of a restart loop. Spec review before code.
- **Phase 2: PHP portal** — channel catalog, local bcrypt + JWT issuance,
  MediaMTX JWKS auth. Open decision: publisher auth pattern (long-lived
  publish JWT vs authMethod:http with loopback exemption) — see mediamtx.yml.
- **Phase 3: DMZ** — TLS :443, webrtcAdditionalHosts, bind MediaMTX API and
  status daemon back to loopback (portal relays stats), Entra OIDC, CORS.
- **Phase 4: fleet** — config mgmt, CheckMK checks (status daemon JSON +
  MediaMTX /v3/paths/list), portal ops dashboard via outbound heartbeats.

## Known open items / risks

- `decklink-status.cpp` compiles against the DeckLink SDK, which was not
  available at authoring time — expect possible minor enum/API fixes on
  first `make` against a given SDK version.
- `vah264enc` property names verified for GStreamer 1.24; VA plugins have
  shifted between releases — `gst-inspect-1.0 vah264enc` is the source of
  truth if properties are rejected.
- MediaMTX API (:9997) and status daemon (:9998) are LAN-trust in Phase 1
  config; MUST be loopback-bound before DMZ exposure (Phase 3).
- Auto-switch thresholds in test-player.html are conservative first guesses;
  tune from field data.

## Conventions (owner: David McFerrin, davidmcferrin-spec)

- Stacks: bash/Python/PHP + vanilla JS. GNU C++ only where required
  (DeckLink SDK). **No Docker, no Node, no frontend frameworks, no Composer.**
- **No pip.** Python is stdlib-only today; if a dependency ever becomes
  necessary, it comes from apt (`python3-<package>`), never pip.
- `setup.sh` is the canonical installer — keep it in sync with any new
  package, file, or unit added to the project.
- Dark monospace UI aesthetic (see test-player.html palette) — consistent
  across the tool family.
- Production-ready code only: no placeholders, no TODOs. Unit tests for new
  or changed logic (`test/`). Complete file rewrites over accumulated diffs.
- Architecture decisions confirmed with the owner before code.
- Keep README.md and this file updated with every meaningful change.
