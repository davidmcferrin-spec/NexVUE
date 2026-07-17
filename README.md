# NexVUE — Edge Node (Phase 1)

**NexVUE** — self-hosted SDI-to-WebRTC return-feed and remote-monitoring
gateway (sibling of NexAlert). One edge node per station:
DeckLink capture card (4 or 8x 3G-SDI in) -> GStreamer (deinterlace +
Quick Sync H.264 + Opus) -> MediaMTX -> WHEP (WebRTC) to any browser.

Supported capture cards (channel count set by `MAX_DEVICES` per channel env):
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
  **Duo 2 (4 ch)** works identically — set `MAX_DEVICES=4` and enable only
  `nexvue-encode@0..3`. The Duo 2 Mini (low-profile) is the pick if the
  chassis only takes half-height cards (e.g. an SFF box).
- DIN 1.0/2.3-to-BNC breakout cables, one per channel (8 for Quad 2, 4 for
  Duo 2) — the cards have mini connectors, NOT full-size BNC; easy to leave
  off the PO, painful to be missing
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
sudo nano /etc/nexvue/channels/0.env
#   (inline '# comments' and whitespace in the env file are fine —
#    the unit sources it through a shell)   # set DEVICE_NUMBER=0, CHANNEL_PATH=ch0
                                       # (Duo 2: also set MAX_DEVICES=4)

sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx nexvue-encode@0
```

Add channels by creating `1.env` .. `7.env` and enabling `nexvue-encode@1` .. `@7`.

### 5. Input status daemon (signal/reference display in the player)

Requires the Blackmagic **DeckLink SDK** (separate download from Desktop
Video — "Desktop Video SDK" on the same support page). Use the **same major
version** as the installed Desktop Video driver (e.g. DV 16 + SDK 16 — DV 16
is current and preferred on the HWE kernel). Then:

```bash
make DECKLINK_SDK=/path/to/Blackmagic_DeckLink_SDK_16.x
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

## Firewall (ufw)

Ubuntu's `ufw` is default-deny once enabled, so the service ports must be
opened explicitly. Port/protocol map:

| Port      | Proto     | Scope        | Purpose                                   |
|-----------|-----------|--------------|-------------------------------------------|
| 8889      | TCP       | viewers      | WHEP signaling (HTTP POST/PATCH/DELETE)   |
| 8189      | UDP + TCP | viewers      | WebRTC media (UDP) + ICE-TCP fallback     |
| 9997      | TCP       | LAN mgmt     | MediaMTX API (viewer counts, egress)      |
| 9998      | TCP       | LAN mgmt     | Status daemon (input/reference JSON)      |
| 9999      | —         | **loopback** | Metrics dashboard — reached via Apache proxy on 443 (default); see Usage Metrics section for the direct-access alternative |
| 80 / 443  | TCP       | viewers      | Apache serving the player page            |
| 8554      | —         | **loopback** | RTSP ingest — do NOT open (127.0.0.1 only)|

Two things people get wrong here: the WebRTC **media** port (8189) needs
**both UDP and TCP** — UDP carries the media, TCP is the fallback for
viewers on UDP-hostile networks — and it is a *different* port from the WHEP
signaling port (8889). Opening only 8889 gets you a session that negotiates
then plays nothing.

### Phase 1 (trusted LAN) — open to everyone on the subnet

```bash
sudo ufw allow 80/tcp comment 'NexVUE player (Apache)'
sudo ufw allow 8889/tcp comment 'NexVUE WHEP signaling'
sudo ufw allow 8189 comment 'NexVUE WebRTC media (UDP+TCP)'
sudo ufw allow 9997/tcp comment 'NexVUE MediaMTX API'
sudo ufw allow 9998/tcp comment 'NexVUE status daemon'
sudo ufw enable
# 9999 (metrics) is intentionally NOT opened — it's loopback-only by default
# and reached via the Apache proxy on 443. See "Usage Metrics Dashboard" for
# the direct-access alternative if you'd rather open the port instead.
sudo ufw status verbose
```

(`ufw allow 8189` with no proto opens both UDP and TCP, which is what the
media port needs.)

### Tighter: restrict the management ports to your ops subnet

The API (9997) and status daemon (9998) expose viewer/session data and, in
the case of the MediaMTX API, session-kick and config endpoints — no auth in
Phase 1. Limit them to the engineering subnet rather than the whole LAN
(metrics/9999 isn't listed here — it's loopback-only by default, see above):

```bash
sudo ufw allow from 10.200.0.0/16 to any port 9997 proto tcp comment 'NexVUE API (ops only)'
sudo ufw allow from 10.200.0.0/16 to any port 9998 proto tcp comment 'NexVUE status (ops only)'
```

### Phase 3 (DMZ) — viewers only, management goes loopback

In the DMZ the three management ports must NOT be reachable at all (bind
them to loopback in config; the portal relays their data via outbound
heartbeat). Open only what viewers need, and 443 replaces 8889 once TLS is on:

```bash
sudo ufw allow 443/tcp comment 'NexVUE WHEP signaling (TLS)'
sudo ufw allow 8189 comment 'NexVUE WebRTC media (UDP+TCP)'
# 9997/9998 intentionally NOT opened — loopback only in DMZ
# 9999 (metrics) is loopback-only by default already, nothing changes here
```

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

Watch for iGPU thermal throttling (`intel_gpu_top`) with all channels hot
(8 on Quad 2, 4 on Duo 2).

## TLS / HTTPS (all-or-nothing across all four ports)

If Apache serving the player page is put behind TLS (including by IT-security
mandate), **every service the page talks to must also be TLS**, or the
browser blocks the mismatched requests. This is not optional once the page is
HTTPS — browsers refuse ALL plain-HTTP fetches from an HTTPS page (mixed
content), and separately, each `scheme://host:port` is checked independently,
so getting one port wrong throws a different, confusing error than the others.

There are effectively FOUR independent TLS switches to flip — a common
mistake is enabling some and assuming the rest inherited it. They did not:

| Service | Port | Config key(s) | Symptom if forgotten |
|---|---|---|---|
| MediaMTX WHEP (viewers)   | 8889 | `webrtcEncryption`, `webrtcServerKey/Cert` | Mixed-content block (if page is HTTPS) |
| MediaMTX Control API      | 9997 | `apiEncryption`, `apiServerKey/Cert` — **separate from webrtcEncryption above, does NOT inherit it** | `ERR_SSL_PROTOCOL_ERROR` |
| NexVUE status daemon      | 9998 | `NEXVUE_STATUS_TLS_CERT`/`_KEY` env vars on the systemd unit | `ERR_SSL_PROTOCOL_ERROR` |
| NexVUE metrics dashboard  | 9999 | Only relevant if you opt into direct access (`NEXVUE_METRICS_BIND=0.0.0.0`) — default is loopback-only, reached via the Apache proxy instead, no TLS needed for that hop | `ERR_SSL_PROTOCOL_ERROR` if half-configured (direct-access mode only) |

`ERR_SSL_PROTOCOL_ERROR` specifically means the browser tried a TLS
handshake against a server that's still answering plain HTTP — i.e. that
particular switch wasn't actually flipped (or the unit wasn't reloaded after
editing). A generic mixed-content *console warning* (not a network error)
means the page is HTTPS and a request is plain HTTP with no TLS attempted at
all.

### Steps (assumes Apache's TLS is already working)

1. **Locate Apache's cert/key:**
   ```bash
   sudo grep -ri "SSLCertificateFile\|SSLCertificateKeyFile" /etc/apache2/sites-enabled/*.conf
   ```
2. **Copy them somewhere the `nexvue` user can read** (Apache's key is
   normally root-only or `ssl-cert`-group-only; don't loosen Apache's own
   permissions — copy instead):
   ```bash
   sudo mkdir -p /etc/nexvue/tls
   sudo cp /path/to/fullchain.pem /etc/nexvue/tls/fullchain.pem
   sudo cp /path/to/privkey.pem   /etc/nexvue/tls/privkey.pem
   sudo chown nexvue:nexvue /etc/nexvue/tls/*.pem
   sudo chmod 600 /etc/nexvue/tls/privkey.pem
   sudo chmod 644 /etc/nexvue/tls/fullchain.pem
   ```
3. **`mediamtx.yml`** already has both `webrtcEncryption` and `apiEncryption`
   blocks pointed at `/etc/nexvue/tls/` — confirm they're uncommented/set to
   `yes` and paths match, then:
   ```bash
   sudo cp mediamtx.yml /etc/nexvue/mediamtx.yml   # only if not hand-edited live
   sudo systemctl restart mediamtx
   journalctl -u mediamtx -n 15 --no-pager   # confirm clean start, no cert errors
   ```
4. **Status daemon** — edit the LIVE unit (not just the repo copy) and
   uncomment the two `Environment=NEXVUE_STATUS_TLS_*` lines:
   ```bash
   sudo systemctl edit --full nexvue-status
   # uncomment the two Environment= lines, save
   sudo systemctl daemon-reload && sudo systemctl restart nexvue-status
   journalctl -u nexvue-status -n 5 --no-pager   # want "serving https", not "serving http"
   ```
   `systemctl cat nexvue-status` shows what's actually LIVE — use it to verify
   an edit really landed, since a repo-file edit alone changes nothing until
   copied to `/etc/systemd/system/` and reloaded.
5. **Deploy the current `test-player.html`** to Apache's docroot — it
   auto-detects `https:`/`http:` from `location.protocol`, so it must be the
   current version or it will keep requesting `http://` regardless of what
   you fixed server-side.
6. **Self-signed cert (e.g. Ubuntu's `ssl-cert-snakeoil`, or any cert issued
   for a hostname while you're testing via bare IP): trust it on each port
   individually**, once per browser — visiting `https://<ip>/` does NOT
   extend trust to `https://<ip>:8889/`:
   ```
   https://<edge-ip>:8889/
   https://<edge-ip>:9997/v3/paths/list
   https://<edge-ip>:9998/status
   https://<edge-ip>:9999/           (only in direct-access mode with TLS enabled;
                                       default loopback-only setup doesn't need this)
   ```
   Click through "Advanced -> Proceed" on each. Skipping this step causes
   silent failures that look identical to a misconfiguration.

A real cert (internal CA, or a hostname + Let's Encrypt) removes the
per-browser click-through in step 6 and is worth doing before this goes
beyond bench testing — self-signed is fine for Phase 1 validation only.

## DeckLink Duo 2 connector direction (read this before patching)

**The Duo 2's BNCs are bidirectional and can be individually configured as
Input or Output.** Out of the box some connectors may default to Output
(commonly showing "NTSC" under Desktop Video Setup's OUTPUT FORMAT column for
that row) rather than Input. An output-configured connector cannot capture —
GStreamer fails immediately with `streaming stopped, reason not-negotiated
(-4)` before a single frame, which is a distinctly different failure than "no
signal" (which still negotiates and emits black). If you see `not-negotiated`
at pipeline start, check this first, before assuming a bad cable or a card
fault.

**Check and fix:**
1. Open `BlackmagicDesktopVideoSetup` (GUI utility, ships with Desktop Video).
2. Each `DeckLink Duo (N)` row shows VIDEO IN, OUTPUT FORMAT, GENLOCK columns.
   A row with something in OUTPUT FORMAT (e.g. "NTSC") is currently an OUTPUT.
3. Click that row's config icon, set the connector direction to **Input**.
4. Repeat for every connector you intend to capture from.

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
answers "how much bandwidth did we serve last week," "was this input locked
all night," and "which IP was watching channel 2 at 3am," not "is the
service up right now." Health/alerting is CheckMK's job, planned for
Phase 4 (see roadmap below) — this dashboard is separate and needs no
CheckMK dependency.

`nexvue-metrics-server.py` polls the MediaMTX API and the `nexvue-status`
daemon every 15s (configurable), stores time-series samples in SQLite
(stdlib `sqlite3` — no pip), and serves a small Chart.js + table dashboard:

- **Total bandwidth** — sum of egress across all published paths (Mbps)
- **Viewers connected** — sum of readers across all paths
- **Active streams** — count of paths currently `ready` (publishing)
- **Input lock status** — one line per DeckLink input, stepped 0/1
  (locked/unlocked), with detected format in the tooltip
- **Viewer sessions table** — one row per viewer, sortable: IP address,
  channel, first/last seen, duration, data served, user agent, live/ended
  status. This is the IP + channel drill-down.

### Access: Apache reverse proxy (default — no new firewall port)

`nexvue-metrics-server.py` binds to **127.0.0.1 only by default** — it is
not reachable from outside the box at all, so it needs **no firewall rule**.
Apache (already open on 443, already trusted by IT) proxies to it over
loopback instead:

```bash
sudo a2enmod proxy proxy_http
```

Add to the existing HTTPS vhost (`/etc/apache2/sites-enabled/000-default-ssl.conf`
or wherever your `<VirtualHost *:443>` block lives):

```apache
ProxyPass /metrics/ http://127.0.0.1:9999/
ProxyPassReverse /metrics/ http://127.0.0.1:9999/
```

```bash
sudo systemctl restart apache2
```

Then open `https://<your-apache-host>/metrics/` — same TLS cert, same
already-open port, same firewall posture as everything else on that site.
The dashboard's own fetch calls use relative paths specifically so this
works regardless of the proxy prefix you choose.

This also means the metrics daemon's own TLS support (below) is unnecessary
in this setup — the Apache-to-backend hop never leaves the machine.

### Access: direct port (alternative, needs a firewall rule)

If you'd rather not proxy through Apache — e.g. a trusted internal LAN,
or testing before Apache is set up — bind externally instead:

```bash
sudo systemctl edit --full nexvue-metrics
# add: Environment=NEXVUE_METRICS_BIND=0.0.0.0
sudo systemctl daemon-reload && sudo systemctl restart nexvue-metrics
sudo ufw allow 9999/tcp comment 'NexVUE metrics dashboard'
```

Then `http://<edge-ip>:9999/` (or `https://` with `NEXVUE_METRICS_TLS_CERT`/
`_KEY` set — see the Configuration table below).

### Install

Already covered by `sudo ./setup.sh`. Manually:

```bash
sudo install -m 755 nexvue-metrics-server.py /usr/local/bin/
sudo install -m 644 nexvue-metrics-dashboard.html /usr/local/bin/
sudo install -m 644 nexvue-metrics.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nexvue-metrics
```

Time-range buttons (1h/6h/24h/7d/30d) query `/api/history` and `/api/viewers`;
the page auto-refreshes every 30s. Click a viewer-table column header to sort
by it (click again to reverse).

### Viewer drill-down: how it works

`/v3/paths/list` only gives a reader *count* per path — no IP, no per-session
detail. The actual client address, connection start time, and byte counts
live on a separate MediaMTX endpoint, `/v3/webrtcsessions/list`, which the
collector also polls each cycle. Each WebRTC session there is tagged
`state: "read"` (a viewer) or `state: "publish"` (one of *our own* encoders
publishing into MediaMTX) — only `"read"` sessions are stored, so an
encoder's own connection never shows up in the viewer table.

Sessions are stored as one row per `session_id`, upserted each poll:
`first_seen` is set once and never overwritten; `last_seen` and bytes served
advance every cycle the session is still active. That gives a clean
per-viewer lifecycle record — IP, channel, when they joined, when they were
last seen, how long, how much data — without one row per poll cycle per
viewer bloating the table. A session is shown as "live" if it was seen within
the last 3 poll intervals; otherwise "ended."

### Configuration (systemd `Environment=` lines, all optional)

| Variable | Default | Purpose |
|---|---|---|
| `NEXVUE_MEDIAMTX_API_URL` | `https://127.0.0.1:9997` | Where to poll for bandwidth/viewers/streams/sessions |
| `NEXVUE_STATUS_URL` | `https://127.0.0.1:9998` | Where to poll for input lock/format |
| `NEXVUE_METRICS_POLL_INTERVAL_S` | `15` | Seconds between polls |
| `NEXVUE_METRICS_RETENTION_DAYS` | `30` | Samples/sessions older than this are pruned hourly |
| `NEXVUE_METRICS_DB` | `/var/lib/nexvue/metrics.db` | SQLite file path (auto-created via `StateDirectory=`) |
| `NEXVUE_METRICS_BIND` | `127.0.0.1` | Set to `0.0.0.0` for direct external access (see above) |
| `NEXVUE_METRICS_TLS_CERT`/`_KEY` | unset | TLS on :9999 — only relevant in direct-access mode |

If either MediaMTX or the status daemon is still plain HTTP (TLS not yet
configured — see the TLS section above), set the corresponding `_URL`
variable to `http://` instead of the `https://` default.

### Notes

- The collector calls MediaMTX/status over loopback with certificate
  verification disabled — safe specifically because that traffic never
  leaves 127.0.0.1; see the comment on `_unverified_ssl_context()` in the
  script if extending this pattern elsewhere.
- Per-channel samples are stored (not just totals) for future per-channel
  breakdowns, even though the current dashboard only charts totals — the
  `channel` column in the `samples` table is there if you want to add that
  later without a schema change.
- 30 days of 15-second samples across a handful of channels is a few tens of
  thousands of rows — trivial for SQLite; no performance tuning needed at
  this scale.
- `remoteAddr` includes the port (e.g. `203.0.113.7:54321`); the dashboard
  table strips it for readability, but it's stored as-is in `viewer_sessions`
  if you need it for something else (e.g. correlating with firewall logs).

## Operational notes

- **Channel env files tolerate inline `#` comments and whitespace.** The
  encoder unit sources `/etc/nexvue/channels/<N>.env` through a shell
  (`nexvue-encode@.service`'s `ExecStart`), not systemd's native
  `EnvironmentFile=` parser — the latter does NOT strip inline comments, so a
  line like `MAX_DEVICES=4   # note` would otherwise pass the comment through
  as part of the value and break arithmetic checks. `nexvue-encode.sh` also
  defensively strips inline comments itself (`strip_inline()`), so this is
  safe even if the script is ever invoked outside the unit.
- **`test-player.html` auto-discovers the edge host** from `location.hostname`
  — load it via Apache at any address and WHEP/API/status all target that
  same host on their fixed ports (8889/9997/9998). The host field is an
  optional override, not a requirement. Protocol (`http:`/`https:`) is also
  auto-detected from the page's own scheme — see the TLS section above if
  that's not lining up.
- **Mirror/flip persist through fullscreen.** Applied as an inline
  `transform` on the video (not a CSS class), and the dedicated "⛶
  Fullscreen" button fullscreens the wrapper `<div>`, not the `<video>`
  element itself — fullscreening the video directly (e.g. via its native
  player-bar control) lets the browser override the transform. Use the ⛶
  button, not the native control, if mirror/flip need to survive fullscreen.
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
