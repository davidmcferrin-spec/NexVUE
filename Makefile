# Build decklink-status.
#
# Requires the Blackmagic DeckLink SDK (free, but license-gated download —
# grab "Desktop Video SDK" from the Blackmagic support site and unzip):
#
#   make DECKLINK_SDK=~/Blackmagic_DeckLink_SDK_14.x
#   sudo make install
#
# The SDK's Linux include dir provides DeckLinkAPI.h plus
# DeckLinkAPIDispatch.cpp, which does the dlopen of the installed
# libDeckLinkAPI.so at runtime (so the binary has no hard link dependency).

DECKLINK_SDK ?= /opt/decklink-sdk
SDK_INC      := $(DECKLINK_SDK)/Linux/include

CXX      ?= g++
CXXFLAGS += -std=c++17 -O2 -Wall -Wextra -I$(SDK_INC)
LDLIBS   += -ldl -lpthread

decklink-status: decklink-status.cpp $(SDK_INC)/DeckLinkAPIDispatch.cpp
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDLIBS)

install: decklink-status
	install -m 755 decklink-status /usr/local/bin/

clean:
	rm -f decklink-status

.PHONY: install clean
