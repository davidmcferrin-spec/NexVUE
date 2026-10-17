# Operations

Facts for a box that is already installed. Install, configure, and upgrade
are in the README.

## Ports

| Port | Proto | Who | Purpose |
|------|-------|-----|---------|
| 22 | TCP | operators | SSH |
| 80 | TCP | viewers | Redirect to the same URL on HTTPS. The UI is not served here. |
| 443 | TCP | viewers | Apache UI: `/login`, `/player`, `/multiview`, `/metrics`, `/settings`, `/services`, `/users` |
| 8889 | TCP | viewers | WHEP signaling (TLS) |
| 8189 | UDP and TCP | viewers | WebRTC media. UDP carries the media; TCP is the ICE fallback. |
| 9997 | TCP | loopback | MediaMTX control API. The player uses `/api/mediamtx`. |
| 9998 | TCP | loopback | DeckLink status daemon. The player uses `/api/status`. |
| 8554 | TCP | loopback | RTSP publish from the encoders. |

`ufw allow 8189` with no protocol opens UDP and TCP. Signaling on 8889
without 8189 negotiates a session that never shows a picture. Off-LAN
viewers also need Settings → Public reachability so ICE advertises the
public name or NAT address. Cloudflare TURN is the relay for networks that
block 8189 entirely; each viewer is still one copy off the station NIC.
Cloudflare Stream is the fan-out path: the edge WHIPs each live path once.

## TLS

`setup.sh` creates `/etc/nexvue/tls/fullchain.pem` and `privkey.pem` when
they are missing (self-signed, group `ssl-cert`, key mode 640) and points
Apache and MediaMTX at them. Files that are already there are kept.

Settings → Certificates (admin) either runs pinned lego with TLS-ALPN-01 on
port 443, or installs an uploaded PEM pair. Apache is stopped only for the
ACME challenge and started again afterward. Issue and renew use the Public
hostname. `nexvue-tls-renew.timer` renews a lego certificate inside 30 days
of expiry and does nothing for a self-signed or uploaded pair.

The JWKS vhost on `127.0.0.1:9080` stays plain HTTP so MediaMTX can fetch
keys without following the HTTPS redirect.

## Signal loss

Publish (appsrc → HI/LO encode → RTSP) stays up across an SDI fault.
Capture (DeckLink → appsink) is torn down and opened again. Publish repeats
the last frame for `SIGNAL_LOSS_HOLD_S` (default 15 seconds), then black, so
the WHEP session stays up. Audio sends each captured PCM chunk once, then
silence while capture is down.

An enabled input that has never locked auto-parks after
`AUTO_PARK_UNLOCK_CYCLES` consecutive unlocked starts (default 5).
`nexvue-encode-auto-unpark.timer` starts that slot again when DeckLink
reports lock. Disable or Stop on a feed that is still locked leaves it
down. `AUTO_UNPARK=false` or `AUTO_UNPARK_LOCK_POLLS=0` turns auto-unpark
off.

Leave empty ports disabled when you can. Services Enable and Disable both
run `reset-failed`, so a parked encoder does not stay red.

## Captions

CEA-608/CC1 is a side channel. It is not burned in, and MediaMTX still
carries only H.264 and Opus. Encode writes
`/run/nexvue/captions/<path>.json`. `nexvue-captions.php` serves Server-Sent
Events on the same origin. The FIFO sink is unbuffered: 608 arrives at
about 60–120 bytes per second, and the default 64KB buffer would stall the
overlay. The decoder shows at most two lines, newest at the bottom. Idle
erase is about 16 seconds (`NEXVUE_CAPTIONS_IDLE_ERASE_S`). PHP treats a
non-empty file older than `NEXVUE_CAPTIONS_STALE_S` (default 60 seconds) as
cleared. A dead caption reader does not restart the encoder.

Probe with that encoder stopped:

```bash
sudo systemctl stop nexvue-encode@0
sudo -u nexvue nexvue-captions-probe.sh 0
sudo systemctl start nexvue-encode@0
```

## Audio

Encode opens 8-channel DeckLink audio and publishes 8-channel positioned
Opus (default 384 kbit/s) on HI and LO. `AUDIO_LAYOUT` and `AUDIO_EMBEDS`
only choose which embeds the browser VU offers. Player and Multiview add
`multiopus` to the WHEP offer for channels 3–8. Use Chrome or Edge for that.

## Metrics

`nexvue-metrics` writes `/var/lib/nexvue/metrics.db` and does not listen.
The dashboard is `/metrics` on Apache. Samples are UTC. Labels use
`America/New_York` unless `NEXVUE_METRICS_TZ` is set. iGPU busy percent is
read from a long-running `intel_gpu_top -J` process. A one-shot pipe never
flushes, so the chart stays empty.

## Portal

`sudo ./setup.sh --portal` on a machine without DeckLink or MediaMTX.
Production sign-in is the NexAPP cookie. Catalog User and Admin map to
portal `org_viewer` and `org_admin`. A catalog admin does not become an
edge admin.

Enrollment and the heartbeat are started by the edge. The portal does not
dial the node. After adoption the edge publishes a merged JWKS: the local
key, plus the portal key cached from the last heartbeat. A portal outage
leaves local login, share links, and the encoder publish JWT working.
Health is the age of the last heartbeat (stale after 700 seconds).
`/watch` opens the edge WHEP directly, or Stream when that station is
Hybrid or All viewers. Video does not transit the portal.

## DeckLink connectors

Duo and Quad BNCs are half-duplex inputs. `setup.sh` runs
`decklink-configure --apply-inputs` when the helper is installed. Confirm
lock and mode with `decklink-status`. Sub-device order is not guaranteed
to match the BNC labels.

## Closeout helpers

`setup.sh` installs `nexvue-phase1-deploy-verify.sh` and
`nexvue-phase1-closeout.sh`. After a deploy:

```bash
sudo nexvue-phase1-deploy-verify.sh
sudo nexvue-phase1-closeout.sh
```

The closeout default window is 72 hours. A high start count over that
window with a quiet last hour is a warning from earlier bring-up.
`nexvue-encode-storm-diagnose.sh` classifies a live restart storm.
