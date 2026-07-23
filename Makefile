# Build decklink-status + decklink-audio-probe.
#
# Requires the Blackmagic DeckLink SDK (free, license-gated — grab the
# "Desktop Video SDK" from the Blackmagic support site and unzip). Use the
# SAME major version as the installed Desktop Video driver (DV 16 + SDK 16).
#
#   make DECKLINK_SDK=/opt/decklink-sdk
#   sudo make install
#
# DECKLINK_SDK must point at the SDK ROOT (the folder containing Linux/).
#
# IMPORTANT — paths with spaces: make cannot reliably carry space-containing
# paths in prerequisites or its text functions. The Blackmagic SDK unzips to
# a folder WITH spaces ("Blackmagic DeckLink SDK 16.0"). Symlink it to a
# space-free path once, and point DECKLINK_SDK at the symlink:
#
#   ln -s "/usr/local/src/Blackmagic DeckLink SDK 16.0" /opt/decklink-sdk
#   make                       # /opt/decklink-sdk is the default
#
# The recipe resolves the header dir and dispatch source with a shell script
# at build time (shell handles spaces fine), so even a symlink whose TARGET
# contains spaces works — only the symlink path itself must be space-free.

DECKLINK_SDK ?= /opt/decklink-sdk

CXX      ?= g++
CXXFLAGS += -std=c++17 -O2 -Wall -Wextra
LDLIBS   += -ldl -lpthread -lm

decklink-status: decklink-status.cpp
	@sdk='$(DECKLINK_SDK)'; \
	hdr="$$(find -L "$$sdk" -name DeckLinkAPI.h 2>/dev/null | head -n1)"; \
	dispatch="$$(find -L "$$sdk" -name DeckLinkAPIDispatch.cpp 2>/dev/null | head -n1)"; \
	if [ -z "$$hdr" ]; then echo "ERROR: DeckLinkAPI.h not found under $$sdk/Linux"; exit 1; fi; \
	if [ -z "$$dispatch" ]; then echo "ERROR: DeckLinkAPIDispatch.cpp not found under $$sdk/Linux"; exit 1; fi; \
	incdir="$$(dirname "$$hdr")"; \
	echo "Using headers:  $$incdir"; \
	echo "Using dispatch: $$dispatch"; \
	$(CXX) $(CXXFLAGS) -I"$$incdir" -o decklink-status decklink-status.cpp "$$dispatch" $(LDLIBS)

decklink-audio-probe: decklink-audio-probe.cpp
	@sdk='$(DECKLINK_SDK)'; \
	hdr="$$(find -L "$$sdk" -name DeckLinkAPI.h 2>/dev/null | head -n1)"; \
	dispatch="$$(find -L "$$sdk" -name DeckLinkAPIDispatch.cpp 2>/dev/null | head -n1)"; \
	if [ -z "$$hdr" ]; then echo "ERROR: DeckLinkAPI.h not found under $$sdk/Linux"; exit 1; fi; \
	if [ -z "$$dispatch" ]; then echo "ERROR: DeckLinkAPIDispatch.cpp not found under $$sdk/Linux"; exit 1; fi; \
	incdir="$$(dirname "$$hdr")"; \
	echo "Using headers:  $$incdir"; \
	echo "Using dispatch: $$dispatch"; \
	$(CXX) $(CXXFLAGS) -I"$$incdir" -o decklink-audio-probe decklink-audio-probe.cpp "$$dispatch" $(LDLIBS)

all: decklink-status decklink-audio-probe

install: all
	install -m 755 decklink-status /usr/local/bin/
	install -m 755 decklink-audio-probe /usr/local/bin/

clean:
	rm -f decklink-status decklink-audio-probe

.PHONY: all install clean
