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
#include <setjmp.h>
#include <signal.h>
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

static Fn8 g_orig_response, g_orig_event, g_orig_open, g_orig_tick, g_orig_actions, g_orig_update, g_orig_send;
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
static const MethodInfo *g_open_method, *g_actions_method, *g_send_method, *g_update_method;
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
    int hold;               // keep the channels open after the query (released by tap_release)
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

// Held queries: channels kept open (watched) until released, as the UI keeps the
// dialogs it shows open. FM only acts on some events (e.g. a conversation choice)
// while someone watches the dialog.
#define HOLDS 16
static struct { uint32_t id; int n; int release; } g_holds[HOLDS];

static void hold_release_step(void) {
    static uint8_t acts[24 * 2 * 256];
    for (int h = 0; h < HOLDS; h++) {
        if (!g_holds[h].id || !g_holds[h].release) continue;
        int na = 0;
        for (int i = 0; i < g_holds[h].n && i < 256; i++) {
            u64 key = OUR_KEY_PREFIX | ((u64)(g_holds[h].id & 0x7fff) << 32) | (u64)i;
            put_action(acts, na++, key, 0, g_mode_close);
            put_action(acts, na++, key, 0, g_mode_remove_node);
        }
        run_actions(acts, na);
        g_holds[h].id = 0;
        g_holds[h].release = 0;
    }
}

int tap_release(uint32_t id) {
    pthread_mutex_lock(&g_q.m);
    int found = 0;
    for (int h = 0; h < HOLDS; h++)
        if (g_holds[h].id == id) { g_holds[h].release = 1; found = 1; }
    pthread_mutex_unlock(&g_q.m);
    return found;
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
            int kept = 0;
            if (g_q.hold)
                for (int h = 0; h < HOLDS && !kept; h++)
                    if (!g_holds[h].id) { g_holds[h].id = g_q.id; g_holds[h].n = g_q.n; g_holds[h].release = 0; kept = 1; }
            if (!kept) query_close();
            g_q.state = 3;
            pthread_cond_broadcast(&g_q.done_cv);
        }
    }
    hold_release_step();
    pthread_mutex_unlock(&g_q.m);
}

// --- fault guard ------------------------------------------------------------------
// Reading FM's values goes through objects we do not own. If one turns out to be
// unreadable, the fault must cost that one value, never the game: while a value
// is encoded, SIGSEGV/SIGBUS jump back and the value becomes null. Outside the
// guarded section the previous handlers (Unity's) run as before.
static struct sigaction g_old_segv, g_old_bus;
static __thread sigjmp_buf *t_guard;
static volatile int g_faults;

static void fault_handler(int sig, siginfo_t *info, void *ctx) {
    if (t_guard) {
        sigjmp_buf *b = t_guard;
        t_guard = NULL;
        g_faults++;
        siglongjmp(*b, 1);
    }
    struct sigaction *old = sig == SIGBUS ? &g_old_bus : &g_old_segv;
    if (old->sa_flags & SA_SIGINFO) {
        if (old->sa_sigaction) { old->sa_sigaction(sig, info, ctx); return; }
    } else if (old->sa_handler != SIG_DFL && old->sa_handler != SIG_IGN) {
        old->sa_handler(sig);
        return;
    }
    sigaction(sig, old, NULL);  // default action: let the fault happen again unhandled
}

static void install_fault_guard(void) {
    struct sigaction sa = {0};
    sa.sa_sigaction = fault_handler;
    sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, &g_old_segv);
    sigaction(SIGBUS, &sa, &g_old_bus);
}

static char *encode_guarded(Il2CppObject *v) {
    static __thread JsonBuf j;
    sigjmp_buf here;
    memset(&j, 0, sizeof j);
    if (sigsetjmp(here, 1)) {  // a fault while encoding: drop this value
        free(j.data);
        memset(&j, 0, sizeof j);
        log_line("tap: unreadable value skipped (%d so far)", g_faults);
        return strdup("null");
    }
    t_guard = &here;
    encode_value(&j, v, 0);
    t_guard = NULL;
    return j.data ? j.data : strdup("null");
}

// The value's own text, for the log; a fault costs only this value.
static void string_guarded(Il2CppObject *v, char *buf, size_t n) {
    sigjmp_buf here;
    if (sigsetjmp(here, 1)) {
        strlcpy(buf, "<unreadable>", n);
        return;
    }
    t_guard = &here;
    if (is_typed_value(v)) typed_value_string(v, buf, n);
    else strlcpy(buf, "?", n);
    t_guard = NULL;
}

// Called from the response hook for keys with our prefix.
static void query_collect(const u64 *keys, int nk, Il2CppObject **vals, int nv) {
    pthread_mutex_lock(&g_q.m);
    for (int i = 0; i < nk && i < nv; i++) {
        if (!is_ours(keys[i]) || ((keys[i] >> 32) & 0x7fff) != (g_q.id & 0x7fff)) continue;
        int idx = (int)(keys[i] & 0xffffffff);
        if (idx < 0 || idx >= g_q.n || g_q.state != 2) continue;
        char *json = encode_guarded(vals[i]);
        if (!g_q.values[idx]) g_q.received++;
        free(g_q.values[idx]);
        g_q.values[idx] = json;
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

static char *query_target_impl(const uint8_t *target, int target_len, const uint32_t *props, int n, int hold,
                               uint32_t *hold_id);

char *tap_query_target(const uint8_t *target, int target_len, const uint32_t *props, int n) {
    return query_target_impl(target, target_len, props, n, 0, NULL);
}

char *tap_hold_target(const uint8_t *target, int target_len, const uint32_t *props, int n, uint32_t *hold_id) {
    return query_target_impl(target, target_len, props, n, 1, hold_id);
}

static char *query_target_impl(const uint8_t *target, int target_len, const uint32_t *props, int n, int hold,
                               uint32_t *hold_id) {
    if (!tap_ready() || n <= 0 || target_len < 1 || target_len > (int)sizeof g_q.target) return NULL;
    if (hold && n > 256) n = 256;
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
    g_q.hold = hold;
    if (hold_id) *hold_id = g_q.id;
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
    g_q.hold = 0;
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

// --- UI -> engine events and recent targets (for the world engine) -------------
#define EVENT_RING 256
static struct { uint64_t seq; uint32_t id; int tlen, dlen; uint8_t target[96]; uint8_t data[1024]; } g_events[EVENT_RING];
static uint64_t g_event_seq;
static pthread_mutex_t g_event_lock = PTHREAD_MUTEX_INITIALIZER;
#define TYPE_SLOTS 256
static struct { int len; uint8_t bytes[96]; } g_last_target[TYPE_SLOTS];

static void remember_event(uint32_t id, const uint8_t *t, int tl, const uint8_t *d, int dl) {
    pthread_mutex_lock(&g_event_lock);
    int slot = (int)(g_event_seq % EVENT_RING);
    g_events[slot].seq = ++g_event_seq;
    g_events[slot].id = id;
    g_events[slot].tlen = tl < 96 ? tl : 96;
    g_events[slot].dlen = dl < 1024 ? dl : 1024;
    memcpy(g_events[slot].target, t, (size_t)g_events[slot].tlen);
    memcpy(g_events[slot].data, d, (size_t)g_events[slot].dlen);
    pthread_mutex_unlock(&g_event_lock);
}

static void remember_target(const uint8_t *t, int tl) {
    if (tl < 9 || tl > 96) return;
    uint32_t code, type;
    memcpy(&code, t + 1, 4);
    memcpy(&type, t + 5, 4);
    if (code != CODE_011 || type >= TYPE_SLOTS) return;
    pthread_mutex_lock(&g_event_lock);
    g_last_target[type].len = tl;
    memcpy(g_last_target[type].bytes, t, (size_t)tl);
    pthread_mutex_unlock(&g_event_lock);
}

char *tap_events_json(uint64_t since) {
    JsonBuf j = {0};
    char buf[3000];
    json_raw(&j, "[");
    int first = 1;
    pthread_mutex_lock(&g_event_lock);
    uint64_t oldest = g_event_seq > EVENT_RING ? g_event_seq - EVENT_RING + 1 : 1;
    for (uint64_t q = (since + 1 > oldest ? since + 1 : oldest); q <= g_event_seq; q++) {
        int slot = (int)((q - 1) % EVENT_RING);
        char th[200], dh[2100];
        hex(g_events[slot].target, g_events[slot].tlen, th, sizeof th);
        hex(g_events[slot].data, g_events[slot].dlen, dh, sizeof dh);
        snprintf(buf, sizeof buf, "%s{\"seq\":%llu,\"id\":%u,\"target\":\"%s\",\"data\":\"%s\"}", first ? "" : ",",
                 (unsigned long long)g_events[slot].seq, g_events[slot].id, th, dh);
        json_raw(&j, buf);
        first = 0;
    }
    pthread_mutex_unlock(&g_event_lock);
    json_raw(&j, "]");
    return j.data;
}

char *tap_last_target(uint32_t type) {
    if (type >= TYPE_SLOTS) return NULL;
    char th[200] = "";
    pthread_mutex_lock(&g_event_lock);
    hex(g_last_target[type].bytes, g_last_target[type].len, th, sizeof th);
    pthread_mutex_unlock(&g_event_lock);
    return th[0] ? strdup(th) : NULL;
}

// --- the UI's channel tree (for the In-Game Editor) -------------------------------
// The UI builds its bindings as a tree of channel nodes: AddNode(key, parent),
// SetPropertyID(key, prop). An editor field is a node whose children carry its
// label (eman) and value (lvtp). We keep the UI's nodes and their latest values,
// so a field can be found by its label and written on the UI's own channel,
// exactly as the editor does when the player types a value. Never opens anything.
#define NODES 16384
static struct node { u64 key, parent; uint32_t prop; int live; char value[96]; } g_nodes[NODES];
static pthread_mutex_t g_node_lock = PTHREAD_MUTEX_INITIALIZER;
#define CODE_EMAN 0x6e616d65u
#define CODE_LVTP 0x7074766cu

static struct node *node_slot(u64 key, int create) {
    size_t i = (size_t)(key ^ (key >> 29)) % NODES;
    for (int n = 0; n < NODES; n++, i = (i + 1) % NODES) {
        if (g_nodes[i].key == key) return &g_nodes[i];
        if (!g_nodes[i].key || (!g_nodes[i].live && create)) {
            if (!create) return NULL;
            memset(&g_nodes[i], 0, sizeof g_nodes[i]);
            g_nodes[i].key = key;
            return &g_nodes[i];
        }
    }
    return NULL;
}

static void node_action(u64 key, int32_t mode, u64 extra) {
    if (is_ours(key) || !key) return;
    pthread_mutex_lock(&g_node_lock);
    struct node *n = node_slot(key, mode == g_mode_add_node || mode == g_mode_set_property);
    if (n) {
        if (mode == g_mode_add_node) { n->parent = extra; n->live = 1; }
        else if (mode == g_mode_set_property) { n->prop = (uint32_t)extra; n->live = 1; }
        else if (mode == g_mode_remove_node || mode == g_mode_close) { n->live = 0; }
    }
    pthread_mutex_unlock(&g_node_lock);
}

static void node_value(u64 key, const char *value) {
    pthread_mutex_lock(&g_node_lock);
    struct node *n = node_slot(key, 0);
    if (n && n->live && (n->prop == CODE_EMAN || n->prop == CODE_LVTP)) strlcpy(n->value, value, sizeof n->value);
    pthread_mutex_unlock(&g_node_lock);
}

char *tap_editor_json(void) {
    JsonBuf j = {0};
    json_raw(&j, "[");
    int first = 1;
    static struct node *values[4096];
    pthread_mutex_lock(&g_node_lock);
    int nvalues = 0;
    for (int i = 0; i < NODES && nvalues < 4096; i++)
        if (g_nodes[i].key && g_nodes[i].live && g_nodes[i].prop == CODE_LVTP) values[nvalues++] = &g_nodes[i];
    for (int i = 0; i < NODES; i++) {
        struct node *label = &g_nodes[i];
        if (!label->key || !label->live || label->prop != CODE_EMAN || !label->value[0]) continue;
        for (int k = 0; k < nvalues; k++) {
            struct node *v = values[k];
            if (v->parent != label->parent) continue;
            char key[24];
            snprintf(key, sizeof key, "%016llx", (unsigned long long)v->key);
            json_raw(&j, first ? "{\"label\":" : ",{\"label\":");
            json_str(&j, label->value);
            json_raw(&j, ",\"value\":");
            json_str(&j, v->value);
            json_raw(&j, ",\"key\":");
            json_str(&j, key);
            json_raw(&j, "}");
            first = 0;
        }
    }
    pthread_mutex_unlock(&g_node_lock);
    json_raw(&j, "]");
    return j.data;
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
        if (i < nv) string_guarded(vals[i], val, sizeof val);
        else strlcpy(val, "?", sizeof val);
        node_value(keys[i], val);
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
        remember_target(target, tl);
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
            if (code == CODE_FERD) { note_seen(val); record = val; }
        }
        // The record type comes only from database record targets (' 011', Guid, ferd):
        // other 3-pair targets (e.g. action objects: ' 011', Guid, dica) must not change it.
        if (pairs == 3 && tl >= 25) {
            uint32_t c0, v0, c2;
            memcpy(&c0, target + 1, 4);
            memcpy(&v0, target + 5, 4);
            memcpy(&c2, target + 17, 4);
            if (c0 == CODE_011 && c2 == CODE_FERD) g_record_type = v0;
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
        u64 extra;
        memcpy(&extra, acts + 24 * i + 8, 8);
        node_action(key, mode, extra);
    }
    if (g_tap && !t_busy && n) {  // record the UI's own actions (for learning its protocols)
        t_busy = 1;
        int logged = 0;
        for (int i = 0; i < n && logged < 200; i++) {
            int32_t mode;
            u64 key, extra;
            memcpy(&key, acts + 24 * i, 8);
            memcpy(&extra, acts + 24 * i + 8, 8);
            memcpy(&mode, acts + 24 * i + 16, 4);
            if (is_ours(key) || mode == g_mode_close) continue;
            char cc[16];
            fourcc_string(key_get(key), cc);
            if (!logged++) tap_printf("A actions=%d\n", n);
            tap_printf("  %016llx %s mode=%d extra=%016llx\n", key, cc, mode, extra);
        }
        if (logged) fflush(g_tap);
        t_busy = 0;
    }
    g_orig_actions(a, b, c, d, e, f, g, h);
}

// IGamePlugin.UpdateChannelData(ChannelAction action, Span<byte> data): the UI
// writing a value (e.g. choosing an option). ChannelAction (24 bytes) arrives
// by pointer in x1; the span in x2/x3.
static void hook_update(u64 a, u64 b, u64 c, u64 d, u64 e, u64 f, u64 g, u64 h) {
    if (g_tap && !t_busy) {
        t_busy = 1;
        uint8_t act[24];
        u64 key = 0, extra = 0;
        int32_t mode = -1;
        if (safe_read((const void *)b, act, sizeof act)) {
            memcpy(&key, act, 8);
            memcpy(&extra, act + 8, 8);
            memcpy(&mode, act + 16, 4);
        }
        u64 x[8] = {a, b, c, d, e, f, g, h};
        const void *dp;
        int dn;
        span_arg(x, 2, &dp, &dn);
        uint8_t raw[160];
        char rh[330], cc[16];
        hex(raw, span_copy(dp, dn, 1, raw, sizeof raw), rh, sizeof rh);
        fourcc_string(key_get(key), cc);
        tap_printf("U key=%016llx %s mode=%d extra=%016llx bytes=%d raw=%s\n", key, cc, mode, extra, dn, rh);
        fflush(g_tap);
        t_busy = 0;
    }
    g_orig_update(a, b, c, d, e, f, g, h);
}

// IGamePlugin.SendEvent(uint id, Span<byte> target, Span<byte> data): UI -> engine events.
static void hook_send(u64 a, u64 b, u64 c, u64 d, u64 e, u64 f, u64 g, u64 h) {
    {
        u64 x[8] = {a, b, c, d, e, f, g, h};
        const void *tp, *dp;
        int tn, dn;
        span_arg(x, 2, &tp, &tn);
        span_arg(x, 4, &dp, &dn);
        static uint8_t et[96], ed[1024];
        int etl = span_copy(tp, tn, 1, et, sizeof et), edl = span_copy(dp, dn, 1, ed, sizeof ed);
        remember_event((uint32_t)b, et, etl, ed, edl);
    }
    if (g_tap && !t_busy) {
        t_busy = 1;
        u64 x[8] = {a, b, c, d, e, f, g, h};
        const void *tp, *dp;
        int tn, dn;
        span_arg(x, 2, &tp, &tn);
        span_arg(x, 4, &dp, &dn);
        uint8_t t[96], raw[160];
        char th[200], rh[330];
        hex(t, span_copy(tp, tn, 1, t, sizeof t), th, sizeof th);
        hex(raw, span_copy(dp, dn, 1, raw, sizeof raw), rh, sizeof rh);
        tap_printf("S id=%u target(%d)=%s data(%d)=%s\n", (uint32_t)b, tn, th, dn, rh);
        fflush(g_tap);
        t_busy = 0;
    }
    g_orig_send(a, b, c, d, e, f, g, h);
}

// --- our own events (world engine -> FM), sent on the main thread -------------------
// Exactly what the interface does when the player clicks: IGamePlugin.SendEvent with
// the same id, target and data. One at a time; the caller waits for the result.
static struct {
    pthread_mutex_t lock, m;
    pthread_cond_t done;
    int pending, finished;
    uint32_t id;
    int tlen, dlen;
    uint8_t target[96], data[1024];
} g_out = {PTHREAD_MUTEX_INITIALIZER, PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER};

static void send_step(void) {
    pthread_mutex_lock(&g_out.m);
    if (g_out.pending && g_orig_send && g_send_method && g_plugin) {
        u64 x[8] = {g_plugin, g_out.id, 0, 0, 0, 0, (u64)g_send_method, 0};
        uint8_t span[2][16] = {{0}};
        u64 tp = (u64)g_out.target, dp = (u64)g_out.data;
        int32_t tl = g_out.tlen, dl = g_out.dlen;
        memcpy(span[0] + g_span.ptr_off, &tp, 8); memcpy(span[0] + g_span.len_off, &tl, 4);
        memcpy(span[1] + g_span.ptr_off, &dp, 8); memcpy(span[1] + g_span.len_off, &dl, 4);
        memcpy(&x[2], span[0], 16);
        memcpy(&x[4], span[1], 16);
        g_orig_send(x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7]);
        if (g_tap) { tap_printf("S(ours) id=%u tlen=%d dlen=%d\n", g_out.id, tl, dl); fflush(g_tap); }
        g_out.pending = 0;
        g_out.finished = 1;
        pthread_cond_broadcast(&g_out.done);
    }
    pthread_mutex_unlock(&g_out.m);
}

// Writing a value on one of the UI's own channels (UpdateChannelData), main thread.
static struct {
    pthread_mutex_t lock, m;
    pthread_cond_t done;
    int pending, finished;
    u64 key;
    int dlen;
    uint8_t data[1024];
} g_upd = {PTHREAD_MUTEX_INITIALIZER, PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER, 0, 0, 0, 0, {0}};

static void update_step(void) {
    pthread_mutex_lock(&g_upd.m);
    if (g_upd.pending && g_orig_update && g_update_method && g_plugin) {
        uint8_t action[24] = {0};
        int32_t mode = 4;  // UpdateChannelData
        memcpy(action, &g_upd.key, 8);
        memcpy(action + 16, &mode, 4);
        uint8_t span[16] = {0};
        u64 dp = (u64)g_upd.data;
        int32_t dl = g_upd.dlen;
        memcpy(span + g_span.ptr_off, &dp, 8);
        memcpy(span + g_span.len_off, &dl, 4);
        u64 s0, s1;
        memcpy(&s0, span, 8);
        memcpy(&s1, span + 8, 8);
        g_orig_update(g_plugin, (u64)action, s0, s1, (u64)g_update_method, 0, 0, 0);
        if (g_tap) { tap_printf("U(ours) key=%016llx bytes=%d\n", (unsigned long long)g_upd.key, dl); fflush(g_tap); }
        g_upd.pending = 0;
        g_upd.finished = 1;
        pthread_cond_broadcast(&g_upd.done);
    }
    pthread_mutex_unlock(&g_upd.m);
}

int tap_editor_set(uint64_t key, const uint8_t *data, int dl) {
    if (!tap_ready() || !g_orig_update || !g_update_method || dl <= 0 || dl > 1024) return 0;
    pthread_mutex_lock(&g_node_lock);  // only a live editor value channel the UI opened
    struct node *n = node_slot(key, 0);
    int ok_key = n && n->live && n->prop == CODE_LVTP;
    pthread_mutex_unlock(&g_node_lock);
    if (!ok_key) return -1;
    pthread_mutex_lock(&g_upd.lock);
    pthread_mutex_lock(&g_upd.m);
    g_upd.key = key;
    g_upd.dlen = dl;
    memcpy(g_upd.data, data, (size_t)dl);
    g_upd.pending = 1;
    g_upd.finished = 0;
    struct timespec until;
    clock_gettime(CLOCK_REALTIME, &until);
    until.tv_sec += 5;
    while (!g_upd.finished && pthread_cond_timedwait(&g_upd.done, &g_upd.m, &until) == 0) {}
    int ok = g_upd.finished;
    g_upd.pending = 0;
    pthread_mutex_unlock(&g_upd.m);
    pthread_mutex_unlock(&g_upd.lock);
    return ok;
}

int tap_send_event(uint32_t id, const uint8_t *target, int tl, const uint8_t *data, int dl) {
    if (!tap_ready() || !g_orig_send || !g_send_method || tl <= 0 || tl > 96 || dl < 0 || dl > 1024) return 0;
    pthread_mutex_lock(&g_out.lock);
    pthread_mutex_lock(&g_out.m);
    g_out.id = id;
    g_out.tlen = tl; memcpy(g_out.target, target, (size_t)tl);
    g_out.dlen = dl; memcpy(g_out.data, data, (size_t)dl);
    g_out.pending = 1; g_out.finished = 0;
    struct timespec until;
    clock_gettime(CLOCK_REALTIME, &until);
    until.tv_sec += 5;
    while (!g_out.finished && pthread_cond_timedwait(&g_out.done, &g_out.m, &until) == 0) {}
    int ok = g_out.finished;
    g_out.pending = 0;  // timed out: never send late
    pthread_mutex_unlock(&g_out.m);
    pthread_mutex_unlock(&g_out.lock);
    return ok;
}

// --- opening the In-Game Editor the way the pencil button does --------------------
// IGEModule.OpenIGEPanel(IGEPanelIndex, IDataReference object, EditorReference editor),
// called on the main thread with the same objects the UI builds: a PersonReference
// (or ClubReference) for the record and an EditorReference (record type 42, crBD =
// record, session, dise 0). The panel then opens its own channels as usual.
#define CODE_CRBD 0x44427263u
#define CODE_DISE 0x65736964u
static struct {
    pthread_mutex_t lock, m;
    pthread_cond_t done;
    int pending, finished, result;
    uint32_t record;
    char kind[16];
    int op;  // 0 open, 1 close (dry run: log only), 2 close, 3 read tab index, 4 set tab index
    int index;
} g_ige = {PTHREAD_MUTEX_INITIALIZER, PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER, 0, 0, 0, 0, {0}, 0, 0};

static const MethodInfo *method_with_params(Il2CppClass *k, const char *name, int argc);
static size_t field_offset(Il2CppClass *k, const char *name);

static Il2CppObject *first_instance(Il2CppClass *k) {
    Il2CppClass *resources_k = il2cpp_find_class("UnityEngine.CoreModule.dll", "UnityEngine", "Resources");
    const MethodInfo *find_all = method_with_params(resources_k, "FindObjectsOfTypeAll", 1);
    if (!k || !find_all) return NULL;
    Il2CppException *exc = NULL;
    void *fargs[1] = {il2cpp_type_get_object(il2cpp_class_get_type(k))};
    Il2CppObject *arr = il2cpp_runtime_invoke(find_all, NULL, fargs, &exc);
    uint64_t count = 0;
    Il2CppObject *obj = NULL;
    if (!arr || exc || !safe_read((uint8_t *)arr + 0x18, &count, 8) || !count ||
        !safe_read((uint8_t *)arr + 0x20, &obj, sizeof obj)) return NULL;
    return obj;
}

static void layer_name(Il2CppObject *s, char *out, size_t n) {
    if (s) il2cpp_utf8((Il2CppString *)s, out, n);
    else strlcpy(out, "(null)", n);
}

// Closes the editor: PanelManager.CloseHighestPanel() (as Esc does). With dry,
// only logs the layers involved.
static int ige_close_main(int dry) {
    Il2CppClass *module_k = il2cpp_find_class("FM.UI.dll", "FM.UI", "IGEModule");
    Il2CppClass *pm_k = il2cpp_find_class("SI.Bindable.dll", "SI.Bindable", "PanelManager");
    Il2CppObject *module = first_instance(module_k), *pm = first_instance(pm_k);
    const MethodInfo *close_all = method_with_params(pm_k, "CloseHighestPanel", 0);
    if (!module || !pm || !close_all) { log_line("ige: close: module %p pm %p method %p", (void *)module, (void *)pm, (void *)close_all); return -2; }
    size_t hi_off = field_offset(module_k, "m_highestIGELayer"), list_off = field_offset(module_k, "m_layersToProcess");
    Il2CppObject *hi = NULL, *arr = NULL;
    safe_read((uint8_t *)module + hi_off, &hi, sizeof hi);
    safe_read((uint8_t *)module + list_off, &arr, sizeof arr);
    char name[128];
    layer_name(hi, name, sizeof name);
    log_line("ige: highest IGE layer = %s", name);
    uint64_t n = 0;
    if (arr && safe_read((uint8_t *)arr + 0x18, &n, 8))
        for (uint64_t i = 0; i < n && i < 16; i++) {
            Il2CppObject *s = NULL;
            safe_read((uint8_t *)arr + 0x20 + 8 * i, &s, sizeof s);
            layer_name(s, name, sizeof name);
            log_line("ige: layer to process [%llu] = %s", (unsigned long long)i, name);
        }
    uint32_t card_id = 0, report_id = 0;
    safe_read((uint8_t *)module + field_offset(module_k, "m_cardLayerPanelID"), &card_id, 4);
    safe_read((uint8_t *)module + field_offset(module_k, "m_reportLayerPanelID"), &report_id, 4);
    log_line("ige: editor panel ids card=%u report=%u", card_id, report_id);
    if (dry) return 1;
    Il2CppException *exc = NULL;
    il2cpp_runtime_invoke(close_all, pm, NULL, &exc);  // the editor is the highest panel right after opening
    log_line("ige: CloseHighestPanel()%s", exc ? " threw" : "");
    return exc ? -5 : 1;
}

static const MethodInfo *method_with_params(Il2CppClass *k, const char *name, int argc) {
    void *it = NULL;
    const MethodInfo *m;
    while (k && (m = il2cpp_class_get_methods(k, &it)))
        if (!strcmp(il2cpp_method_get_name(m), name) && (int)il2cpp_method_get_param_count(m) == argc) return m;
    return NULL;
}

// new T(List<InteropReference.Pair>{pairs}) for an InteropReference subclass T.
static Il2CppObject *make_reference(Il2CppClass *k, const uint32_t *codes, const int32_t *values, int n) {
    const MethodInfo *ctor = NULL;
    void *it = NULL;
    const MethodInfo *m;
    char tn[256];
    while (k && (m = il2cpp_class_get_methods(k, &it))) {  // the .ctor(List<InteropReference.Pair>) one
        if (strcmp(il2cpp_method_get_name(m), ".ctor") || il2cpp_method_get_param_count(m) != 1) continue;
        il2cpp_type_name(il2cpp_method_get_param(m, 0), tn, sizeof tn);
        log_line("ige: %s.ctor(%s)", il2cpp_class_get_name(k), tn);
        if (strstr(tn, "List")) ctor = m;
    }
    if (!ctor) { log_line("ige: %s has no .ctor(List<Pair>)", k ? il2cpp_class_get_name(k) : "?"); return NULL; }
    Il2CppClass *list_k = il2cpp_class_from_type(il2cpp_method_get_param(ctor, 0));
    const MethodInfo *list_ctor = method_with_params(list_k, ".ctor", 0), *add = method_with_params(list_k, "Add", 1);
    if (!list_k || !list_ctor || !add) { log_line("ige: List methods missing (%p %p %p)", (void *)list_k, (void *)list_ctor, (void *)add); return NULL; }
    Il2CppException *exc = NULL;
    Il2CppObject *list = il2cpp_object_new(list_k);
    il2cpp_runtime_invoke(list_ctor, list, NULL, &exc);
    for (int i = 0; i < n && !exc; i++) {
        int32_t pair[2];
        memcpy(&pair[0], &codes[i], 4);
        pair[1] = values[i];
        void *args[1] = {pair};
        il2cpp_runtime_invoke(add, list, args, &exc);
    }
    if (exc) { log_line("ige: List build threw"); return NULL; }
    Il2CppObject *obj = il2cpp_object_new(k);
    void *args[1] = {list};
    il2cpp_runtime_invoke(ctor, obj, args, &exc);
    if (exc) log_line("ige: %s.ctor threw", il2cpp_class_get_name(k));
    return exc ? NULL : obj;
}

static int ige_open_main(uint32_t record, const char *kind) {
    Il2CppClass *module_k = il2cpp_find_class("FM.UI.dll", "FM.UI", "IGEModule");
    Il2CppClass *editor_k = il2cpp_find_class("FM.UI.dll", "FM.UI", "EditorReference");
    int club = !strcmp(kind, "club");
    Il2CppClass *object_k = il2cpp_find_class("FM.UI.dll", "FM.UI", club ? "ClubReference" : "PersonReference");
    Il2CppClass *resources_k = il2cpp_find_class("UnityEngine.CoreModule.dll", "UnityEngine", "Resources");
    const MethodInfo *open = method_with_params(module_k, "OpenIGEPanel", 3);
    const MethodInfo *find_all = method_with_params(resources_k, "FindObjectsOfTypeAll", 1);
    if (!module_k || !editor_k || !object_k || !open || !find_all) { log_line("ige: classes/methods missing"); return -2; }
    Il2CppException *exc = NULL;
    void *fargs[1] = {il2cpp_type_get_object(il2cpp_class_get_type(module_k))};
    Il2CppObject *arr = il2cpp_runtime_invoke(find_all, NULL, fargs, &exc);
    uint64_t count = 0;
    Il2CppObject *module = NULL;
    if (!arr || exc || !safe_read((uint8_t *)arr + 0x18, &count, 8) || !count ||
        !safe_read((uint8_t *)arr + 0x20, &module, sizeof module) || !module) { log_line("ige: no IGEModule instance"); return -3; }
    uint32_t ocodes[3] = {CODE_011, CODE_GUID, CODE_FERD};
    int32_t ovals[3] = {club ? 0x1c : 0x1c, (int32_t)g_guid, (int32_t)record};
    uint32_t ecodes[4] = {CODE_011, CODE_CRBD, CODE_GUID, CODE_DISE};
    int32_t evals[4] = {42, (int32_t)record, (int32_t)g_guid, 0};
    Il2CppObject *object = make_reference(object_k, ocodes, ovals, 3);
    Il2CppObject *editor = make_reference(editor_k, ecodes, evals, 4);
    if (!object || !editor) { log_line("ige: could not build references"); return -4; }
    Il2CppClass *index_k = il2cpp_class_from_type(il2cpp_method_get_param(open, 0));
    int32_t index = 0;
    FieldInfo *f = index_k ? il2cpp_class_get_field_from_name(index_k, club ? "Club" : "Player") : NULL;
    if (f) il2cpp_field_static_get_value(f, &index);
    uint8_t idx = (uint8_t)index;
    void *args[3] = {&idx, object, editor};
    il2cpp_runtime_invoke(open, module, args, &exc);
    log_line("ige: OpenIGEPanel(%s %08x, index %d)%s", kind, record, idx, exc ? " threw" : "");
    return exc ? -5 : 1;
}

// The editor's selected tab: IGEModule.m_index, a ValueWithChangeNotify<int>. Reading
// logs the struct's layout; setting goes through its own set_Value on a boxed copy
// (so the change notification reaches the UI) and copies the result back.
static int ige_tab_main(int set, int value) {
    Il2CppClass *module_k = il2cpp_find_class("FM.UI.dll", "FM.UI", "IGEModule");
    Il2CppObject *module = first_instance(module_k);
    FieldInfo *f = module_k ? il2cpp_class_get_field_from_name(module_k, "m_index") : NULL;
    if (!module || !f) { log_line("ige: tab: no module / m_index"); return -2; }
    Il2CppClass *vk = il2cpp_class_from_type(il2cpp_field_get_type(f));
    size_t off = il2cpp_field_get_offset(f);
    int size = il2cpp_class_value_size(vk, NULL);
    void *it = NULL;
    FieldInfo *vf;
    int value_off = -1;
    while ((vf = il2cpp_class_get_fields(vk, &it))) {
        if (il2cpp_field_get_flags(vf) & FIELD_ATTRIBUTE_STATIC) continue;
        char tn[128];
        il2cpp_type_name(il2cpp_field_get_type(vf), tn, sizeof tn);
        log_line("ige: tab struct field %s : %s @%zu", il2cpp_field_get_name(vf), tn, il2cpp_field_get_offset(vf));
        if (value_off < 0 && !strcmp(tn, "System.Int32")) value_off = (int)il2cpp_field_get_offset(vf) - 0x10;
    }
    int32_t cur = 0;
    if (value_off >= 0) safe_read((uint8_t *)module + off + value_off, &cur, 4);
    log_line("ige: tab index = %d (struct size %d)", cur, size);
    if (!set) return cur;
    const MethodInfo *setter = method_with_params(vk, "set_Value", 1);
    if (!setter || size <= 0 || size > 256) { log_line("ige: tab: no set_Value"); return -3; }
    Il2CppObject *boxed = il2cpp_value_box(vk, (uint8_t *)module + off);
    Il2CppException *exc = NULL;
    int32_t v = value;
    void *args[1] = {&v};
    il2cpp_runtime_invoke(setter, boxed, args, &exc);
    if (exc) { log_line("ige: tab: set_Value threw"); return -5; }
    memcpy((uint8_t *)module + off, (uint8_t *)boxed + 0x10, (size_t)size);
    log_line("ige: tab index set to %d", value);
    return value;
}

// --- clicking FM's own UI (Unity UI Toolkit) ----------------------------------------
// Finds text elements whose visible text equals a given text (zero-width spaces
// ignored) in every UIDocument, and clicks one: a ClickEvent sent to the element
// (it bubbles to its handlers) and, when an ancestor has a Clickable, that
// Clickable's SimulateSingleClick. Main thread, fault-guarded.
static const MethodInfo *method_up(Il2CppClass *k, const char *name, int argc) {
    for (; k; k = il2cpp_class_get_parent(k)) {
        const MethodInfo *m = method_with_params(k, name, argc);
        if (m) return m;
    }
    return NULL;
}

static Il2CppObject *call0(Il2CppObject *obj, const char *name) {
    const MethodInfo *m = obj ? method_up(il2cpp_object_get_class(obj), name, 0) : NULL;
    Il2CppException *exc = NULL;
    Il2CppObject *r = m ? il2cpp_runtime_invoke(m, obj, NULL, &exc) : NULL;
    return exc ? NULL : r;
}

static int same_text(Il2CppString *s, const char *want) {
    char buf[256], clean[256];
    if (!s) return 0;
    il2cpp_utf8(s, buf, sizeof buf);
    size_t o = 0;
    for (const char *p = buf; *p && o < sizeof clean - 1;) {
        if ((unsigned char)p[0] == 0xE2 && (unsigned char)p[1] == 0x80 && (unsigned char)p[2] == 0x8B) { p += 3; continue; }
        clean[o++] = *p++;
    }
    clean[o] = 0;
    return !strcmp(clean, want);
}

static struct { Il2CppClass *text_k; const MethodInfo *child_count, *element_at; Il2CppObject *found[32]; int nfound; } g_ui;

static void ui_walk(Il2CppObject *ve, const char *want, int depth) {
    if (!ve || depth > 60 || g_ui.nfound >= 32) return;
    Il2CppClass *k = il2cpp_object_get_class(ve);
    if (g_ui.text_k && derives_from(k, g_ui.text_k)) {
        Il2CppObject *s = call0(ve, "get_text");
        if (same_text((Il2CppString *)s, want)) g_ui.found[g_ui.nfound++] = ve;
    }
    Il2CppException *exc = NULL;
    Il2CppObject *boxed = il2cpp_runtime_invoke(g_ui.child_count, ve, NULL, &exc);
    int32_t n = 0;
    if (!boxed || exc || !safe_read((uint8_t *)boxed + 0x10, &n, 4)) return;
    for (int32_t i = 0; i < n && i < 2000; i++) {
        void *args[1] = {&i};
        exc = NULL;
        Il2CppObject *child = il2cpp_runtime_invoke(g_ui.element_at, ve, args, &exc);
        if (!exc && child) ui_walk(child, want, depth + 1);
    }
}

static char g_ui_text[128];
static int g_ui_click, g_ui_nth;

static int ui_main(void) {
    Il2CppClass *doc_k = il2cpp_find_class("UnityEngine.UIElementsModule.dll", "UnityEngine.UIElements", "UIDocument");
    Il2CppClass *ve_k = il2cpp_find_class("UnityEngine.UIElementsModule.dll", "UnityEngine.UIElements", "VisualElement");
    g_ui.text_k = il2cpp_find_class("UnityEngine.UIElementsModule.dll", "UnityEngine.UIElements", "TextElement");
    g_ui.child_count = method_up(ve_k, "get_childCount", 0);
    g_ui.element_at = method_up(ve_k, "ElementAt", 1);
    Il2CppClass *resources_k = il2cpp_find_class("UnityEngine.CoreModule.dll", "UnityEngine", "Resources");
    const MethodInfo *find_all = method_with_params(resources_k, "FindObjectsOfTypeAll", 1);
    if (!doc_k || !ve_k || !g_ui.text_k || !g_ui.child_count || !g_ui.element_at || !find_all) { log_line("ui: classes/methods missing"); return -2; }
    Il2CppException *exc = NULL;
    void *fargs[1] = {il2cpp_type_get_object(il2cpp_class_get_type(doc_k))};
    Il2CppObject *arr = il2cpp_runtime_invoke(find_all, NULL, fargs, &exc);
    uint64_t ndocs = 0;
    if (!arr || exc || !safe_read((uint8_t *)arr + 0x18, &ndocs, 8)) return -3;
    g_ui.nfound = 0;
    for (uint64_t d = 0; d < ndocs && d < 64; d++) {
        Il2CppObject *doc = NULL;
        safe_read((uint8_t *)arr + 0x20 + 8 * d, &doc, sizeof doc);
        ui_walk(call0(doc, "get_rootVisualElement"), g_ui_text, 0);
    }
    for (int i = 0; i < g_ui.nfound; i++) {  // log where each match sits (its ancestors' names)
        char path[512] = "";
        Il2CppObject *e = g_ui.found[i];
        for (int up = 0; e && up < 8; up++, e = call0(e, "get_parent")) {
            char nm[64] = "";
            Il2CppObject *s = call0(e, "get_name");
            if (s) il2cpp_utf8((Il2CppString *)s, nm, sizeof nm);
            strlcat(path, nm[0] ? nm : il2cpp_class_get_name(il2cpp_object_get_class(e)), sizeof path);
            strlcat(path, " < ", sizeof path);
        }
        log_line("ui: match %d: %s", i, path);
    }
    if (!g_ui_click || !g_ui.nfound) return g_ui.nfound;
    int pick = g_ui_nth < 0 ? g_ui.nfound + g_ui_nth : g_ui_nth;
    if (pick < 0 || pick >= g_ui.nfound) return -4;
    Il2CppObject *target = g_ui.found[pick];
    Il2CppClass *click_k = il2cpp_find_class("UnityEngine.UIElementsModule.dll", "UnityEngine.UIElements", "ClickEvent");
    const MethodInfo *pooled = method_up(click_k, "GetPooled", 0);
    exc = NULL;
    Il2CppObject *evt = pooled ? il2cpp_runtime_invoke(pooled, NULL, NULL, &exc) : NULL;
    if (evt && !exc) {
        const MethodInfo *set_target = method_up(il2cpp_object_get_class(evt), "set_target", 1);
        const MethodInfo *send = method_up(il2cpp_object_get_class(target), "SendEvent", 1);
        void *a1[1] = {target};
        if (set_target) il2cpp_runtime_invoke(set_target, evt, a1, &exc);
        void *a2[1] = {evt};
        if (send && !exc) il2cpp_runtime_invoke(send, target, a2, &exc);
        log_line("ui: ClickEvent sent to match %d%s", pick, exc ? " (threw)" : "");
    }
    for (Il2CppObject *e = target; e; e = call0(e, "get_parent")) {  // a Clickable (buttons, tabs)
        Il2CppObject *clickable = call0(e, "get_clickable");
        if (!clickable) continue;
        const MethodInfo *sim = method_up(il2cpp_object_get_class(clickable), "SimulateSingleClick", 2);
        exc = NULL;
        Il2CppObject *evt2 = pooled ? il2cpp_runtime_invoke(pooled, NULL, NULL, &exc) : NULL;
        int32_t delay = 0;
        void *a3[2] = {evt2, &delay};
        if (sim && evt2 && !exc) il2cpp_runtime_invoke(sim, clickable, a3, &exc);
        log_line("ui: Clickable.SimulateSingleClick%s", exc ? " threw" : "");
        break;
    }
    return 1;
}

static void ige_step(void) {
    pthread_mutex_lock(&g_ige.m);
    if (g_ige.pending) {
        sigjmp_buf here;
        if (sigsetjmp(here, 1)) {
            g_ige.result = -9;  // a fault inside: nothing more is attempted
            log_line("ige: fault while opening the editor");
        } else {
            t_guard = &here;
            g_ige.result = g_ige.op == 5 ? ui_main() : g_ige.op >= 3 ? ige_tab_main(g_ige.op == 4, g_ige.index)
                         : g_ige.op ? ige_close_main(g_ige.op == 1) : ige_open_main(g_ige.record, g_ige.kind);
            t_guard = NULL;
        }
        g_ige.pending = 0;
        g_ige.finished = 1;
        pthread_cond_broadcast(&g_ige.done);
    }
    pthread_mutex_unlock(&g_ige.m);
}

static int ige_run(uint32_t record, const char *kind, int op) {
    if (!tap_ready()) return 0;
    pthread_mutex_lock(&g_ige.lock);
    pthread_mutex_lock(&g_ige.m);
    g_ige.op = op;
    g_ige.record = record;
    strlcpy(g_ige.kind, kind, sizeof g_ige.kind);
    g_ige.pending = 1;
    g_ige.finished = 0;
    g_ige.result = 0;
    struct timespec until;
    clock_gettime(CLOCK_REALTIME, &until);
    until.tv_sec += 5;
    while (!g_ige.finished && pthread_cond_timedwait(&g_ige.done, &g_ige.m, &until) == 0) {}
    int r = g_ige.finished ? g_ige.result : 0;
    g_ige.pending = 0;
    pthread_mutex_unlock(&g_ige.m);
    pthread_mutex_unlock(&g_ige.lock);
    return r;
}

int tap_editor_open(uint32_t record, const char *kind) { return ige_run(record, kind, 0); }
int tap_editor_close(int dry) { return ige_run(0, "", dry ? 1 : 2); }
int tap_editor_tab(int set, int index) { g_ige.index = index; return ige_run(0, "", set ? 4 : 3); }
int tap_ui_click(const char *text, int click, int nth) {
    strlcpy(g_ui_text, text, sizeof g_ui_text);
    g_ui_click = click;
    g_ui_nth = nth;
    return ige_run(0, "", 5);
}

static void hook_tick(u64 a, u64 b, u64 c, u64 d, u64 e, u64 f, u64 g, u64 h) {
    g_orig_tick(a, b, c, d, e, f, g, h);
    if (!g_plugin) g_plugin = a;
    query_step();
    send_step();
    update_step();
    ige_step();
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
    const MethodInfo *update = method(wrapper, "UpdateChannelData", 2);
    const MethodInfo *send = method(wrapper, "SendEvent", 3);
    g_typed_value_as_string = method(typed_value, "AsString", 0);
    g_typed_value_get = method_returning(typed_value, "Get", "System.Object");
    g_open_method = open;
    g_actions_method = actions;
    g_send_method = send;
    g_update_method = update;
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

    install_fault_guard();
    g_tap = data_file("tap.log", "w");
    int n = patch_vtable(receiver, response, (void *)hook_response, &g_orig_response);
    n += patch_vtable(receiver, event, (void *)hook_event, &g_orig_event);
    n += patch_vtable(wrapper, open, (void *)hook_open, &g_orig_open);
    n += patch_vtable(wrapper, actions, (void *)hook_actions, &g_orig_actions);
    n += patch_vtable(wrapper, tick, (void *)hook_tick, &g_orig_tick);
    if (update) n += patch_vtable(wrapper, update, (void *)hook_update, &g_orig_update);
    if (send) n += patch_vtable(wrapper, send, (void *)hook_send, &g_orig_send);
    log_line("tap: installed (%d vtable slots)", n);
}

#else
void tap_install(void) { log_line("tap: only supported on Apple Silicon"); }
int tap_ready(void) { return 0; }
char *tap_query(uint32_t record, const uint32_t *props, int n) { (void)record; (void)props; (void)n; return NULL; }
char *tap_query_target(const uint8_t *t, int tl, const uint32_t *props, int n) { (void)t; (void)tl; (void)props; (void)n; return NULL; }
char *tap_hold_target(const uint8_t *t, int tl, const uint32_t *props, int n, uint32_t *id) { (void)t; (void)tl; (void)props; (void)n; (void)id; return NULL; }
int tap_release(uint32_t id) { (void)id; return 0; }
int tap_seen(uint32_t *out, int cap) { (void)out; (void)cap; return 0; }
uint32_t tap_focus(uint8_t kind) { (void)kind; return 0; }
char *tap_events_json(uint64_t since) { (void)since; return strdup("[]"); }
char *tap_last_target(uint32_t type) { (void)type; return NULL; }
int tap_send_event(uint32_t id, const uint8_t *t, int tl, const uint8_t *d, int dl) { (void)id; (void)t; (void)tl; (void)d; (void)dl; return 0; }
char *tap_editor_json(void) { return strdup("[]"); }
int tap_editor_open(uint32_t record, const char *kind) { (void)record; (void)kind; return 0; }
int tap_editor_close(int dry) { (void)dry; return 0; }
int tap_editor_tab(int set, int index) { (void)set; (void)index; return 0; }
int tap_ui_click(const char *text, int click, int nth) { (void)text; (void)click; (void)nth; return 0; }
int tap_editor_set(uint64_t key, const uint8_t *d, int dl) { (void)key; (void)d; (void)dl; return 0; }
uint32_t tap_guid(void) { return 0; }
void fourcc_string(uint32_t v, char out[16]) { snprintf(out, 16, "#%08x", v); }
uint32_t fourcc_parse(const char *s) { return (uint32_t)strtoul(s + 1, NULL, 16); }
#endif
