# NexVUE

NexVUE is a self-hosted SDI-to-WebRTC gateway for station return feeds and
remote monitoring. Each edge node captures 3G-SDI on a DeckLink card (Quad 2
is 8 channels, Duo 2 is 4, Duo is 2), encodes with Intel Quick Sync (H.264
and 8-channel Opus), and serves WHEP from MediaMTX. Browsers connect to the
edge. A separate catalog portal never carries the video.

```
SDI --> DeckLink --> GStreamer (deinterlace, vah264enc, Opus)
                 --> RTSP (loopback) --> MediaMTX --> WHEP --> browser
```

LAN glass-to-glass target is about 200 ms. Output raster and frame rate stay
fixed per channel, so an input format change does not renegotiate viewers.

## Install

Edge host: Ubuntu 24.04 LTS. Arrow Lake (Core Ultra 200S) needs the HWE
kernel (`linux-generic-hwe-24.04`) before the media stack. Install Blackmagic
Desktop Video, and put the matching DeckLink SDK at `/opt/decklink-sdk`.
`setup.sh` downloads MediaMTX when the `mediamtx` binary is not already
installed.

From the repo root:

```bash
sudo ./setup.sh
sudo ./setup.sh --check
```

That installs the GStreamer, Apache, and PHP packages, systemd units, ops
sudoers, and the web UI. It writes a self-signed pair under
`/etc/nexvue/tls/` when those files are absent, enables `mediamtx`,
`nexvue-status`, `nexvue-metrics`, `nexvue-sfu-publish` (idle until
Cloudflare Stream is on), `nexvue-decklink-configure`, and
`nexvue-encode@0` through `@(MAX_CHANNELS-1)`. It sets the host timezone to
`America/New_York`, turns NTP on, and allows OpenSSH, 80, 443, 8889, and
8189 in ufw. Port 80 only redirects to HTTPS.

Confirm Quick Sync and DeckLink before the first encode:

```bash
vainfo | grep -i h264
gst-inspect-1.0 decklinkvideosrc
```

The catalog portal is a different machine (Apache and PHP only):

```bash
sudo ./setup.sh --portal
```

On the NexAPP hub the portal is the Alias `/nexvue`. The adopt URL on an
edge must include that path (`https://nexapp.nexstar.tv/nexvue`).

## Configure

Station env is `/etc/nexvue/nexvue.env`. Channel env is
`/etc/nexvue/channels/N.env`. The encode unit sources the channel file with
bash, so a value that contains spaces has to be double-quoted
(`CHANNEL_ALIAS="TVU 35"`). Settings writes that quoting.

Slot count is `MAX_DEVICES` and `MAX_CHANNELS` (one number). Settings →
Card / encode slots is the admin control: Quad 2 = 8, Duo 2 = 4, Duo = 2.
An empty SDI port that stays enabled auto-parks after repeated unlocks and
auto-unparks when lock returns.

Sign in at `https://<edge>/login`. The bootstrap user is `admin` /
`password` and must set a new password. The auth store is
`/var/lib/nexvue/auth/` (www-data). Roles are admin, operator, sharer
(Viewer+Share), and viewer. Share links are `/player?t=` and
`/multiview?t=` (also `/s/<token>`).

Admin settings that change who can watch:

- **Public reachability** writes the DNS name and NAT IPv4 into
  `webrtcAdditionalHosts`, then restarts MediaMTX. Blank means LAN-only.
  That hostname is also the Let's Encrypt name.
- **Certificates** runs pinned lego (TLS-ALPN-01 on port 443) or installs
  an uploaded PEM pair into `/etc/nexvue/tls/`.
- **Cloudflare TURN** is an optional ICE relay for viewers who cannot use
  UDP 8189. The key lives in `auth.db`.
- **Cloudflare Stream** is optional fan-out. Hybrid keeps station logins on
  local WHEP and sends share links and portal `/watch` to Stream.

WHEP is always `https://<host>:8889`. A self-signed certificate needs a
one-time browser exception on that port. Trusting port 443 does not cover
8889.

## Upgrade

An admin uses Services → **Update from repo**. That fetches
`origin/$NEXVUE_UPDATE_BRANCH` (default `main`), hard-resets the clone named
in `/etc/nexvue/repo.path`, and runs `setup.sh`. The top nav shows the
`VERSION` file. The confirm dialog lists commit subjects between the running
clone and origin. Update does not restart encoders. A failed run leaves
`/var/lib/nexvue/update-setup.log` (Services → Setup log).

The same install step from a shell, after the clone is on the commit you
want:

```bash
sudo ./setup.sh
sudo ./setup.sh --check
```

Ports, TLS, signal loss, captions, metrics, and the portal are in
[docs/operations.md](docs/operations.md).
