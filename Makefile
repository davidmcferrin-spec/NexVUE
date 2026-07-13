# Build decklink-status.
#
# Requires the Blackmagic DeckLink SDK (free, but license-gated download —
# grab "Desktop Video SDK" from the Blackmagic support site and unzip).
# Use the SAME major version as the installed Desktop Video driver
# (e.g. DV 16 + SDK 16).
#
#   make DECKLINK_SDK=/opt/decklink-sdk
#   sudo make install
#
# DECKLINK_SDK must point at the SDK ROOT (the folder containing Linux/).
# Tip for space-in-path SDKs, symlink once:
#   ln -s "/usr/local/src/Blackmagic DeckLink SDK 16.0" /opt/decklink-sdk
#
# DeckLinkAPIDispatch.cpp does the dlopen of the installed libDeckLinkAPI.so
# at runtime (so the built binary has no hard link dependency). Its location
# moved between SDK versions — under Linux/include in older SDKs, under
# Linux/Samples (or elsewhere) in newer ones — so we locate it dynamically
# rather than assuming a fixed path.

DECKLINK_SDK ?= /opt/decklink-sdk
SDK_INC      := $(DECKLINK_SDK)/Linux/include

# Find the header dir (dir containing DeckLinkAPI.h) and the dispatch source,
# anywhere under the SDK root. Fall back to SDK_INC for the include path.
DECKLINK_HDR_DIR := $(dir $(firstword $(shell find $(DECKLINK_SDK) -name DeckLinkAPI.h 2>/dev/null)))
DECKLINK_DISPATCH := $(firstword $(shell find $(DECKLINK_SDK) -name DeckLinkAPIDispatch.cpp 2>/dev/null))
INC_DIR := $(if $(DECKLINK_HDR_DIR),$(DECKLINK_HDR_DIR),$(SDK_INC))

CXX      ?= g++
CXXFLAGS += -std=c++17 -O2 -Wall -Wextra -I$(INC_DIR)
LDLIBS   += -ldl -lpthread

decklink-status: decklink-status.cpp
	@test -n "$(DECKLINK_DISPATCH)" || { \
	  echo "ERROR: DeckLinkAPIDispatch.cpp not found under $(DECKLINK_SDK)"; \
	  echo "       Check DECKLINK_SDK points at the SDK root (folder containing Linux/)."; \
	  echo "       Found headers at: $(DECKLINK_HDR_DIR)"; \
	  exit 1; }
	@test -n "$(DECKLINK_HDR_DIR)" || { \
	  echo "ERROR: DeckLinkAPI.h not found under $(DECKLINK_SDK)"; exit 1; }
	@echo "Using headers:  $(INC_DIR)"
	@echo "Using dispatch: $(DECKLINK_DISPATCH)"
	$(CXX) $(CXXFLAGS) -o $@ decklink-status.cpp "$(DECKLINK_DISPATCH)" $(LDLIBS)

install: decklink-status
	install -m 755 decklink-status /usr/local/bin/

clean:
	rm -f decklink-status

.PHONY: install clean
