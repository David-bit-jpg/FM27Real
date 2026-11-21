#pragma once

// Hooks FM's interface <-> engine channel traffic and logs it to tap.log.
// Call once, on an attached thread, after the runtime has settled.
void tap_install(void);
