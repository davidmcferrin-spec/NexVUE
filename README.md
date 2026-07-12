# NexVUE — Edge Node (Phase 1)

**NexVUE** — self-hosted SDI-to-WebRTC return-feed and remote-monitoring
gateway (sibling of NexAlert). One edge node per station:
DeckLink Quad 2 (8x 3G-SDI in) -> GStreamer (deinterlace + Quick Sync H.264 +
Opus) -> MediaMTX -> WHEP (WebRTC) to any browser.

Phase 1 scope: single node, LAN only, no TLS, no auth. Proves ingest,
encode stability, and latency numbers before the portal (Phase 2) and DMZ
exposure (Phase 3) are built.

```
SDI 1080i59.94 x8 --> [DeckLink Quad 2]
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
- Blackmagic DeckLink Quad 2 in the PCIe 4.0 x16 slot (card is Gen2 x8)
- 8x DIN 1.0/2.3-to-BNC breakout cables — the Quad 2 has mini connectors,
  NOT full-size BNC; easy to leave off the PO, painful to be missing
- Ubuntu 24.04 LTS Server
- Optional: HP Care Pack to 3yr for unattended remote sites (base is 1/1/1)

Capacity guidance: 8x 1080p59.94 HI encodes (plus enabled LO renditions) is
near the practical ceiling for the Arrow Lake media engine.
Run motion-critical channels (program, director) at 59.94p (`DEINT_FIELDS=all`)
and monitoring channels (multiview, prompter) at 29.97p (`DEINT_FIELDS=top`) to
stay comfortably inside it.

## Install

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
  intel-media-va-driver-non-free vainfo
```

Verify Quick Sync is visible (expect H264 encode entrypoints under iHD driver):

```bash
vainfo | grep -i h264
```

If vainfo shows no encode entrypoints on Arrow Lake, the repo media driver is
too old — install `intel-media-va-driver-non-free` from Intel's own apt
repository (or the kisak PPA) and re-check. `nexvue-encode.sh` fails loudly
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

### 4. This package

```bash
sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin nexvue

sudo mkdir -p /etc/nexvue/channels
sudo cp mediamtx.yml /etc/nexvue/
sudo cp nexvue-encode.sh /usr/local/bin/ && sudo chmod 755 /usr/local/bin/nexvue-encode.sh
sudo cp mediamtx.service nexvue-encode@.service /etc/systemd/system/

# One env file per channel you want live (see channels-example.env):
sudo cp channels-example.env /etc/nexvue/channels/0.env
sudo nano /etc/nexvue/channels/0.env   # set DEVICE_NUMBER=0, CHANNEL_PATH=ch0

sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx nexvue-encode@0
```

Add channels by creating `1.env` .. `7.env` and enabling `nexvue-encode@1` .. `@7`.

### 5. Input status daemon (signal/reference display in the player)

Requires the Blackmagic **DeckLink SDK** (separate download from Desktop
Video — "Desktop Video SDK" on the same support page). Then:

```bash
make DECKLINK_SDK=/path/to/Blackmagic_DeckLink_SDK_14.x
sudo make install                       # -> /usr/local/bin/decklink-status
/usr/local/bin/decklink-status          # sanity: JSON with your 8 inputs

sudo cp nexvue-status-server.py /usr/local/bin/ \
  && sudo chmod 755 /usr/local/bin/nexvue-status-server.py
sudo cp nexvue-status.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nexvue-status
curl -s http://127.0.0.1:9998/status   # sanity: same JSON via HTTP
```

Optional but recommended — the player degrades gracefully (dots grey,
"n/a" tiles) if this isn't running.

## Verify

```bash
systemctl status mediamtx nexvue-encode@0
journalctl -fu nexvue-encode@0
```

Then from a LAN machine:

- **Built-in player:** `http://<edge-ip>:8889/ch0`
- **Test player with stats:** open `test-player.html`, set the edge URL,
  click a channel. Gives resolution/fps, bitrate, RTT, jitter buffer, loss.

### Latency measurement (do this properly once)

Point a channel's SDI source at a burnt-in timecode or a clock, put the WHEP
player next to the source monitor, photograph both in one frame, subtract.
Repeat at 59.94p and 29.97p settings, and with `ENABLE_AUDIO` on and off.
Target: **~200 ms** on LAN with the tuning below; treat >300 ms as a bug.

### Latency budget & tuning

Approximate steady-state budget on LAN, 1080i59.94 source, tuned defaults:

| Stage                              | Cost      | Knob                                   |
|------------------------------------|-----------|----------------------------------------|
| SDI frame capture (interlaced)     | ~33 ms    | physics — none                         |
| DeckLink driver queue              | ~33 ms    | `DECKLINK_BUFFER_FRAMES=2` (default 5 allows up to ~165 ms) |
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

Leave all populated channels running for 72h before calling Phase 1 done:

```bash
journalctl -u 'nexvue-encode@*' --since -72h | grep -ci restart   # want 0
```

Watch for iGPU thermal throttling (`intel_gpu_top`) with all 8 channels hot.

## Operational notes

- **Input status & reference:** the `nexvue-status` daemon (port 9998) polls the
  DeckLink Status API via the `decklink-status` helper and serves JSON
  (per-input signal lock + detected format, genlock reference lock + mode).
  The test player shows this as green/red dots per channel plus SDI input and
  Reference tiles. Build the helper first: download the Blackmagic DeckLink
  SDK, then `make DECKLINK_SDK=/path/to/sdk && sudo make install`, and
  `systemctl enable --now nexvue-status`. Status queries coexist safely with an
  active capture.
- **LO renditions (adaptive bandwidth):** `LO_ENABLE=true` in a channel env
  publishes `<path>lo` (default 720p29.97 @ 1.2 Mbps) alongside the HI
  rendition — one capture, two QSV encodes via tee. Viewers on bad links get
  switched to it by the portal player (Phase 2). Enable per channel, not
  globally: each LO adds an encode, and the practical envelope is ~8 HI
  59.94p + 8 LO on the Arrow Lake media engine. Verify in the soak.
- **Self-healing model:** constant output caps mean input format changes never
  drop viewer sessions; the watchdog turns capture hangs into clean systemd
  restarts; black frames ride through signal loss. Remaining known gap: a
  channel with no signal at boot serves nothing (restart loop) rather than a
  slate — closing that is the Phase 1.5 Python supervisor (persistent RTSP
  session, DeckLink/slate input switching, "NO SIGNAL" burn-in).
- **Signal-present alarming** belongs in CheckMK (Phase 4): the status daemon
  JSON is the data source (local check or HTTP agent), alongside the
  MediaMTX API (`/v3/paths/list`) for stream/session state.
- **Format changes:** normalized away — output caps are constant per channel.
- **No Docker, no Node** — two binaries, two scripts, systemd.

## Phase roadmap (agreed architecture)

| Phase | Scope |
|---|---|
| 1 (this) | Single edge, LAN WHEP, no auth. Prove stability + latency. |
| 2 | PHP portal: channel catalog, local bcrypt auth, JWT issuance, MediaMTX JWKS integration. Decide publisher-auth pattern (see comment in `mediamtx.yml`). |
| 3 | DMZ exposure: TLS on 443, `webrtcAdditionalHosts` = public FQDN, single UDP 8189 rule + ICE-TCP fallback, Entra ID OIDC at portal, CORS validation portal-origin -> edge. |
| 4 | Fleet rollout: per-station config management, CheckMK checks (encoder-alive, signal-present, session counts), portal ops dashboard fed by outbound edge heartbeats. |

## Known limitations (accepted for Phase 1)

- No ABR/simulcast: WebRTC congestion control degrades quality on a bad link
  rather than buffering. If field complaints warrant it, publish a second
  low-bitrate rendition per channel (`ch0lo`) and add a quality toggle in the
  portal player.
- WebRTC delivers progressive only — 1080i59.94 sources are deinterlaced at
  ingest (that's the `DEINT_FIELDS` setting; there is no interlaced passthrough).
- JWT-in-query-param (Phase 2) will appear in edge access logs; keep DMZ log
  retention short and TTLs at 60–120 s.
