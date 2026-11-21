# Builds the FM27Real in-game bridge (universal: Apple Silicon + Intel).
OUT := ../build/libfm27real.dylib
SRC := $(wildcard *.c) $(wildcard *.m)
HDR := $(wildcard *.h)

$(OUT): $(SRC) $(HDR)
	mkdir -p ../build
	xcrun clang -arch arm64 -arch x86_64 -O2 -Wall -Wextra -fobjc-arc -dynamiclib \
		-framework AppKit -framework WebKit -o $@ $(SRC)
	codesign -s - -f $@

clean:
	rm -f $(OUT)

.PHONY: clean
