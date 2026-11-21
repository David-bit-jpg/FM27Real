// Data channels: observes the traffic between FM's interface and its native
// game engine (tap.log), and runs our own queries against the engine.
//
// Hooks are installed by swapping entries in classes' virtual-method tables
// (ordinary heap data); game code is never modified, so the hardened
// runtime's code-signing checks are unaffected. Protocol: docs/PROTOCOL.md.
#include "tap.h"

#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "il2cpp_api.h"
#include "json.h"
#include "log.h"

#if defined(__aarch64__)

typedef uint64_t u64;
typedef void (*Fn8)(u64, u64, u64, u64, u64, u64, u64, u64);

static Fn8 g_orig_response, g_orig_event, g_orig_open, g_orig_tick, g_orig_actions;
static FILE *g_tap;
static size_t g_tap_bytes;
static const size_t kTapLimit = 64u << 20;  // stop logging after 64 MB
static __thread int t_busy;                 // re-entrancy guard for logging

// --- runtime handles resolved at install time --------------------------------
static const MethodInfo *g_typed_value_as_string, *g_typed_value_get;
static Il2CppClass *g_record_ref_class;  // FM.UI.DatabaseRecordReference
static Il2CppClass *g_string_class;
static Il2CppClass *g_dynref_class;   // SI.Bindable.DynamicReference: a Dictionary<uint, TypedValue>
static Il2CppClass *g_interop_class;  // SI.Interop.InteropReference: identity = list of (u32, i32) pairs
static const MethodInfo *g_dynref_get_id;
static size_t g_interop_pairs_off;
static size_t g_record_index_off, g_record_type_off;
static size_t g_receiver_list_offset;      // GameReceiver.m_resusableTypedList
static size_t g_receiver_callback_offset;  // GameReceiver.m_onChannelDataChange
static size_t g_list_items_offset, g_list_size_offset;
static const MethodInfo *g_open_method, *g_actions_method;
static int32_t g_mode_add_node = -1, g_mode_remove_node = -1, g_mode_close = -1, g_mode_set_property = -1;
static int g_action_size;  // sizeof(ChannelAction)
static u64 g_plugin;       // the IGamePlugin native wrapper instance
static struct { int ptr_off, len_off; } g_span = {-1, -1};

#define CODE_011 0x31313020u   // " 011": record type
#define CODE_GUID 0x47756964u  // "Guid": loaded game session
#define CODE_FERD 0x64726566u  // "ferd": database record id (high byte = kind)
static uint32_t g_guid, g_record_type, g_max_gen;

// Records the UI has shown recently (newest last), for discovery.
#define SEEN_MAX 256
static uint32_t g_seen[SEEN_MAX];
static int g_seen_count;
static pthread_mutex_t g_seen_lock = PTHREAD_MUTEX_INITIALIZER;

// --- small helpers -------------------------------------------------------------
static void tap_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void tap_printf(const char *fmt, ...) {
    if (!g_tap || g_tap_bytes > kTapLimit) return;
    va_list ap;
    va_start(ap, fmt);
    int w = vfprintf(g_tap, fmt, ap);
    va_end(ap);
    if (w > 0) g_tap_bytes += (size_t)w;
    if (g_tap_bytes > kTapLimit) fputs("--- tap.log size limit reached ---\n", g_tap);
}

static void hex(const uint8_t *p, int n, char *out, size_t cap) {
    size_t o = 0;
    for (int i = 0; i < n && o + 3 < cap; i++) o += (size_t)snprintf(out + o, cap - o, "%02x", p[i]);
    out[o] = 0;
}

static int safe_read(const void *addr, void *out, size_t n) {
    mach_vm_size_t got = 0;
    return mach_vm_read_overwrite(mach_task_self(), (mach_vm_address_t)addr, n, (mach_vm_address_t)out, &got) ==
               KERN_SUCCESS && got == n;
}

// Span<T> arguments arrive as two registers each: data pointer, then length.
static void span_arg(const u64 *x, int reg, const void **data, int *len) {
    uint8_t raw[16];
    u64 pair[2] = {x[reg], x[reg + 1]};
    memcpy(raw, pair, sizeof raw);  // memcpy, not pointer casts: avoids aliasing UB
    void *p;
    int32_t n;
    memcpy(&p, raw + g_span.ptr_off, sizeof p);
    memcpy(&n, raw + g_span.len_off, sizeof n);
    *data = p;
    *len = n;
}

// Copies up to cap bytes of a span; returns elements copied (0 if unreadable).
static int span_copy(const void *data, int len, size_t elem, void *buf, size_t cap) {
    if (!data || len <= 0) return 0;
    size_t want = (size_t)len * elem;
    if (want > cap) want = cap - cap % elem;
    return safe_read(data, buf, want) ? (int)(want / elem) : 0;
}

void fourcc_string(uint32_t v, char out[16]) {
    char c[4] = {(char)(v & 0xff), (char)(v >> 8), (char)(v >> 16), (char)(v >> 24)};
    for (int i = 0; i < 4; i++)
        if (c[i] < 0x20 || c[i] > 0x7e) { snprintf(out, 16, "#%08x", v); return; }
    snprintf(out, 16, "%c%c%c%c", c[0], c[1], c[2], c[3]);
}

uint32_t fourcc_parse(const char *s) {
    if (s[0] == '#') return (uint32_t)strtoul(s + 1, NULL, 16);
    uint32_t v = 0;
    for (int i = 0; i < 4 && s[i]; i++) v |= (uint32_t)(uint8_t)s[i] << (8 * i);
    return v;
}

// Channel key -> property for UI traffic (tap.log readability).
#define KEY_SLOTS 65536
static struct { u64 key; uint32_t prop, record; } g_keys[KEY_SLOTS];

static void key_put(u64 key, uint32_t prop, uint32_t record) {
    uint32_t h = (uint32_t)(key ^ (key >> 29)) & (KEY_SLOTS - 1);
    for (int i = 0; i < 64; i++, h = (h + 1) & (KEY_SLOTS - 1))
        if (!g_keys[h].key || g_keys[h].key == key) {
            g_keys[h].key = key;
            g_keys[h].prop = prop;
            g_keys[h].record = record;
            return;
        }
}

static uint32_t key_record(u64 key) {
    uint32_t h = (uint32_t)(key ^ (key >> 29)) & (KEY_SLOTS - 1);
    for (int i = 0; i < 64; i++, h = (h + 1) & (KEY_SLOTS - 1)) {
        if (g_keys[h].key == key) return g_keys[h].record;
        if (!g_keys[h].key) break;
    }
    return 0;
}

// Open UI channels per database record. A record with far more open channels
// than any other is what the player is looking at (a profile page opens 100+
// channels on one person; a squad list opens ~20 on each of many).
#define OPEN_SLOTS 8192
static struct { uint32_t record; int open; } g_open[OPEN_SLOTS];
static pthread_mutex_t g_open_lock = PTHREAD_MUTEX_INITIALIZER;

static void open_add(uint32_t record, int delta) {
    if (!record) return;
    pthread_mutex_lock(&g_open_lock);
    uint32_t h = (record * 2654435761u) & (OPEN_SLOTS - 1);
    for (int i = 0; i < 64; i++, h = (h + 1) & (OPEN_SLOTS - 1))
        if (!g_open[h].record || g_open[h].record == record) {
            g_open[h].record = record;
            g_open[h].open += delta;
            if (g_open[h].open < 0) g_open[h].open = 0;
            break;
        }
    pthread_mutex_unlock(&g_open_lock);
}

uint32_t tap_focus(uint8_t kind) {
    uint32_t best = 0;
    int best_n = 0, second = 0;
    pthread_mutex_lock(&g_open_lock);
    for (int i = 0; i < OPEN_SLOTS; i++) {
        if (!g_open[i].record || (g_open[i].record >> 24) != kind) continue;
        int n = g_open[i].open;
        if (n > best_n) { second = best_n; best_n = n; best = g_open[i].record; }
        else if (n > second) second = n;
    }
    pthread_mutex_unlock(&g_open_lock);
    return best_n >= 40 && best_n >= 2 * second ? best : 0;
}

static uint32_t key_get(u64 key) {
    uint32_t h = (uint32_t)(key ^ (key >> 29)) & (KEY_SLOTS - 1);
    for (int i = 0; i < 64; i++, h = (h + 1) & (KEY_SLOTS - 1)) {
        if (g_keys[h].key == key) return g_keys[h].prop;
        if (!g_keys[h].key) break;
    }
    return 0;
}

static Il2CppObject *invoke0(const MethodInfo *m, void *obj) {
    Il2CppException *exc = NULL;
    Il2CppObject *r = il2cpp_runtime_invoke(m, obj, NULL, &exc);
    return exc ? NULL : r;
}

static void typed_value_string(Il2CppObject *v, char *buf, size_t n) {
    if (!v) { strlcpy(buf, "null", n); return; }
    const MethodInfo *m = il2cpp_object_get_virtual_method(v, g_typed_value_as_string);
    Il2CppObject *s = invoke0(m ? m : g_typed_value_as_string, v);
    il2cpp_utf8((Il2CppString *)s, buf, n);
    for (char *c = buf; *c; c++) if (*c == '\n' || *c == '\t') *c = ' ';
}

static int list_items(Il2CppObject *list, Il2CppObject **out, int cap) {
    uint8_t *arr = NULL;
    int32_t count = 0;
    if (!list || !g_list_items_offset ||
        !safe_read((uint8_t *)list + g_list_items_offset, &arr, sizeof arr) ||
        !safe_read((uint8_t *)list + g_list_size_offset, &count, sizeof count) || !arr || count <= 0)
        return 0;
    if (count > cap) count = cap;
    return safe_read(arr + 0x20, out, sizeof(void *) * (size_t)count) ? count : 0;  // array data at 0x20
}

static void learn_list_layout(Il2CppObject *list) {
    Il2CppClass *k = il2cpp_object_get_class(list);
    void *it = NULL;
    FieldInfo *f;
    while ((f = il2cpp_class_get_fields(k, &it))) {
        const char *n = il2cpp_field_get_name(f);
        if (!strcmp(n, "_items")) g_list_items_offset = il2cpp_field_get_offset(f);
        if (!strcmp(n, "_size")) g_list_size_offset = il2cpp_field_get_offset(f);
    }
    log_line("tap: List layout items@0x%zx size@0x%zx", g_list_items_offset, g_list_size_offset);
}

// Values GameReceiver decoded into m_resusableTypedList (after the original call).
static int decoded_values(Il2CppObject *receiver, Il2CppObject **out, int cap) {
    Il2CppObject *list = NULL;
    if (!safe_read((uint8_t *)receiver + g_receiver_list_offset, &list, sizeof list) || !list) return 0;
    if (!g_list_items_offset) learn_list_layout(list);
    return list_items(list, out, cap);
}

static int derives_from(Il2CppClass *k, Il2CppClass *base) {
    for (int depth = 0; k && depth < 32; depth++, k = il2cpp_class_get_parent(k))
        if (k == base) return 1;
    return 0;
}

// --- Dictionary<uint, TypedValue> (SI.Core.Record) ------------------------------
static struct {
    int known;  // 0 unknown, 1 ok, -1 unsupported
    size_t entries_off, count_off;
    int entry_size, hash_off, next_off, key_off, value_off;
} g_dict;

static void learn_dict_layout(Il2CppClass *k) {
    g_dict.known = -1;
    g_dict.hash_off = g_dict.next_off = g_dict.key_off = g_dict.value_off = -1;
    for (int depth = 0; k && depth < 8; depth++, k = il2cpp_class_get_parent(k)) {
        void *it = NULL;
        FieldInfo *f;
        while ((f = il2cpp_class_get_fields(k, &it))) {
            const char *n = il2cpp_field_get_name(f);
            if (il2cpp_field_get_flags(f) & FIELD_ATTRIBUTE_STATIC) continue;
            if (!strcmp(n, "_entries") || !strcmp(n, "entries")) {
                g_dict.entries_off = il2cpp_field_get_offset(f);
                Il2CppClass *arr = il2cpp_class_from_type(il2cpp_field_get_type(f));
                Il2CppClass *el = arr ? il2cpp_class_get_element_class(arr) : NULL;
                // Entry is a struct; its own value size is the array stride (the
                // array API reports a pointer size for this shared generic).
                g_dict.entry_size = el ? il2cpp_class_value_size(el, NULL) : 0;
                void *it2 = NULL;
                FieldInfo *ef;
                while (el && (ef = il2cpp_class_get_fields(el, &it2))) {
                    const char *en = il2cpp_field_get_name(ef);
                    int off = (int)il2cpp_field_get_offset(ef) - 0x10;  // value-type offsets include the header
                    if (!strcmp(en, "hashCode")) g_dict.hash_off = off;
                    else if (!strcmp(en, "next")) g_dict.next_off = off;
                    else if (!strcmp(en, "key")) g_dict.key_off = off;
                    else if (!strcmp(en, "value")) g_dict.value_off = off;
                }
            }
            if (!strcmp(n, "_count") || !strcmp(n, "count")) g_dict.count_off = il2cpp_field_get_offset(f);
        }
        if (g_dict.entries_off && g_dict.count_off) break;
    }
    if (g_dict.value_off >= 0 && g_dict.entry_size < g_dict.value_off + 8)
        g_dict.entry_size = (g_dict.value_off + 8 + 7) & ~7;  // never smaller than the fields it holds
    if (g_dict.entries_off && g_dict.count_off && g_dict.entry_size > 0 && g_dict.key_off >= 0 &&
        g_dict.value_off >= 0 && (g_dict.hash_off >= 0 || g_dict.next_off >= 0))
        g_dict.known = 1;
    log_line("tap: Dictionary layout entries@0x%zx count@0x%zx entry=%d hash@%d next@%d key@%d value@%d -> %s",
             g_dict.entries_off, g_dict.count_off, g_dict.entry_size, g_dict.hash_off, g_dict.next_off,
             g_dict.key_off, g_dict.value_off, g_dict.known > 0 ? "ok" : "unsupported");
}

static void encode_value(JsonBuf *j, Il2CppObject *tv, int depth);
static Il2CppClass *g_typed_value_class;

// True when obj is readable and its class derives from TypedValue. Classes seen
// before are cached so the parent walk only runs once per class.
static int is_typed_value(Il2CppObject *obj) {
    static Il2CppClass *good[64];
    static int ngood;
    Il2CppClass *k = NULL;
    uint8_t probe[16];
    if (!obj || !safe_read(obj, &k, sizeof k) || !k || !safe_read(k, probe, sizeof probe)) return 0;
    for (int i = 0; i < ngood; i++) if (good[i] == k) return 1;
    if (!derives_from(k, g_typed_value_class)) return 0;
    if (ngood < 64) good[ngood++] = k;
    return 1;
}

// {"dyn": "<reference type id>", "props": {"code": value, ...}}
static void encode_dynref(JsonBuf *j, Il2CppObject *obj, int depth) {
    if (!g_dict.known) learn_dict_layout(il2cpp_object_get_class(obj));
    uint32_t type_id = 0;
    Il2CppObject *boxed = g_dynref_get_id ? invoke0(il2cpp_object_get_virtual_method(obj, g_dynref_get_id), obj) : NULL;
    if (boxed) safe_read((uint8_t *)boxed + 0x10, &type_id, 4);
    char head[48];
    snprintf(head, sizeof head, "{\"dyn\":\"%08x\",\"props\":{", type_id);
    json_raw(j, head);
    uint8_t *entries = NULL;
    int32_t count = 0;
    if (g_dict.known > 0 && safe_read((uint8_t *)obj + g_dict.entries_off, &entries, sizeof entries) &&
        safe_read((uint8_t *)obj + g_dict.count_off, &count, sizeof count) && entries) {
        int first = 1;
        for (int i = 0; i < count && i < 4096; i++) {
            uint8_t e[64];
            if (g_dict.entry_size > (int)sizeof e ||
                !safe_read(entries + 0x20 + (size_t)i * (size_t)g_dict.entry_size, e, (size_t)g_dict.entry_size))
                break;
            int32_t hash = 0, next = 0;
            if (g_dict.hash_off >= 0) memcpy(&hash, e + g_dict.hash_off, 4);
            if (g_dict.next_off >= 0) memcpy(&next, e + g_dict.next_off, 4);
            if (g_dict.hash_off >= 0 ? hash < 0 : next < -1) continue;  // free slot
            uint32_t key;
            Il2CppObject *val;
            memcpy(&key, e + g_dict.key_off, 4);
            memcpy(&val, e + g_dict.value_off, sizeof val);
            char code[16];
            fourcc_string(key, code);
            if (!first) json_raw(j, ",");
            first = 0;
            json_str(j, code);
            json_raw(j, ":");
            if (is_typed_value(val)) encode_value(j, val, depth + 1);
            else json_raw(j, "null");
        }
    }
    json_raw(j, "}}");
}

// Other interop references: {"target": "<hex>", "type": "..."}; the hex can be
// passed back to /query?target= to read the object's properties.
static int interop_target(Il2CppObject *obj, uint8_t *out, int cap) {
    Il2CppObject *list = NULL;
    if (!g_interop_pairs_off || !safe_read((uint8_t *)obj + g_interop_pairs_off, &list, sizeof list) || !list)
        return 0;
    uint8_t *arr = NULL;
    int32_t n = 0;
    if (!safe_read((uint8_t *)list + g_list_items_offset, &arr, sizeof arr) ||
        !safe_read((uint8_t *)list + g_list_size_offset, &n, sizeof n) || !arr || n <= 0 || n > 32 || 1 + 8 * n > cap)
        return 0;
    out[0] = (uint8_t)n;  // u8 pair count, then (u32 code, i32 value) pairs
    return safe_read(arr + 0x20, out + 1, 8 * (size_t)n) ? 1 + 8 * n : 0;
}

// --- value -> JSON -------------------------------------------------------------
// Database records become {"ref":"010baef4"}; lists become arrays; strings have
// FM's link markup reduced to their visible text; everything else is the
// value's own string form (numbers and booleans as JSON literals).
static void encode_value(JsonBuf *j, Il2CppObject *tv, int depth) {
    if (!tv) { json_raw(j, "null"); return; }
    const MethodInfo *get = il2cpp_object_get_virtual_method(tv, g_typed_value_get);
    Il2CppObject *obj = invoke0(get ? get : g_typed_value_get, tv);
    if (!obj) { json_raw(j, "null"); return; }
    Il2CppClass *k = il2cpp_object_get_class(obj);
    const char *kname = il2cpp_class_get_name(k);

    if (!strcmp(kname, "List`1") && depth < 8) {
        static __thread Il2CppObject *items[8][1024];
        int n = list_items(obj, items[depth], 1024);
        json_raw(j, "[");
        for (int i = 0; i < n; i++) {
            if (i) json_raw(j, ",");
            if (is_typed_value(items[depth][i])) encode_value(j, items[depth][i], depth + 1);
            else json_raw(j, "null");
        }
        json_raw(j, "]");
        return;
    }
    if (g_record_ref_class && derives_from(k, g_record_ref_class)) {
        int32_t index = 0, type = 0;
        safe_read((uint8_t *)obj + g_record_index_off, &index, 4);
        safe_read((uint8_t *)obj + g_record_type_off, &type, 4);
        char ref[16];
        snprintf(ref, sizeof ref, "%08x", ((uint32_t)type << 24) | ((uint32_t)index & 0xffffff));
        json_raw(j, "{\"ref\":");
        json_str(j, ref);
        json_raw(j, ",\"type\":");
        json_str(j, kname);
        json_raw(j, "}");
        return;
    }
    if (g_dynref_class && derives_from(k, g_dynref_class) && depth < 6) {
        encode_dynref(j, obj, depth);
        return;
    }
    if (g_interop_class && derives_from(k, g_interop_class)) {
        uint8_t t[1 + 8 * 32];
        int tl = interop_target(obj, t, sizeof t);
        if (tl) {
            char th[2 * sizeof t + 1];
            hex(t, tl, th, sizeof th);
            json_raw(j, "{\"target\":");
            json_str(j, th);
            json_raw(j, ",\"type\":");
            json_str(j, kname);
            json_raw(j, "}");
            return;
        }
    }
    static __thread char buf[16384];
    if (k == g_string_class) {
        il2cpp_utf8((Il2CppString *)obj, buf, sizeof buf);
        json_fm_text(j, buf);
        return;
    }
    typed_value_string(tv, buf, sizeof buf);
    if (!strcmp(kname, "GameDate")) {
        // Packed as year << 17 | day-of-year (1-based) << 8 | time of day.
        const char *d1 = strstr(buf, "Data1 = ");
        unsigned long v = d1 ? strtoul(d1 + 8, NULL, 10) : 0;
        int year = (int)(v >> 17), doy = (int)((v >> 8) & 0x1ff);
        if (!year || !doy) { json_raw(j, "null"); return; }
        static const int mdays[12] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
        int leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0, month = 0;
        while (month < 11 && doy > mdays[month] + (month == 1 && leap)) doy -= mdays[month] + (month == 1 && leap), month++;
        char date[24];
        snprintf(date, sizeof date, "%04d-%02d-%02d", year, month + 1, doy);
        json_str(j, date);
        return;
    }
    if (!strcmp(buf, "True")) json_raw(j, "true");
    else if (!strcmp(buf, "False")) json_raw(j, "false");
    else if (json_is_number(buf)) json_raw(j, buf);
    else if (strstr(buf, kname) == buf || strstr(buf, "Reference")) {
        json_raw(j, "{\"object\":");
        json_str(j, kname);
        json_raw(j, "}");
    } else {
        json_str(j, buf);
    }
}

// --- queries -------------------------------------------------------------------
// One query at a time: the HTTP thread fills g_q and waits; the main thread
// (Tick hook) opens the channels, the response hook collects values, and the
// Tick hook closes the channels and signals completion.
#define OUR_KEY_PREFIX 0xF27E000000000000ull
#define OUR_KEY_MASK 0xFFFF000000000000ull
#define OUR_ROOT_KEY (OUR_KEY_PREFIX | 0x0000FFFF00000000ull)
#define QUERY_MAX 2048
static int is_ours(u64 key) { return (key & OUR_KEY_MASK) == OUR_KEY_PREFIX; }

static struct {
    pthread_mutex_t lock;   // serialises callers
    pthread_mutex_t m;      // protects state below
    pthread_cond_t done_cv;
    int state;              // 0 idle, 1 pending, 2 open, 3 done
    uint32_t id;            // query number, part of each key
    uint8_t target[1 + 8 * 32];  // serialized target: u8 count + (u32, i32) pairs
    int target_len;
    uint32_t props[QUERY_MAX];
    char *values[QUERY_MAX];
    int n, opened, received;
    int frames, quiet;      // frames since open / since last response
} g_q = {.lock = PTHREAD_MUTEX_INITIALIZER, .m = PTHREAD_MUTEX_INITIALIZER, .done_cv = PTHREAD_COND_INITIALIZER};

static u64 query_key(int i) { return OUR_KEY_PREFIX | ((u64)(g_q.id & 0x7fff) << 32) | (u64)i; }

static void put_action(uint8_t *buf, int i, u64 key, u64 extra, int32_t mode) {
    uint8_t *a = buf + (size_t)g_action_size * (size_t)i;  // ChannelAction { u64 key; u64 additionalKey; i32 mode; }
    memset(a, 0, (size_t)g_action_size);
    memcpy(a, &key, 8);
    memcpy(a + 8, &extra, 8);
    memcpy(a + 16, &mode, 4);
}

static void run_actions(uint8_t *buf, int n) {
    if (n) g_orig_actions(g_plugin, (u64)buf, (u64)n, (u64)g_actions_method, 0, 0, 0, 0);
}

static void query_open_batch(void) {
    static int root_added;
    static uint8_t ch[16 * 256], acts[24 * (2 * 256 + 1)];

    int n = 0, na = 0;
    if (!root_added) { put_action(acts, na++, OUR_ROOT_KEY, 0, g_mode_add_node); root_added = 1; }
    for (; n < 256 && g_q.opened < g_q.n; n++, g_q.opened++) {
        uint32_t prop = g_q.props[g_q.opened], gen = ++g_max_gen;
        u64 key = query_key(g_q.opened);
        memcpy(ch + 16 * n, &prop, 4);  // ChannelData { u32 property; u32 generation; u64 key; }
        memcpy(ch + 16 * n + 4, &gen, 4);
        memcpy(ch + 16 * n + 8, &key, 8);
        put_action(acts, na++, key, OUR_ROOT_KEY, g_mode_add_node);  // as the UI does: register the node,
        put_action(acts, na++, key, (u64)prop, g_mode_set_property);  // then its property, then open
    }
    run_actions(acts, na);
    if (n) g_orig_open(g_plugin, (u64)g_q.target, (u64)g_q.target_len, (u64)ch, (u64)n, (u64)g_open_method, 0, 0);
}

static void query_close(void) {
    static uint8_t acts[24 * 2 * QUERY_MAX];
    int na = 0;
    for (int i = 0; i < g_q.n; i++) {
        put_action(acts, na++, query_key(i), 0, g_mode_close);
        put_action(acts, na++, query_key(i), 0, g_mode_remove_node);
    }
    run_actions(acts, na);
}

// Main thread, once per frame.
static void query_step(void) {
    pthread_mutex_lock(&g_q.m);
    if (g_q.state == 1) {
        query_open_batch();
        if (g_q.opened >= g_q.n) { g_q.state = 2; g_q.frames = g_q.quiet = 0; }
    } else if (g_q.state == 2) {
        g_q.frames++;
        g_q.quiet++;
        // Done when every property answered, or nothing new for 30 frames once
        // replies have started (properties that do not apply never answer).
        // Large batches can take a while before the first reply arrives.
        int idle = g_q.received ? g_q.quiet > 30 : g_q.frames > 240;
        if (g_q.received >= g_q.n || idle || g_q.frames > 900) {
            query_close();
            g_q.state = 3;
            pthread_cond_broadcast(&g_q.done_cv);
        }
    }
    pthread_mutex_unlock(&g_q.m);
}

// Called from the response hook for keys with our prefix.
static void query_collect(const u64 *keys, int nk, Il2CppObject **vals, int nv) {
    pthread_mutex_lock(&g_q.m);
    for (int i = 0; i < nk && i < nv; i++) {
        if (!is_ours(keys[i]) || ((keys[i] >> 32) & 0x7fff) != (g_q.id & 0x7fff)) continue;
        int idx = (int)(keys[i] & 0xffffffff);
        if (idx < 0 || idx >= g_q.n || g_q.state != 2) continue;
        JsonBuf j = {0};
        encode_value(&j, vals[i], 0);
        if (!g_q.values[idx]) g_q.received++;
        free(g_q.values[idx]);
        g_q.values[idx] = j.data;
        g_q.quiet = 0;
    }
    pthread_mutex_unlock(&g_q.m);
}

int tap_ready(void) { return g_plugin && g_guid && g_record_type && g_orig_tick; }

char *tap_query(uint32_t record, const uint32_t *props, int n) {
    uint8_t target[25];
    uint32_t pairs[6] = {CODE_011, g_record_type, CODE_GUID, g_guid, CODE_FERD, record};
    target[0] = 3;  // u8 pair count, then (code, value) pairs
    memcpy(target + 1, pairs, sizeof pairs);
    return tap_query_target(target, sizeof target, props, n);
}

char *tap_query_target(const uint8_t *target, int target_len, const uint32_t *props, int n) {
    if (!tap_ready() || n <= 0 || target_len < 1 || target_len > (int)sizeof g_q.target) return NULL;
    if (n > QUERY_MAX) n = QUERY_MAX;
    pthread_mutex_lock(&g_q.lock);
    pthread_mutex_lock(&g_q.m);
    g_q.id++;
    memcpy(g_q.target, target, (size_t)target_len);
    g_q.target_len = target_len;
    memcpy(g_q.props, props, sizeof(uint32_t) * (size_t)n);
    memset(g_q.values, 0, sizeof(char *) * (size_t)n);
    g_q.n = n;
    g_q.opened = g_q.received = 0;
    g_q.state = 1;
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += 20;
    while (g_q.state != 3)
        if (pthread_cond_timedwait(&g_q.done_cv, &g_q.m, &deadline)) break;  // game paused or minimised
    int finished = g_q.state == 3;

    JsonBuf j = {0};
    json_raw(&j, "{");
    int first = 1;
    for (int i = 0; i < n; i++) {
        if (!g_q.values[i]) continue;
        char code[16];
        fourcc_string(g_q.props[i], code);
        if (!first) json_raw(&j, ",");
        first = 0;
        json_str(&j, code);
        json_raw(&j, ":");
        json_raw(&j, g_q.values[i]);
        free(g_q.values[i]);
        g_q.values[i] = NULL;
    }
    json_raw(&j, "}");
    g_q.state = 0;  // an unfinished query's channels stay open; harmless, they only report changes
    pthread_mutex_unlock(&g_q.m);
    pthread_mutex_unlock(&g_q.lock);
    if (!finished) log_line("query: timed out (game not ticking?)");
    return j.data;
}

int tap_seen(uint32_t *out, int cap) {
    pthread_mutex_lock(&g_seen_lock);
    int n = g_seen_count < cap ? g_seen_count : cap;
    memcpy(out, g_seen + (g_seen_count - n), sizeof(uint32_t) * (size_t)n);
    pthread_mutex_unlock(&g_seen_lock);
    return n;
}

uint32_t tap_guid(void) { return g_guid; }

static void note_seen(uint32_t record) {
    pthread_mutex_lock(&g_seen_lock);
    for (int i = 0; i < g_seen_count; i++)
        if (g_seen[i] == record) {  // move to the end (most recent)
            memmove(g_seen + i, g_seen + i + 1, sizeof(uint32_t) * (size_t)(g_seen_count - i - 1));
            g_seen_count--;
            break;
        }
    if (g_seen_count == SEEN_MAX) {
        memmove(g_seen, g_seen + 1, sizeof(uint32_t) * (SEEN_MAX - 1));
        g_seen_count--;
    }
    g_seen[g_seen_count++] = record;
    pthread_mutex_unlock(&g_seen_lock);
}

// --- hooks ---------------------------------------------------------------------
// SendDataResponse(Span<ulong> keys, Span<uint> generations, Span<byte> data)
static void hook_response(u64 a, u64 b, u64 c, u64 d, u64 e, u64 f, u64 g, u64 h) {
    u64 x[8] = {a, b, c, d, e, f, g, h};
    const void *kp, *gp, *bp;
    int kn, gn, bn;
    span_arg(x, 1, &kp, &kn);
    static u64 keys[2048];
    int nk = span_copy(kp, kn, 8, keys, sizeof keys);
    int ours = 0;
    for (int i = 0; i < nk; i++) ours += is_ours(keys[i]);

    // Responses to our own channels must not reach the UI, which does not know
    // their keys: detach the UI callback for that call only.
    if (ours && ours == nk && g_receiver_callback_offset) {
        void **cb = (void **)((uint8_t *)a + g_receiver_callback_offset);
        void *saved = *cb;
        *cb = NULL;
        g_orig_response(a, b, c, d, e, f, g, h);
        *cb = saved;
    } else {
        g_orig_response(a, b, c, d, e, f, g, h);
    }

    static Il2CppObject *vals[2048];
    if (ours) {
        static __thread int collecting;  // replies may arrive inside our own Tick work
        if (!collecting) {
            collecting = 1;
            int nv = decoded_values((Il2CppObject *)a, vals, 2048);
            query_collect(keys, nk, vals, nv);
            collecting = 0;
        }
        if (ours == nk) return;  // tap.log is for UI traffic only
    }
    if (t_busy || !g_tap) return;
    t_busy = 1;
    span_arg(x, 3, &gp, &gn);
    span_arg(x, 5, &bp, &bn);
    static uint32_t gens[2048];
    int ng = span_copy(gp, gn, 4, gens, sizeof gens);
    int nv = decoded_values((Il2CppObject *)a, vals, 2048);
    tap_printf("R keys=%d bytes=%d values=%d\n", kn, bn, nv);
    char val[512], cc[16];
    for (int i = 0; i < nk; i++) {
        fourcc_string(key_get(keys[i]), cc);
        if (i < nv) typed_value_string(vals[i], val, sizeof val);
        else strlcpy(val, "?", sizeof val);
        tap_printf("  %016llx %s gen=%u = %s\n", keys[i], cc, i < ng ? gens[i] : 0, val);
    }
    fflush(g_tap);
    t_busy = 0;
}

// SendEvent(uint id, Span<byte> data)
static void hook_event(u64 a, u64 b, u64 c, u64 d, u64 e, u64 f, u64 g, u64 h) {
    g_orig_event(a, b, c, d, e, f, g, h);
    if (t_busy || !g_tap) return;
    t_busy = 1;
    u64 x[8] = {a, b, c, d, e, f, g, h};
    const void *bp;
    int bn;
    span_arg(x, 2, &bp, &bn);
    uint8_t raw[64];
    char rh[140];
    hex(raw, span_copy(bp, bn, 1, raw, sizeof raw), rh, sizeof rh);
    tap_printf("E id=%u bytes=%d raw=%s\n", (uint32_t)b, bn, rh);
    fflush(g_tap);
    t_busy = 0;
}

// OpenChannels(Span<byte> target, Span<ChannelData> channels)
static void hook_open(u64 a, u64 b, u64 c, u64 d, u64 e, u64 f, u64 g, u64 h) {
    if (!t_busy) {
        t_busy = 1;
        if (!g_plugin) g_plugin = a;
        u64 x[8] = {a, b, c, d, e, f, g, h};
        const void *tp, *cp;
        int tn, cn;
        span_arg(x, 1, &tp, &tn);
        span_arg(x, 3, &cp, &cn);
        uint8_t target[96];
        static uint8_t ch[16 * 4096];
        int tl = span_copy(tp, tn, 1, target, sizeof target);
        int n = span_copy(cp, cn, 16, ch, sizeof ch);
        // target: u8 pair count, then (u32 code, u32 value) pairs
        int pairs = tl > 0 ? target[0] : 0;
        uint32_t record = 0;
        for (int p = 0; p < pairs && 9 + 8 * p <= tl; p++) {
            uint32_t code, val;
            memcpy(&code, target + 1 + 8 * p, 4);
            memcpy(&val, target + 5 + 8 * p, 4);
            if (code == CODE_GUID && val && val != g_guid) {  // a career was (re)loaded
                g_guid = val;
                pthread_mutex_lock(&g_open_lock);
                memset(g_open, 0, sizeof g_open);
                pthread_mutex_unlock(&g_open_lock);
            }
            if (code == CODE_011 && pairs == 3) g_record_type = val;
            if (code == CODE_FERD) { note_seen(val); record = val; }
        }
        open_add(record, n);
        char th[200], cc[16];
        hex(target, tl, th, sizeof th);
        tap_printf("O target(%d)=%s channels=%d\n", tn, th, cn);
        for (int i = 0; i < n; i++) {
            uint32_t prop, gen;
            u64 key;
            memcpy(&prop, ch + 16 * i, 4);
            memcpy(&gen, ch + 16 * i + 4, 4);
            memcpy(&key, ch + 16 * i + 8, 8);
            key_put(key, prop, record);
            if (gen > g_max_gen && gen < 0x40000000u) g_max_gen = gen;
            fourcc_string(prop, cc);
            tap_printf("  %016llx %s gen=%u\n", key, cc, gen);
        }
        if (g_tap) fflush(g_tap);
        t_busy = 0;
    }
    g_orig_open(a, b, c, d, e, f, g, h);
}

// PerformChannelActions(Span<ChannelAction>): counts the UI closing channels.
static void hook_actions(u64 a, u64 b, u64 c, u64 d, u64 e, u64 f, u64 g, u64 h) {
    u64 x[8] = {a, b, c, d, e, f, g, h};
    const void *ap;
    int an;
    span_arg(x, 1, &ap, &an);
    static uint8_t acts[24 * 4096];
    int n = span_copy(ap, an, 24, acts, sizeof acts);
    for (int i = 0; i < n; i++) {
        int32_t mode;
        u64 key;
        memcpy(&key, acts + 24 * i, 8);
        memcpy(&mode, acts + 24 * i + 16, 4);
        if (mode == g_mode_close && !is_ours(key)) open_add(key_record(key), -1);
    }
    g_orig_actions(a, b, c, d, e, f, g, h);
}

static void hook_tick(u64 a, u64 b, u64 c, u64 d, u64 e, u64 f, u64 g, u64 h) {
    g_orig_tick(a, b, c, d, e, f, g, h);
    if (!g_plugin) g_plugin = a;
    query_step();
}

// --- installation --------------------------------------------------------------
// Replaces every vtable entry {methodPtr, method == m} in klass (or, with no
// hook, only looks up the original pointer). Returns the number of slots.
static int patch_vtable(Il2CppClass *klass, const MethodInfo *m, void *hook, Fn8 *orig) {
    void *const *mi = (void *const *)m;
    int patched = 0;
    for (size_t off = 0; off < 0x4000; off += 8) {
        void *pair[2];
        if (!safe_read((uint8_t *)klass + off, pair, sizeof pair)) break;
        if (pair[1] != m || (pair[0] != mi[0] && pair[0] != mi[1])) continue;
        void **slot = (void **)((uint8_t *)klass + off);
        if (!*orig) *orig = (Fn8)pair[0];
        if (!hook) return 1;
        mach_vm_protect(mach_task_self(), (mach_vm_address_t)slot, 8, false, VM_PROT_READ | VM_PROT_WRITE);
        *slot = hook;
        patched++;
    }
    log_line("tap: %s.%s: %d slot(s)", il2cpp_class_get_name(klass), il2cpp_method_get_name(m), patched);
    return patched;
}

static const MethodInfo *method(Il2CppClass *k, const char *name, int argc) {
    const MethodInfo *m = k ? il2cpp_class_get_method_from_name(k, name, argc) : NULL;
    if (!m) log_line("tap: method %s.%s/%d not found", k ? il2cpp_class_get_name(k) : "?", name, argc);
    return m;
}

// The non-generic overload of a method (e.g. TypedValue.Get() returning object).
static const MethodInfo *method_returning(Il2CppClass *k, const char *name, const char *ret) {
    void *it = NULL;
    const MethodInfo *m;
    char tn[256];
    while (k && (m = il2cpp_class_get_methods(k, &it))) {
        if (strcmp(il2cpp_method_get_name(m), name) || il2cpp_method_get_param_count(m)) continue;
        il2cpp_type_name(il2cpp_method_get_return_type(m), tn, sizeof tn);
        if (!strcmp(tn, ret)) return m;
    }
    log_line("tap: %s %s() not found", ret, name);
    return NULL;
}

static size_t field_offset(Il2CppClass *k, const char *name) {
    FieldInfo *f = k ? il2cpp_class_get_field_from_name(k, name) : NULL;
    return f ? il2cpp_field_get_offset(f) : 0;
}

static int32_t enum_value(Il2CppClass *k, const char *name) {
    int32_t v = -1;
    FieldInfo *f = k ? il2cpp_class_get_field_from_name(k, name) : NULL;
    if (f) il2cpp_field_static_get_value(f, &v);
    return v;
}

static int learn_span_layout(const MethodInfo *m) {
    Il2CppClass *k = il2cpp_class_from_type(il2cpp_method_get_param(m, 0));
    int size = il2cpp_class_value_size(k, NULL);
    void *it = NULL;
    FieldInfo *f;
    while ((f = il2cpp_class_get_fields(k, &it))) {
        if (il2cpp_field_get_flags(f) & FIELD_ATTRIBUTE_STATIC) continue;
        const char *n = il2cpp_field_get_name(f);
        int off = (int)il2cpp_field_get_offset(f) - 0x10;  // value-type offsets include the object header
        if (!strcmp(n, "_reference") || !strcmp(n, "_pointer")) g_span.ptr_off = off;
        else if (!strcmp(n, "_length")) g_span.len_off = off;
    }
    log_line("tap: Span size %d (pointer @%d, length @%d)", size, g_span.ptr_off, g_span.len_off);
    return size == 16 && g_span.ptr_off >= 0 && g_span.len_off >= 0;
}

void tap_install(void) {
    Il2CppClass *receiver = il2cpp_find_class("FM.GamePlugin.dll", "FM.GamePlugin", "GameReceiver");
    Il2CppClass *wrapper = il2cpp_find_class("FM.GamePlugin.dll", "", "FM_GamePlugin_IGamePlugin_NativeWrapper");
    Il2CppClass *typed_value = il2cpp_find_class("SI.Core.dll", "SI.Core", "TypedValue");
    g_typed_value_class = typed_value;
    Il2CppClass *modes = il2cpp_find_class("FM.GamePlugin.dll", "FM.GamePlugin", "ChannelActionMode");
    Il2CppClass *action = il2cpp_find_class("FM.GamePlugin.dll", "FM.GamePlugin", "ChannelAction");
    g_record_ref_class = il2cpp_find_class("FM.UI.dll", "FM.UI", "DatabaseRecordReference");
    g_string_class = il2cpp_find_class("mscorlib.dll", "System", "String");
    g_dynref_class = il2cpp_find_class("SI.Bindable.dll", "SI.Bindable", "DynamicReference");
    g_interop_class = il2cpp_find_class("SI.Interop.dll", "SI.Interop", "InteropReference");
    g_dynref_get_id = g_dynref_class ? il2cpp_class_get_method_from_name(g_dynref_class, "get_ID", 0) : NULL;
    g_interop_pairs_off = field_offset(g_interop_class, "m_id");
    log_line("tap: DynamicReference %s, InteropReference pairs@0x%zx", g_dynref_class ? "found" : "missing", g_interop_pairs_off);

    const MethodInfo *response = method(receiver, "SendDataResponse", 3);
    const MethodInfo *event = method(receiver, "SendEvent", 2);
    const MethodInfo *open = method(wrapper, "OpenChannels", 2);
    const MethodInfo *tick = method(wrapper, "Tick", 0);
    const MethodInfo *actions = method(wrapper, "PerformChannelActions", 1);
    g_typed_value_as_string = method(typed_value, "AsString", 0);
    g_typed_value_get = method_returning(typed_value, "Get", "System.Object");
    g_open_method = open;
    g_actions_method = actions;
    g_receiver_list_offset = field_offset(receiver, "m_resusableTypedList");
    g_receiver_callback_offset = field_offset(receiver, "m_onChannelDataChange");
    g_record_index_off = field_offset(g_record_ref_class, "m_index");
    g_record_type_off = field_offset(g_record_ref_class, "<Type>k__BackingField");
    g_mode_add_node = enum_value(modes, "AddNode");
    g_mode_remove_node = enum_value(modes, "RemoveNode");
    g_mode_close = enum_value(modes, "CloseChannel");
    g_mode_set_property = enum_value(modes, "SetPropertyID");
    g_action_size = action ? il2cpp_class_value_size(action, NULL) : 0;

    if (!response || !event || !open || !tick || !actions || !g_typed_value_as_string || !g_typed_value_get ||
        !g_receiver_list_offset || !g_receiver_callback_offset || !g_record_index_off || !g_record_type_off ||
        g_mode_add_node < 0 || g_mode_remove_node < 0 || g_mode_close < 0 || g_mode_set_property < 0 ||
        g_action_size != 24 || !g_string_class || !learn_span_layout(response)) {
        log_line("tap: not installed (unexpected game layout; see lines above)");
        return;
    }

    g_tap = data_file("tap.log", "w");
    int n = patch_vtable(receiver, response, (void *)hook_response, &g_orig_response);
    n += patch_vtable(receiver, event, (void *)hook_event, &g_orig_event);
    n += patch_vtable(wrapper, open, (void *)hook_open, &g_orig_open);
    n += patch_vtable(wrapper, actions, (void *)hook_actions, &g_orig_actions);
    n += patch_vtable(wrapper, tick, (void *)hook_tick, &g_orig_tick);
    log_line("tap: installed (%d vtable slots)", n);
}

#else
void tap_install(void) { log_line("tap: only supported on Apple Silicon"); }
int tap_ready(void) { return 0; }
char *tap_query(uint32_t record, const uint32_t *props, int n) { (void)record; (void)props; (void)n; return NULL; }
char *tap_query_target(const uint8_t *t, int tl, const uint32_t *props, int n) { (void)t; (void)tl; (void)props; (void)n; return NULL; }
int tap_seen(uint32_t *out, int cap) { (void)out; (void)cap; return 0; }
uint32_t tap_focus(uint8_t kind) { (void)kind; return 0; }
uint32_t tap_guid(void) { return 0; }
void fourcc_string(uint32_t v, char out[16]) { snprintf(out, 16, "#%08x", v); }
uint32_t fourcc_parse(const char *s) { return (uint32_t)strtoul(s + 1, NULL, 16); }
#endif
