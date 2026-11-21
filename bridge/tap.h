#pragma once
#include <stdint.h>

// Hooks FM's interface <-> engine channel traffic (logged to tap.log) and
// enables our own queries. Call once, on an attached thread.
void tap_install(void);

// True once a career is loaded and queries can run.
int tap_ready(void);

// Queries properties of a database record (e.g. 0x010baef4) on the game's main
// thread. Blocks up to ~20 s. Returns a malloc'd JSON object {"code": value},
// or NULL when not ready. Call from a background thread.
char *tap_query(uint32_t record, const uint32_t *props, int n);

// Records the interface opened recently (oldest first). Returns the count.
int tap_seen(uint32_t *out, int cap);

// Session id of the loaded career (0 if none).
uint32_t tap_guid(void);

// 4-character property codes: "eman" <-> 0x6e616d65 (little-endian).
void fourcc_string(uint32_t v, char out[16]);
uint32_t fourcc_parse(const char *s);
