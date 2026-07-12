#!/usr/bin/env bash
# Unit tests for nexvue-encode.sh pipeline assembly (GStreamer stubbed via PATH).
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/test/stubbin:$PATH"
fail() { echo "FAIL: $1"; exit 1; }
run_encode() { bash ./nexvue-encode.sh "$@"; }

expect_usage_64() {
	local rc
	set +e
	bash ./nexvue-encode.sh >/dev/null 2>&1
	rc=$?
	set -e
	[ "$rc" -eq 64 ] || fail "$1 (expected exit 64, got $rc)"
}

# T1: default single-rendition pipeline
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 run_encode)
grep -q "rtsp://127.0.0.1:8554/ch0" <<<"$out" || fail "T1 default RTSP url"
grep -q "watchdog" <<<"$out" || fail "T1 watchdog present"
grep -q "width=1920,height=1080,framerate=60000/1001" <<<"$out" || fail "T1 normalization caps"
grep -q "opusenc" <<<"$out" || fail "T1 audio present by default"
grep -q "tee" <<<"$out" && fail "T1 no tee when LO disabled"

# T2: LO rendition adds tee, second sink, lo caps, audio tee
out=$(DEVICE_NUMBER=3 CHANNEL_PATH=ch3 LO_ENABLE=true run_encode)
grep -q "name=sinklo location=rtsp://127.0.0.1:8554/ch3lo" <<<"$out" || fail "T2 lo sink url"
grep -q "tee name=vt" <<<"$out" || fail "T2 video tee"
grep -q "width=1280,height=720,framerate=30000/1001" <<<"$out" || fail "T2 lo caps"
grep -q "tee name=at" <<<"$out" || fail "T2 audio tee"
grep -c "vah264enc" <<<"$out" | grep -q "^2$" || fail "T2 two encoders"

# T3: silent channel drops audio entirely
out=$(DEVICE_NUMBER=1 CHANNEL_PATH=ch1 ENABLE_AUDIO=false run_encode)
grep -q "opusenc" <<<"$out" && fail "T3 audio should be absent"
grep -q "decklinkaudiosrc" <<<"$out" && fail "T3 audiosrc should be absent"

# T4: top-field mode sets 29.97p normalization
out=$(DEVICE_NUMBER=2 CHANNEL_PATH=ch2 DEINT_FIELDS=top run_encode)
grep -q "framerate=30000/1001" <<<"$out" || fail "T4 29.97p caps"

# T5: invalid inputs rejected with usage exit code
DEVICE_NUMBER=9 CHANNEL_PATH=ch9 expect_usage_64 "T5 accepted device 9"
DEVICE_NUMBER=0 CHANNEL_PATH=ch0 DEINT_FIELDS=bogus expect_usage_64 "T5 accepted bogus deint"

# T6: x264 fallback path
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 VIDEO_ENCODER=x264enc run_encode)
grep -q "x264enc tune=zerolatency" <<<"$out" || fail "T6 x264 fallback"


# T7: LO_PRESET ladder maps to correct raster and default bitrate
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 LO_ENABLE=true LO_PRESET=360p run_encode)
grep -q "width=640,height=360" <<<"$out" || fail "T7 360p raster"
grep -q "bitrate=500" <<<"$out" || fail "T7 360p default bitrate"
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 LO_ENABLE=true LO_PRESET=240p run_encode)
grep -q "width=426,height=240" <<<"$out" || fail "T7 240p raster"

# T8: explicit LO_WIDTH/HEIGHT/BITRATE override the preset
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 LO_ENABLE=true LO_PRESET=480p LO_WIDTH=512 LO_HEIGHT=288 LO_BITRATE_KBPS=400 run_encode)
grep -q "width=512,height=288" <<<"$out" || fail "T8 override raster"
grep -q "bitrate=400" <<<"$out" || fail "T8 override bitrate"

# T9: invalid preset rejected
DEVICE_NUMBER=0 CHANNEL_PATH=ch0 LO_ENABLE=true LO_PRESET=1080p expect_usage_64 "T9 accepted bogus preset"

echo "All pipeline assembly tests passed."
