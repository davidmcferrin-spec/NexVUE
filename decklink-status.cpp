// decklink-status.cpp — dump SDI input & reference status for all DeckLink
// sub-devices as JSON on stdout.
//
// Queries the IDeckLinkStatus interface, which is safe to use while another
// process (the GStreamer encoder) holds the capture interface.
//
// Output shape:
// {
//   "devices": [
//     { "index": 0, "name": "DeckLink Quad 2 (1)",
//       "input_locked": true, "input_mode": "1080i59.94",
//       "reference_locked": true, "reference_mode": "1080i59.94" },
//     ...
//   ]
// }
//
// Build (requires the Blackmagic DeckLink SDK, free download):
//   make DECKLINK_SDK=/path/to/SDK   (see Makefile)
//
// Device index order matches GStreamer's decklinkvideosrc device-number —
// both walk the same IDeckLinkIterator enumeration order.

#include "DeckLinkAPI.h"
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

// Map common BMDDisplayMode values to broadcast-friendly names; anything
// unmapped falls back to the raw fourcc so new formats still report usefully.
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

int main()
{
    IDeckLinkIterator* iterator = CreateDeckLinkIteratorInstance();
    if (!iterator) {
        fprintf(stderr, "DeckLink drivers not installed or no API available\n");
        printf("{\"devices\":[],\"error\":\"no_decklink_api\"}\n");
        return 1;
    }

    printf("{\"devices\":[");

    IDeckLink* deckLink = nullptr;
    int index = 0;
    bool first = true;

    while (iterator->Next(&deckLink) == S_OK) {
        const char* name = nullptr;
        std::string displayName = "unknown";
        if (deckLink->GetDisplayName(&name) == S_OK && name) {
            displayName = name;
            free((void*)name);
        }

        bool    inputLocked = false, refLocked = false;
        int64_t inputMode = 0, refMode = 0;

        IDeckLinkStatus* status = nullptr;
        if (deckLink->QueryInterface(IID_IDeckLinkStatus, (void**)&status) == S_OK) {
            bool    b = false;
            int64_t v = 0;
            if (status->GetFlag(bmdDeckLinkStatusVideoInputSignalLocked, &b) == S_OK)
                inputLocked = b;
            if (status->GetInt(bmdDeckLinkStatusDetectedVideoInputMode, &v) == S_OK)
                inputMode = v;
            if (status->GetFlag(bmdDeckLinkStatusReferenceSignalLocked, &b) == S_OK)
                refLocked = b;
            if (status->GetInt(bmdDeckLinkStatusReferenceSignalMode, &v) == S_OK)
                refMode = v;
            status->Release();
        }

        printf("%s{\"index\":%d,\"name\":\"%s\","
               "\"input_locked\":%s,\"input_mode\":\"%s\","
               "\"reference_locked\":%s,\"reference_mode\":\"%s\"}",
               first ? "" : ",",
               index,
               jsonEscape(displayName.c_str()).c_str(),
               inputLocked ? "true" : "false",
               modeName(inputLocked ? inputMode : 0).c_str(),
               refLocked ? "true" : "false",
               modeName(refLocked ? refMode : 0).c_str());

        first = false;
        index++;
        deckLink->Release();
    }

    printf("]}\n");
    iterator->Release();
    return 0;
}
