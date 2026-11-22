// Windows has no in-game panel yet: the phone UI opens in the browser instead
// (Windows/launch.ps1). macOS uses overlay.m.
#if defined(_WIN32)
#include "overlay.h"
void overlay_install(void) {}
#endif
