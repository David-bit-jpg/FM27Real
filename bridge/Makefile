# Builds the FM27Real in-game bridge (universal: Apple Silicon + Intel).
OUT := ../build/libfm27real.dylib

$(OUT): fmreal_bridge.c
	mkdir -p ../build
	xcrun clang -arch arm64 -arch x86_64 -O2 -Wall -Wextra -dynamiclib -o $@ $<
	codesign -s - -f $@

clean:
	rm -f $(OUT)

.PHONY: clean
