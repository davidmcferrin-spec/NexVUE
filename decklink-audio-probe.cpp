// decklink-audio-probe.cpp — measure SDI embedded-audio energy on one DeckLink
// sub-device and print JSON (per-embed peak/RMS + active flags).
//
// DeckLink does not report "source layout". This tool captures 8 PCM embeds
// for ~1s and treats channels above a dBFS noise floor as active so Settings
// can suggest AUDIO_LAYOUT. The sub-device must be free (stop nexvue-encode@N).
//
// Usage: decklink-audio-probe <device-number> [duration_ms]
// Device index matches GStreamer decklinkvideosrc device-number / DEVICE_NUMBER.
//
// Build: see Makefile (requires the Blackmagic DeckLink SDK).

#include "DeckLinkAPI.h"
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

static const int DEFAULT_DURATION_MS = 1000;
static const int MAX_DURATION_MS = 3000;
static const int CHANNELS = 8;
static const double DEFAULT_THRESHOLD_DBFS = -60.0;
static const double SILENCE_DBFS = -120.0;

static std::string modeName(int64_t mode)
{
    switch ((BMDDisplayMode)mode) {
        case bmdModeNTSC:          return "525i59.94 (NTSC)";
        case bmdModePAL:           return "625i50 (PAL)";
        case bmdModeHD720p50:      return "720p50";
        case bmdModeHD720p5994:    return "720p59.94";
        case bmdModeHD720p60:      return "720p60";
        case bmdModeHD1080i50:     return "1080i50";
        case bmdModeHD1080i5994:   return "1080i59.94";
        case bmdModeHD1080i6000:   return "1080i60";
        case bmdModeHD1080p2398:   return "1080p23.98";
        case bmdModeHD1080p24:     return "1080p24";
        case bmdModeHD1080p25:     return "1080p25";
        case bmdModeHD1080p2997:   return "1080p29.97";
        case bmdModeHD1080p30:     return "1080p30";
        case bmdModeHD1080p50:     return "1080p50";
        case bmdModeHD1080p5994:   return "1080p59.94";
        case bmdModeHD1080p6000:   return "1080p60";
        case bmdMode2k2398:        return "2K 23.98";
        case bmdMode4K2160p2997:   return "2160p29.97";
        case bmdMode4K2160p5994:   return "2160p59.94";
        default: {
            if (mode == 0) return "unknown";
            char fourcc[5] = {
                (char)((mode >> 24) & 0xFF), (char)((mode >> 16) & 0xFF),
                (char)((mode >> 8) & 0xFF),  (char)(mode & 0xFF), 0 };
            return std::string("fourcc:") + fourcc;
        }
    }
}

static std::string jsonEscape(const char* s)
{
    std::string out;
    for (const char* p = s; *p; ++p) {
        if (*p == '"' || *p == '\\') { out += '\\'; out += *p; }
        else if (*p == '\n') out += "\\n";
        else out += *p;
    }
    return out;
}

static double toDbfs(double peakLinear)
{
    if (peakLinear <= 1e-12) return SILENCE_DBFS;
    double db = 20.0 * std::log10(peakLinear);
    if (db < SILENCE_DBFS) return SILENCE_DBFS;
    return db;
}

class AudioProbeCallback : public IDeckLinkInputCallback {
public:
    std::mutex mtx;
    std::condition_variable cv;
    std::atomic<bool> locked{false};
    std::atomic<bool> gotAudio{false};
    int64_t detectedMode{0};

    // Per-channel peak |sample| and sum of squares (normalized to ±1.0).
    double peak[CHANNELS]{};
    double sumSq[CHANNELS]{};
    uint64_t sampleFrames{0};

    HRESULT STDMETHODCALLTYPE VideoInputFormatChanged(
        BMDVideoInputFormatChangedEvents /*events*/,
        IDeckLinkDisplayMode* newMode,
        BMDDetectedVideoInputFormatFlags /*flags*/) override
    {
        if (newMode) {
            std::lock_guard<std::mutex> lk(mtx);
            detectedMode = (int64_t)newMode->GetDisplayMode();
            locked = true;
            cv.notify_all();
        }
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE VideoInputFrameArrived(
        IDeckLinkVideoInputFrame* frame,
        IDeckLinkAudioInputPacket* audio) override
    {
        if (frame) {
            BMDFrameFlags ff = frame->GetFlags();
            if (!(ff & bmdFrameHasNoInputSource)) {
                locked = true;
                cv.notify_all();
            }
        }
        if (!audio) return S_OK;

        void* bytes = nullptr;
        if (audio->GetBytes(&bytes) != S_OK || !bytes) return S_OK;
        const uint32_t frames = audio->GetSampleFrameCount();
        if (frames == 0) return S_OK;

        // EnableAudioInput uses 32-bit integer interleaved PCM.
        const int32_t* s = static_cast<const int32_t*>(bytes);
        const double scale = 1.0 / 2147483647.0;

        std::lock_guard<std::mutex> lk(mtx);
        for (uint32_t f = 0; f < frames; ++f) {
            for (int c = 0; c < CHANNELS; ++c) {
                double v = std::fabs(s[f * CHANNELS + c] * scale);
                if (v > peak[c]) peak[c] = v;
                sumSq[c] += v * v;
            }
        }
        sampleFrames += frames;
        gotAudio = true;
        cv.notify_all();
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, void**) override { return E_NOINTERFACE; }
    ULONG   STDMETHODCALLTYPE AddRef()  override { return 1; }
    ULONG   STDMETHODCALLTYPE Release() override { return 1; }
};

static void printError(int device, const char* err, bool busy)
{
    printf("{\"ok\":false,\"device\":%d,\"busy\":%s,\"error\":\"%s\",\"channels\":[]}\n",
           device, busy ? "true" : "false", err);
}

int main(int argc, char** argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s <device-number> [duration_ms]\n", argv[0]);
        printError(-1, "usage", false);
        return 2;
    }

    int deviceIndex = -1;
    if (sscanf(argv[1], "%d", &deviceIndex) != 1 || deviceIndex < 0 || deviceIndex > 15) {
        printError(-1, "invalid_device", false);
        return 2;
    }

    int durationMs = DEFAULT_DURATION_MS;
    if (argc >= 3) {
        int d = 0;
        if (sscanf(argv[2], "%d", &d) == 1 && d >= 200 && d <= MAX_DURATION_MS)
            durationMs = d;
    }

    IDeckLinkIterator* iterator = CreateDeckLinkIteratorInstance();
    if (!iterator) {
        printError(deviceIndex, "no_decklink_api", false);
        return 1;
    }

    IDeckLink* deckLink = nullptr;
    int index = 0;
    while (iterator->Next(&deckLink) == S_OK) {
        if (index == deviceIndex) break;
        deckLink->Release();
        deckLink = nullptr;
        index++;
    }
    iterator->Release();

    if (!deckLink) {
        printError(deviceIndex, "device_not_found", false);
        return 1;
    }

    const char* name = nullptr;
    std::string displayName = "unknown";
    if (deckLink->GetDisplayName(&name) == S_OK && name) {
        displayName = name;
        free((void*)name);
    }

    IDeckLinkInput* input = nullptr;
    if (deckLink->QueryInterface(IID_IDeckLinkInput, (void**)&input) != S_OK || !input) {
        deckLink->Release();
        printError(deviceIndex, "no_input_interface", false);
        return 1;
    }

    AudioProbeCallback cb;
    input->SetCallback(&cb);

    HRESULT hr = input->EnableVideoInput(
        bmdModeHD1080i5994, bmdFormat8BitYUV, bmdVideoInputEnableFormatDetection);
    if (hr != S_OK) {
        input->SetCallback(nullptr);
        input->Release();
        deckLink->Release();
        printError(deviceIndex, "busy", true);
        return 3;
    }

    hr = input->EnableAudioInput(
        bmdAudioSampleRate48kHz, bmdAudioSampleType32bitInteger, CHANNELS);
    if (hr != S_OK) {
        input->DisableVideoInput();
        input->SetCallback(nullptr);
        input->Release();
        deckLink->Release();
        printError(deviceIndex, "audio_enable_failed", false);
        return 1;
    }

    if (input->StartStreams() != S_OK) {
        input->DisableAudioInput();
        input->DisableVideoInput();
        input->SetCallback(nullptr);
        input->Release();
        deckLink->Release();
        printError(deviceIndex, "start_streams_failed", false);
        return 1;
    }

    // Wait until we see audio, then accumulate for durationMs.
    {
        std::unique_lock<std::mutex> lk(cb.mtx);
        cb.cv.wait_for(lk, std::chrono::milliseconds(800),
                       [&]{ return cb.gotAudio.load(); });
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(durationMs));

    input->StopStreams();

    if (cb.locked.load() && cb.detectedMode == 0) {
        IDeckLinkStatus* st = nullptr;
        if (deckLink->QueryInterface(IID_IDeckLinkStatus, (void**)&st) == S_OK) {
            int64_t v = 0;
            if (st->GetInt(bmdDeckLinkStatusDetectedVideoInputMode, &v) == S_OK)
                cb.detectedMode = v;
            st->Release();
        }
    }

    input->DisableAudioInput();
    input->DisableVideoInput();
    input->SetCallback(nullptr);
    input->Release();
    deckLink->Release();

    const double thresh = DEFAULT_THRESHOLD_DBFS;
    std::vector<int> activeMask;
    activeMask.reserve(CHANNELS);

    printf("{\"ok\":true,\"device\":%d,\"name\":\"%s\","
           "\"input_locked\":%s,\"input_mode\":\"%s\","
           "\"busy\":false,\"sample_rate\":48000,\"channels_captured\":%d,"
           "\"duration_ms\":%d,\"threshold_dbfs\":%.1f,\"got_audio\":%s,"
           "\"channels\":[",
           deviceIndex,
           jsonEscape(displayName.c_str()).c_str(),
           cb.locked.load() ? "true" : "false",
           modeName(cb.locked.load() ? cb.detectedMode : 0).c_str(),
           CHANNELS,
           durationMs,
           thresh,
           cb.gotAudio.load() ? "true" : "false");

    for (int c = 0; c < CHANNELS; ++c) {
        double peakDb = toDbfs(cb.peak[c]);
        double rmsLin = 0.0;
        if (cb.sampleFrames > 0)
            rmsLin = std::sqrt(cb.sumSq[c] / (double)cb.sampleFrames);
        double rmsDb = toDbfs(rmsLin);
        bool active = peakDb >= thresh;
        if (active) activeMask.push_back(c + 1);
        printf("%s{\"index\":%d,\"peak_dbfs\":%.1f,\"rms_dbfs\":%.1f,\"active\":%s}",
               c ? "," : "",
               c + 1,
               peakDb,
               rmsDb,
               active ? "true" : "false");
    }

    printf("],\"active_count\":%zu,\"active_mask\":[", activeMask.size());
    for (size_t i = 0; i < activeMask.size(); ++i)
        printf("%s%d", i ? "," : "", activeMask[i]);
    printf("]}\n");
    return 0;
}
