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

// Same, for any serialized target (u8 pair count + (u32 code, i32 value) pairs),
// e.g. the "target" hex the encoder emits for interop references.
char *tap_query_target(const uint8_t *target, int target_len, const uint32_t *props, int n);

// Records the interface opened recently (oldest first). Returns the count.
int tap_seen(uint32_t *out, int cap);

// The record of the given kind (0x01 person, 0x03 club, 0x18 team) the player
// is looking at, judged from open UI channels; 0 when none stands out.
uint32_t tap_focus(uint8_t kind);

// Events the UI sent to the engine since sequence number `since`, as a JSON
// array [{seq, id, target, data}] (hex). Keeps the last 256.
char *tap_events_json(uint64_t since);

// Hex of the most recent target the UI opened for record type `type` (the
// value of its " 011" pair), or NULL.
char *tap_last_target(uint32_t type);

// Session id of the loaded career (0 if none).
uint32_t tap_guid(void);

// 4-character property codes: "eman" <-> 0x6e616d65 (little-endian).
void fourcc_string(uint32_t v, char out[16]);
uint32_t fourcc_parse(const char *s);

// Sends an event to the game's engine exactly as the interface does when the
// player clicks (IGamePlugin.SendEvent), on the main thread. Blocks up to 5 s.
// Returns 1 when sent. Never sends late: on timeout nothing is sent.
int tap_send_event(uint32_t id, const uint8_t *target, int target_len, const uint8_t *data, int data_len);

// The In-Game Editor fields the UI currently shows: [{label, value, key}] (key =
// the UI's own value channel). Built only from what the UI did; opens nothing.
char *tap_editor_json(void);

// Writes a value on one of those value channels, exactly as the editor does when
// the player edits the field (main thread, blocks up to 5 s). Returns 1 when
// written, -1 when the key is not a live editor value channel, 0 otherwise.
int tap_editor_set(uint64_t key, const uint8_t *data, int data_len);

// Opens the In-Game Editor panel on a person (kind "person") or club ("club"), as
// the pencil button does (IGEModule.OpenIGEPanel). Main thread, fault-guarded.
// Returns 1 when called, <= 0 on failure.
int tap_editor_open(uint32_t record, const char *kind);

// Closes the In-Game Editor panel (PanelManager.CloseAll on the editor's layer).
// With dry, only logs the layers involved. Returns 1 on success.
int tap_editor_close(int dry);
