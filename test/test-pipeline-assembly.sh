#!/usr/bin/env bash
# Unit tests for nexvue-encode.sh pipeline assembly (GStreamer stubbed via PATH).
#
# Must run under bash (uses pipefail, [[ ]], <<< herestrings). If launched with
# sh/dash (e.g. `sh test-pipeline-assembly.sh`), re-exec under bash so the
# error is "bash not found" at worst, never a cryptic "Illegal option -o
# pipefail".
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
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
out_nw=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 WATCHDOG_MS=0 run_encode)
grep -q "watchdog" <<<"$out_nw" && fail "T1 WATCHDOG_MS=0 must omit watchdog"
grep -q "width=1920,height=1080,framerate=60000/1001" <<<"$out" || fail "T1 normalization caps"
grep -q "opusenc" <<<"$out" || fail "T1 audio present by default"
grep -q "audiorate" <<<"$out" || fail "T1 audiorate present (gapless timestamp fix)"
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


# T10: MAX_DEVICES bounds validation (Duo 2 = 4 channels)
DEVICE_NUMBER=4 CHANNEL_PATH=ch4 MAX_DEVICES=4 expect_usage_64 "T10 accepted device 4 on a 4-ch card"
out=$(DEVICE_NUMBER=3 CHANNEL_PATH=ch3 MAX_DEVICES=4 run_encode)
grep -q "device-number=3" <<<"$out" || fail "T10 rejected valid device 3 on Duo 2"

# T11: default MAX_DEVICES=8 still allows Quad 2 range
out=$(DEVICE_NUMBER=7 CHANNEL_PATH=ch7 run_encode)
grep -q "device-number=7" <<<"$out" || fail "T11 default should allow device 7"
DEVICE_NUMBER=8 CHANNEL_PATH=ch8 expect_usage_64 "T11 accepted device 8 at default MAX_DEVICES=8"

# T12: non-numeric DEVICE_NUMBER rejected
DEVICE_NUMBER=x CHANNEL_PATH=ch0 expect_usage_64 "T12 accepted non-numeric device"


# T13: audioresample quality is wired in and defaults high (9)
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 ./nexvue-encode.sh)
grep -q "audioresample quality=9" <<<"$out" || fail "T13 default resample quality should be 9"

# T14: AUDIO_RESAMPLE_QUALITY is configurable and validated
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 AUDIO_RESAMPLE_QUALITY=3 ./nexvue-encode.sh)
grep -q "audioresample quality=3" <<<"$out" || fail "T14 custom resample quality not applied"
DEVICE_NUMBER=0 CHANNEL_PATH=ch0 AUDIO_RESAMPLE_QUALITY=11 expect_usage_64 "T14 accepted out-of-range resample quality"
DEVICE_NUMBER=0 CHANNEL_PATH=ch0 AUDIO_RESAMPLE_QUALITY=bogus expect_usage_64 "T14 accepted non-numeric resample quality"


# T15: audio queue depth is wired in and defaults to 100
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 ./nexvue-encode.sh)
grep -q "queue max-size-buffers=100 leaky=downstream" <<<"$out" || fail "T15 default audio queue depth should be 100"

# T16: AUDIO_QUEUE_BUFFERS is configurable and validated
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 AUDIO_QUEUE_BUFFERS=250 ./nexvue-encode.sh)
grep -q "max-size-buffers=250 leaky=downstream" <<<"$out" || fail "T16 custom audio queue depth not applied"
DEVICE_NUMBER=0 CHANNEL_PATH=ch0 AUDIO_QUEUE_BUFFERS=0 expect_usage_64 "T16 accepted zero audio queue depth"
DEVICE_NUMBER=0 CHANNEL_PATH=ch0 AUDIO_QUEUE_BUFFERS=bogus expect_usage_64 "T16 accepted non-numeric audio queue depth"

# T17: with LO_ENABLE, both audio tee branches get the configured depth
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 LO_ENABLE=true AUDIO_QUEUE_BUFFERS=75 ./nexvue-encode.sh)
occurrences=$(grep -o "max-size-buffers=75 leaky=downstream" <<<"$out" | wc -l)
[ "$occurrences" -ge 3 ] || fail "T17 audio queue depth should apply to capture queue + both tee branches (got $occurrences)"


# T18: captions side channel injects extract/convert branch (no burn-in overlay)
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 CAPTIONS_ENABLE=true CAPTIONS_PIPELINE_ONLY=true run_encode)
grep -q "output-cc=true" <<<"$out" || fail "T18 output-cc missing"
grep -q "ccextractor name=cc" <<<"$out" || fail "T18 ccextractor missing"
grep -q "cc.caption" <<<"$out" || fail "T18 caption pad branch missing"
grep -q "ccconverter" <<<"$out" || fail "T18 ccconverter missing"
grep -q "closedcaption/x-cea-608,format=raw" <<<"$out" || fail "T18 raw 608 caps missing"
grep -q "filesink location=/dev/null buffer-mode=unbuffered" <<<"$out" || fail "T18 filesink must be unbuffered (64KB default buffer starves the FIFO)"
grep -qE "cc708overlay|cea708overlay|cea608overlay" <<<"$out" && fail "T18 must not burn-in overlays"

# T19: CAPTIONS_ENABLE=false omits caption elements
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 CAPTIONS_ENABLE=false run_encode)
grep -q "output-cc=true" <<<"$out" && fail "T19 output-cc should be absent"
grep -q "ccextractor" <<<"$out" && fail "T19 ccextractor should be absent"
DEVICE_NUMBER=0 CHANNEL_PATH=ch0 CAPTIONS_ENABLE=bogus expect_usage_64 "T19 accepted bogus CAPTIONS_ENABLE"

# T20: AUDIO_CHANNELS stays discrete through Opus (no stereo downmix)
# T20: AUDIO_LAYOUT discrete Opus (no stereo downmix / no Dolby)
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 run_encode)
grep -q "decklinkaudiosrc device-number=0 channels=2" <<<"$out" || fail "T20 default stereo opens 2ch DeckLink"
grep -q "audio/x-raw,format=S16LE,rate=48000,channels=2" <<<"$out" || fail "T20 default Opus stereo S16LE"
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 AUDIO_LAYOUT=51 run_encode)
grep -q "decklinkaudiosrc device-number=0 channels=8" <<<"$out" || fail "T20 51 opens 8ch DeckLink"
grep -q "deinterleave name=adl" <<<"$out" || fail "T20 51 remix deinterleave"
grep -q "adl.src_5" <<<"$out" || fail "T20 51 keeps embed 6"
grep -q "adl.src_6" <<<"$out" && fail "T20 51 must not pull embed 7"
grep -q "audio/x-raw,format=S16LE,rate=48000,channels=6" <<<"$out" || fail "T20 51 Opus is 6ch S16LE"
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 AUDIO_LAYOUT=stereo_sap run_encode)
grep -q "deinterleave name=adl" <<<"$out" || fail "T20 stereo_sap remix deinterleave"
grep -q "adl.src_6" <<<"$out" || fail "T20 stereo_sap pulls embed 7"
grep -q "adl.src_7" <<<"$out" || fail "T20 stereo_sap pulls embed 8"
grep -q "audio/x-raw,format=S16LE,rate=48000,channels=4" <<<"$out" || fail "T20 stereo_sap Opus is 4ch S16LE"
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 AUDIO_LAYOUT=51_sap run_encode)
grep -q "audio/x-raw,format=S16LE,rate=48000,channels=8" <<<"$out" || fail "T20 51_sap Opus is 8ch S16LE"
out=$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 AUDIO_CHANNELS=6 run_encode)
grep -q "channels=6" <<<"$out" || fail "T20 legacy AUDIO_CHANNELS=6 → 51"
DEVICE_NUMBER=0 CHANNEL_PATH=ch0 AUDIO_LAYOUT=bogus expect_usage_64 "T20 accepted bogus AUDIO_LAYOUT"

echo "All pipeline assembly tests passed."
