#!/usr/bin/env bash
###############################################################################
# nexvue-encode.sh — one DeckLink input -> H.264/Opus -> MediaMTX (RTSP)
#
# Invoked by systemd template unit nexvue-encode@<n>.service with environment
# loaded from /etc/nexvue/channels/<n>.env
#
# v3: adds optional LO rendition ("poor man's ABR").
#   LO_ENABLE=true adds a second, lower-bitrate encode of the SAME capture via
#   a tee — published as <CHANNEL_PATH>lo (e.g. ch0lo). One capture, two
#   encodes: DeckLink sub-devices are exclusive-open, so this CANNOT be a
#   second service instance; it must live in the same pipeline.
#
# Resilience design (v2 carried forward):
#   - constant output caps (normalization): input format changes never
#     renegotiate the encoder or drop viewer sessions
#   - watchdog: silent capture hangs become clean systemd restarts
#   - black frames on signal loss keep sessions alive
###############################################################################
# Must run under bash (uses pipefail, [[ ]], arrays). Re-exec under bash if
# launched via sh/dash so failures are clear, not "Illegal option -o pipefail".
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

log() { echo "[nexvue-encode] $*"; }

# ---- Required environment ----------------------------------------------------
: "${DEVICE_NUMBER:?DEVICE_NUMBER is required (DeckLink connector index, 0-based)}"
: "${CHANNEL_PATH:?CHANNEL_PATH is required (MediaMTX path, e.g. ch0)}"

# systemd's EnvironmentFile= (unlike a shell) does NOT strip inline "# comments",
# so a channel env line like "MAX_DEVICES=4   # count" would otherwise pass the
# comment through as part of the value. Defensively strip a trailing
# whitespace+comment and surrounding whitespace from the values we consume, so
# a hand-edited env file with a stray inline comment degrades gracefully
# instead of erroring cryptically in an arithmetic test.
strip_inline() {  # echo the value with any trailing " # comment" and edge spaces removed
  local v="$1"
  v="${v%%#*}"                        # drop from first # to end
  v="${v#"${v%%[![:space:]]*}"}"      # ltrim
  v="${v%"${v##*[![:space:]]}"}"      # rtrim
  printf '%s' "$v"
}
DEVICE_NUMBER="$(strip_inline "${DEVICE_NUMBER}")"
CHANNEL_PATH="$(strip_inline "${CHANNEL_PATH}")"

# ---- Optional environment (defaults tuned for 1080i59.94 sources) ------------
# Channel count of the installed card, for input validation:
#   Duo 2 / Duo 2 Mini = 4, Quad 2 = 8, original Duo = 2.
# Default 8 keeps existing Quad 2 configs unchanged.
MAX_DEVICES="$(strip_inline "${MAX_DEVICES:-8}")"
DEINT_FIELDS="$(strip_inline "${DEINT_FIELDS:-all}")"
BITRATE_KBPS="$(strip_inline "${BITRATE_KBPS:-5000}")"
GOP_FRAMES="$(strip_inline "${GOP_FRAMES:-60}")"
ENABLE_AUDIO="$(strip_inline "${ENABLE_AUDIO:-true}")"
AUDIO_BITRATE_BPS="$(strip_inline "${AUDIO_BITRATE_BPS:-128000}")"
AUDIO_CHANNELS="$(strip_inline "${AUDIO_CHANNELS:-2}")"
AUDIO_FRAME_MS="$(strip_inline "${AUDIO_FRAME_MS:-10}")"
# Audio queue depth in buffers. 16 (the old default) is only ~160-320ms of
# headroom at typical Opus frame sizes — thin against any transient stall
# (encoder thermal cycling, brief CPU contention from other channels or the
# metrics/status pollers). Unlike decklinkaudiosrc's own overflow protection
# (which logs "Dropped N old packets"), a generic GStreamer `queue` element
# silently drops on overflow with NO log message by default — so a queue
# this shallow could be leaking under load for hours with zero trace in the
# journal, and `audiorate` downstream papering over each gap with inserted
# silence is a plausible source of a "watery"/phasy long-run audio artifact.
# Bumped to 100 (~1-2s headroom) by default; still leaky, so a genuinely
# sustained stall still drops rather than growing unbounded latency.
AUDIO_QUEUE_BUFFERS="$(strip_inline "${AUDIO_QUEUE_BUFFERS:-100}")"
# audioresample quality, 0-10. GStreamer's own default is a middling 4.
# Under CONTINUOUS small corrections (e.g. the capture clock drifting
# against the pipeline clock over many hours — watch for recurring
# "Dropped N old packets" warnings well after startup as the tell), a low
# resample quality is what turns an inaudible correction into an audible
# "watery"/phasy artifact. Bumped to 9 by default: costs negligible CPU for
# one audio stream, standard fix for this symptom.
AUDIO_RESAMPLE_QUALITY="$(strip_inline "${AUDIO_RESAMPLE_QUALITY:-9}")"
DECKLINK_BUFFER_FRAMES="$(strip_inline "${DECKLINK_BUFFER_FRAMES:-2}")"
WATCHDOG_MS="${WATCHDOG_MS:-3000}"
OUTPUT_WIDTH="${OUTPUT_WIDTH:-1920}"        # normalized HI raster — constant
OUTPUT_HEIGHT="${OUTPUT_HEIGHT:-1080}"      # regardless of input format
RTSP_URL="${RTSP_URL:-rtsp://127.0.0.1:8554/${CHANNEL_PATH}}"
VIDEO_ENCODER="${VIDEO_ENCODER:-vah264enc}" # vah264enc (QSV/VA-API) | x264enc
EXTRA_ENC_ARGS="${EXTRA_ENC_ARGS:-}"

# LO rendition (adaptive-bandwidth fallback the portal player can switch to)
LO_ENABLE="${LO_ENABLE:-false}"
LO_PRESET="${LO_PRESET:-720p}"              # 720p|540p|480p|360p|240p|180p
LO_FPS="${LO_FPS:-30000/1001}"              # 29.97p default: cellular-friendly
LO_RTSP_URL="${LO_RTSP_URL:-rtsp://127.0.0.1:8554/${CHANNEL_PATH}lo}"

# Caption side channel (CEA-608/CC1 → /run/nexvue/captions/<path>.json).
# Not burned into video; not a second encode. Extract in-pipeline because
# DeckLink sub-devices are exclusive-open.
CAPTIONS_ENABLE="$(strip_inline "${CAPTIONS_ENABLE:-true}")"
CAPTIONS_DIR="$(strip_inline "${CAPTIONS_DIR:-/run/nexvue/captions}")"
CAPTIONS_DECODE_BIN="$(strip_inline "${CAPTIONS_DECODE_BIN:-/usr/local/bin/nexvue-captions-decode.py}")"
# Assembly-test only: inject caption elements without FIFO/decoder (no mkfifo).
CAPTIONS_PIPELINE_ONLY="$(strip_inline "${CAPTIONS_PIPELINE_ONLY:-false}")"

# Preset -> 16:9 raster + default bitrate. Note: the "p" number is the HEIGHT
# (480p = 854x480). All dimensions even, as H.264 requires. Explicit
# LO_WIDTH/LO_HEIGHT/LO_BITRATE_KBPS below override the preset.
case "${LO_PRESET}" in
  720p) LO_W_DEF=1280; LO_H_DEF=720; LO_BR_DEF=1200 ;;
  540p) LO_W_DEF=960;  LO_H_DEF=540; LO_BR_DEF=800  ;;
  480p) LO_W_DEF=854;  LO_H_DEF=480; LO_BR_DEF=700  ;;
  360p) LO_W_DEF=640;  LO_H_DEF=360; LO_BR_DEF=500  ;;
  240p) LO_W_DEF=426;  LO_H_DEF=240; LO_BR_DEF=300  ;;
  180p) LO_W_DEF=320;  LO_H_DEF=180; LO_BR_DEF=200  ;;
  *) log "ERROR: LO_PRESET must be one of 720p,540p,480p,360p,240p,180p — got '${LO_PRESET}'"; exit 64 ;;
esac
LO_WIDTH="${LO_WIDTH:-${LO_W_DEF}}"
LO_HEIGHT="${LO_HEIGHT:-${LO_H_DEF}}"
LO_BITRATE_KBPS="${LO_BITRATE_KBPS:-${LO_BR_DEF}}"

# ---- Sanity checks ------------------------------------------------------------
if ! [[ "${DEVICE_NUMBER}" =~ ^[0-9]+$ ]] || [ "${DEVICE_NUMBER}" -ge "${MAX_DEVICES}" ]; then
    log "ERROR: DEVICE_NUMBER must be 0-$((MAX_DEVICES-1)) for this card (MAX_DEVICES=${MAX_DEVICES}), got '${DEVICE_NUMBER}'"; exit 64
fi
case "${DEINT_FIELDS}" in all|top) ;; *)
    log "ERROR: DEINT_FIELDS must be 'all' or 'top', got '${DEINT_FIELDS}'"; exit 64 ;;
esac
case "${ENABLE_AUDIO}" in true|false) ;; *)
    log "ERROR: ENABLE_AUDIO must be 'true' or 'false'"; exit 64 ;;
esac
case "${LO_ENABLE}" in true|false) ;; *)
    log "ERROR: LO_ENABLE must be 'true' or 'false'"; exit 64 ;;
esac
case "${CAPTIONS_ENABLE}" in true|false) ;; *)
    log "ERROR: CAPTIONS_ENABLE must be 'true' or 'false'"; exit 64 ;;
esac
case "${AUDIO_FRAME_MS}" in 2|5|10|20|40|60) ;; *)
    log "ERROR: AUDIO_FRAME_MS must be one of 2,5,10,20,40,60"; exit 64 ;;
esac
if ! [[ "${AUDIO_RESAMPLE_QUALITY}" =~ ^([0-9]|10)$ ]]; then
    log "ERROR: AUDIO_RESAMPLE_QUALITY must be an integer 0-10, got '${AUDIO_RESAMPLE_QUALITY}'"; exit 64
fi
if ! [[ "${AUDIO_QUEUE_BUFFERS}" =~ ^[0-9]+$ ]] || [ "${AUDIO_QUEUE_BUFFERS}" -lt 1 ]; then
    log "ERROR: AUDIO_QUEUE_BUFFERS must be a positive integer, got '${AUDIO_QUEUE_BUFFERS}'"; exit 64
fi
command -v gst-launch-1.0 >/dev/null || { log "ERROR: gst-launch-1.0 not found"; exit 69; }
gst-inspect-1.0 decklinkvideosrc >/dev/null 2>&1 \
    || { log "ERROR: GStreamer decklink plugin missing (install Desktop Video + gst-plugins-bad)"; exit 69; }

# ---- Encoder selection ---------------------------------------------------------
build_enc() { # $1 = bitrate kbps
  case "${VIDEO_ENCODER}" in
    vah264enc)
      echo "vah264enc rate-control=cbr bitrate=$1 key-int-max=${GOP_FRAMES} b-frames=0 target-usage=7 ${EXTRA_ENC_ARGS}"
      ;;
    x264enc)
      echo "x264enc tune=zerolatency speed-preset=veryfast bitrate=$1 key-int-max=${GOP_FRAMES} bframes=0 ${EXTRA_ENC_ARGS}"
      ;;
  esac
}
case "${VIDEO_ENCODER}" in
  vah264enc)
    gst-inspect-1.0 vah264enc >/dev/null 2>&1 \
        || { log "ERROR: vah264enc unavailable — check intel-media-va-driver-non-free and /dev/dri perms, or set VIDEO_ENCODER=x264enc"; exit 69; }
    ;;
  x264enc) ;;
  *) log "ERROR: unsupported VIDEO_ENCODER '${VIDEO_ENCODER}'"; exit 64 ;;
esac
ENC_HI="$(build_enc "${BITRATE_KBPS}")"
ENC_LO="$(build_enc "${LO_BITRATE_KBPS}")"

# ---- Fixed output framerate (drives the normalization capsfilter) -------------
case "${DEINT_FIELDS}" in
  all) OUTPUT_FPS="60000/1001" ;;
  top) OUTPUT_FPS="30000/1001" ;;
esac

# ---- Captions side channel (optional) ------------------------------------------
CAPTIONS_ACTIVE=false
CAPTIONS_PID=""
CAPTIONS_FIFO=""
cleanup_captions() {
  if [ -n "${CAPTIONS_PID}" ] && kill -0 "${CAPTIONS_PID}" 2>/dev/null; then
    kill "${CAPTIONS_PID}" 2>/dev/null || true
    wait "${CAPTIONS_PID}" 2>/dev/null || true
  fi
  # Never rm /dev/null (CAPTIONS_PIPELINE_ONLY assembly tests).
  if [ -n "${CAPTIONS_FIFO}" ] && [ "${CAPTIONS_FIFO}" != "/dev/null" ]; then
    rm -f "${CAPTIONS_FIFO}"
  fi
}
if [ "${CAPTIONS_ENABLE}" = "true" ]; then
  if ! gst-inspect-1.0 ccextractor >/dev/null 2>&1 \
     || ! gst-inspect-1.0 ccconverter >/dev/null 2>&1; then
    log "WARN: CAPTIONS_ENABLE=true but ccextractor/ccconverter unavailable — captions off"
  elif [ "${CAPTIONS_PIPELINE_ONLY}" = "true" ]; then
    CAPTIONS_FIFO="/dev/null"
    CAPTIONS_ACTIVE=true
  elif [ -f "${CAPTIONS_DECODE_BIN}" ]; then
    mkdir -p "${CAPTIONS_DIR}"
    CAPTIONS_FIFO="${CAPTIONS_DIR}/${CHANNEL_PATH}.ccraw"
    rm -f "${CAPTIONS_FIFO}"
    if mkfifo "${CAPTIONS_FIFO}" 2>/dev/null; then
      chmod 644 "${CAPTIONS_FIFO}" 2>/dev/null || true
      python3 "${CAPTIONS_DECODE_BIN}" \
        --channel "${CHANNEL_PATH}" \
        --fifo "${CAPTIONS_FIFO}" \
        --state-dir "${CAPTIONS_DIR}" &
      CAPTIONS_PID=$!
      trap cleanup_captions EXIT INT TERM
      CAPTIONS_ACTIVE=true
    else
      log "WARN: could not create captions FIFO ${CAPTIONS_FIFO} — captions off"
    fi
  else
    log "WARN: CAPTIONS_ENABLE=true but decode helper missing (${CAPTIONS_DECODE_BIN}) — captions off"
  fi
fi

# ---- Assemble the pipeline -----------------------------------------------------
# Video: capture -> [ccextractor] -> watchdog -> deinterlace -> normalize
#        -> tee -> HI encode -> sink   [-> LO scale/rate -> LO encode -> sinklo]
# Captions (optional): ccextractor.caption -> ccconverter -> FIFO -> decode.py
# Audio: capture -> Opus once -> tee -> both sinks (same encoded track).
PIPELINE="rtspclientsink name=sink location=${RTSP_URL} protocols=tcp"

if [ "${LO_ENABLE}" = "true" ]; then
  PIPELINE+=" rtspclientsink name=sinklo location=${LO_RTSP_URL} protocols=tcp"
fi

PIPELINE+=" decklinkvideosrc device-number=${DEVICE_NUMBER} mode=auto"
PIPELINE+=" buffer-size=${DECKLINK_BUFFER_FRAMES} drop-no-signal-frames=false"
if [ "${CAPTIONS_ACTIVE}" = "true" ]; then
  PIPELINE+=" output-cc=true"
fi
PIPELINE+=" ! queue max-size-buffers=4 leaky=downstream"
if [ "${CAPTIONS_ACTIVE}" = "true" ]; then
  # Extract VANC captions before deinterlace/scale; video continues on cc.src.
  PIPELINE+=" ! ccextractor name=cc"
  PIPELINE+=" cc. ! queue max-size-buffers=4 leaky=downstream"
fi
PIPELINE+=" ! watchdog timeout=${WATCHDOG_MS}"
PIPELINE+=" ! deinterlace fields=${DEINT_FIELDS} method=greedyh"
PIPELINE+=" ! videorate ! videoscale ! videoconvert"
PIPELINE+=" ! video/x-raw,format=NV12,width=${OUTPUT_WIDTH},height=${OUTPUT_HEIGHT},framerate=${OUTPUT_FPS},pixel-aspect-ratio=1/1"

if [ "${LO_ENABLE}" = "true" ]; then
  PIPELINE+=" ! tee name=vt"
  PIPELINE+=" vt. ! queue max-size-buffers=4 leaky=downstream"
  PIPELINE+=" ! ${ENC_HI} ! h264parse config-interval=-1 ! sink."
  # LO branch: drop to LO_FPS first (cheap), then scale down, then encode.
  PIPELINE+=" vt. ! queue max-size-buffers=4 leaky=downstream"
  PIPELINE+=" ! videorate ! videoscale"
  PIPELINE+=" ! video/x-raw,format=NV12,width=${LO_WIDTH},height=${LO_HEIGHT},framerate=${LO_FPS},pixel-aspect-ratio=1/1"
  PIPELINE+=" ! ${ENC_LO} ! h264parse config-interval=-1 ! sinklo."
else
  PIPELINE+=" ! ${ENC_HI} ! h264parse config-interval=-1 ! sink."
fi

if [ "${ENABLE_AUDIO}" = "true" ]; then
  PIPELINE+=" decklinkaudiosrc device-number=${DEVICE_NUMBER} channels=${AUDIO_CHANNELS}"
  PIPELINE+=" ! queue max-size-buffers=${AUDIO_QUEUE_BUFFERS} leaky=downstream"
  # audiorate enforces a gapless, constant-rate timeline: it inserts silence
  # for any gap (e.g. from the queue above leaking under momentary pressure)
  # instead of letting a timestamp discontinuity pass through. Without this,
  # a dropped chunk shows up downstream as a burst of "catch-up" playback —
  # the browser's jitter buffer has no pacing information to know the gap was
  # supposed to take real time, so it just drains the backlog as fast as it
  # arrives. This is the standard GStreamer fix for that symptom.
  PIPELINE+=" ! audiorate"
  PIPELINE+=" ! audioconvert ! audioresample quality=${AUDIO_RESAMPLE_QUALITY} ! audio/x-raw,rate=48000,channels=2"
  PIPELINE+=" ! opusenc bitrate=${AUDIO_BITRATE_BPS} frame-size=${AUDIO_FRAME_MS}"
  if [ "${LO_ENABLE}" = "true" ]; then
    PIPELINE+=" ! tee name=at"
    PIPELINE+=" at. ! queue max-size-buffers=${AUDIO_QUEUE_BUFFERS} leaky=downstream ! sink."
    PIPELINE+=" at. ! queue max-size-buffers=${AUDIO_QUEUE_BUFFERS} leaky=downstream ! sinklo."
  else
    PIPELINE+=" ! sink."
  fi
fi

if [ "${CAPTIONS_ACTIVE}" = "true" ]; then
  # cc.caption is a separate pad; convert CDP/608 metas to raw CEA-608 pairs
  # for nexvue-captions-decode.py. filesink to a FIFO (decoder already reading).
  # buffer-mode=unbuffered is MANDATORY: filesink's default mode accumulates
  # ~64KB before flushing, and raw 608 trickles in at ~60-120 B/s — buffered,
  # the decoder would see nothing for 10+ minutes (same block-buffering trap
  # as the intel_gpu_top one-shot; see CLAUDE.md).
  PIPELINE+=" cc.caption ! queue max-size-buffers=8 leaky=downstream"
  PIPELINE+=" ! ccconverter"
  PIPELINE+=" ! closedcaption/x-cea-608,format=raw"
  PIPELINE+=" ! filesink location=${CAPTIONS_FIFO} buffer-mode=unbuffered sync=false append=false"
fi

log "starting: device=${DEVICE_NUMBER} path=${CHANNEL_PATH} deint=${DEINT_FIELDS} hi=${BITRATE_KBPS}kbps lo=${LO_ENABLE}(${LO_BITRATE_KBPS}kbps) audio=${ENABLE_AUDIO} captions=${CAPTIONS_ACTIVE} enc=${VIDEO_ENCODER}"
log "publishing HI to ${RTSP_URL}$([ "${LO_ENABLE}" = "true" ] && echo ", LO to ${LO_RTSP_URL}")"

# Intentional word-splitting: PIPELINE is a gst-launch description whose
# tokens never contain spaces (caps use commas), so this is safe.
# Do not exec — a background captions decoder needs EXIT cleanup.
# shellcheck disable=SC2086
gst-launch-1.0 -e ${PIPELINE}
rc=$?
cleanup_captions
exit "${rc}"