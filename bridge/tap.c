// Data tap: observes the channel traffic between FM's interface and its native
// game engine, and writes it to tap.log with property names resolved.
//
// Two virtual methods are hooked by swapping their entry in the class's
// virtual-method table (ordinary heap data). Game code is never modified, so
// the hardened runtime's code-signing checks are unaffected.
//
//   IGamePlugin.OpenChannels(target, ChannelData[])      UI -> engine: "watch these
//       (wrapper class FM_GamePlugin_IGamePlugin_NativeWrapper)  properties of this object"
//   IPluginDataReceiver.SendDataResponse(keys, props, bytes)  engine -> UI: values
//       (class FM.GamePlugin.GameReceiver)
//   IPluginDataReceiver.SendEvent(id, bytes)              engine -> UI: events
#include "tap.h"

#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

#include "catalog.h"
#include "il2cpp_api.h"
#include "log.h"

#if defined(__aarch64__)

typedef uint64_t u64;
typedef void (*Fn8)(u64, u64, u64, u64, u64, u64, u64, u64);

static Fn8 g_orig_response, g_orig_event, g_orig_open, g_orig_tick, g_orig_actions;
static FILE *g_tap;
static size_t g_tap_bytes;
static const size_t kTapLimit = 64u << 20;  // stop logging after 64 MB
static __thread int t_busy;                 // re-entrancy guard

// --- type handles resolved at install time ---------------------------------
static const MethodInfo *g_typed_value_as_string;
static size_t g_receiver_list_offset;  // GameReceiver.m_resusableTypedList
static size_t g_list_items_offset, g_list_size_offset;
static int g_list_layout_known;
static size_t g_receiver_callback_offset;  // GameReceiver.m_onChannelDataChange
static const MethodInfo *g_open_method, *g_actions_method;
static int32_t g_close_mode = -1;  // ChannelActionMode.CloseChannel
static int32_t g_add_node_mode = -1, g_remove_node_mode = -1, g_set_property_mode = -1;
static int g_action_size;          // sizeof(ChannelAction)
static u64 g_plugin;               // the IGamePlugin native wrapper instance

// --- active queries ---------------------------------------------------------
// Our own channels use keys with this prefix so they never collide with the UI's.
#define OUR_KEY_PREFIX 0xF27E000000000000ull
#define OUR_KEY_MASK 0xFFFF000000000000ull
static int is_ours(u64 key) { return (key & OUR_KEY_MASK) == OUR_KEY_PREFIX; }

#define CODE_011 0x31313020u   // " 011": record type
#define CODE_GUID 0x47756964u  // "Guid": loaded game session
#define CODE_FERD 0x64726566u  // "ferd": database record id (high byte = record kind)
static uint32_t g_guid, g_seen_person, g_seen_club, g_record_type;
static uint32_t g_max_gen;      // highest generation the UI has used
static int g_probe_hits;        // responses received for our channels

// System.Span<T> layout differs between corlib versions; detected at runtime.
static struct {
    int size;  // value size in bytes; > 16 means passed by pointer on arm64
    int ptr_off, len_off;  // "_reference"/"_pointer" layout
    int pinnable_off, byte_off;  // portable layout (_pinnable + _byteOffset)
} g_span = {.ptr_off = -1, .len_off = -1, .pinnable_off = -1, .byte_off = -1};

// --- output ------------------------------------------------------------------
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

// Span<T> arguments arrive as two registers each: data pointer, then length.
// (Confirmed from raw register dumps; layout fields learned at install time.)
static void span_arg(const u64 *x, int reg, const void **data, int *len) {
    u64 pair[2] = {x[reg], x[reg + 1]};
    uint8_t raw[16];
    memcpy(raw, pair, sizeof raw);  // memcpy, not pointer casts: avoids aliasing UB
    void *p;
    int32_t n;
    memcpy(&p, raw + g_span.ptr_off, sizeof p);
    memcpy(&n, raw + g_span.len_off, sizeof n);
    *data = p;
    *len = n;
}

static int safe_read(const void *addr, void *out, size_t n);

// Copies up to cap bytes of a span into buf; returns bytes copied (0 if unreadable).
static int span_copy(const void *data, int len, size_t elem, void *buf, size_t cap) {
    if (!data || len <= 0) return 0;
    size_t want = (size_t)len * elem;
    if (want > cap) want = cap - cap % elem;
    return safe_read(data, buf, want) ? (int)(want / elem) : 0;
}

static void fourcc(uint32_t v, char out[16]) {
    char c[4] = {(char)(v & 0xff), (char)(v >> 8), (char)(v >> 16), (char)(v >> 24)};
    int printable = 1;
    for (int i = 0; i < 4; i++) if (c[i] < 0x20 || c[i] > 0x7e) printable = 0;
    if (printable) snprintf(out, 16, "%c%c%c%c", c[0], c[1], c[2], c[3]);
    else snprintf(out, 16, "#%08x", v);
}

// Channel key -> property, learned from OpenChannels.
#define KEY_SLOTS 65536
static struct { u64 key; uint32_t prop; } g_keys[KEY_SLOTS];

static void key_put(u64 key, uint32_t prop) {
    uint32_t h = (uint32_t)(key ^ (key >> 29)) & (KEY_SLOTS - 1);
    for (int i = 0; i < 64; i++, h = (h + 1) & (KEY_SLOTS - 1))
        if (!g_keys[h].key || g_keys[h].key == key) { g_keys[h].key = key; g_keys[h].prop = prop; return; }
}

static uint32_t key_get(u64 key) {
    uint32_t h = (uint32_t)(key ^ (key >> 29)) & (KEY_SLOTS - 1);
    for (int i = 0; i < 64; i++, h = (h + 1) & (KEY_SLOTS - 1)) {
        if (g_keys[h].key == key) return g_keys[h].prop;
        if (!g_keys[h].key) break;
    }
    return 0;
}

static char *invoke_to_utf8(const MethodInfo *m, void *obj, char *buf, size_t n) {
    Il2CppException *exc = NULL;
    Il2CppObject *r = il2cpp_runtime_invoke(m, obj, NULL, &exc);
    if (exc) { strlcpy(buf, "<exception>", n); return buf; }
    if (!r) { strlcpy(buf, "null", n); return buf; }
    return il2cpp_utf8((Il2CppString *)r, buf, n);
}

static void value_string(Il2CppObject *v, char *buf, size_t n) {
    if (!v) { strlcpy(buf, "null", n); return; }
    const MethodInfo *m = il2cpp_object_get_virtual_method(v, g_typed_value_as_string);
    invoke_to_utf8(m ? m : g_typed_value_as_string, v, buf, n);
    for (char *c = buf; *c; c++) if (*c == '\n' || *c == '\t') *c = ' ';
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
    g_list_layout_known = g_list_items_offset && g_list_size_offset ? 1 : -1;
    log_line("tap: List<TypedValue> layout items@0x%zx size@0x%zx", g_list_items_offset, g_list_size_offset);
}

// Reads the values GameReceiver decoded into m_resusableTypedList.
static int decoded_values(Il2CppObject *receiver, Il2CppObject **out, int cap) {
    Il2CppObject *list = NULL;
    if (!safe_read((uint8_t *)receiver + g_receiver_list_offset, &list, sizeof list) || !list) return 0;
    if (!g_list_layout_known) learn_list_layout(list);
    if (g_list_layout_known <= 0) return 0;
    uint8_t *arr = NULL;
    int32_t count = 0;
    if (!safe_read((uint8_t *)list + g_list_items_offset, &arr, sizeof arr) ||
        !safe_read((uint8_t *)list + g_list_size_offset, &count, sizeof count) || !arr || count <= 0)
        return 0;
    if (count > cap) count = cap;
    return safe_read(arr + 0x20, out, sizeof(void *) * (size_t)count) ? count : 0;  // array data at 0x20
}

// --- hooks ---------------------------------------------------------------------
// SendDataResponse(Span<ulong> keys, Span<uint> generations, Span<byte> data)
static void probe_record(const u64 *keys, int nk, Il2CppObject **vals, int nv);

static void hook_response(u64 a, u64 b, u64 c, u64 d, u64 e, u64 f, u64 g, u64 h) {
    // Responses to our own channels must not reach the UI, which does not know
    // their keys: detach the UI callback for that call only.
    u64 x0[8] = {a, b, c, d, e, f, g, h};
    const void *kp0;
    int kn0;
    span_arg(x0, 1, &kp0, &kn0);
    static u64 k0[1024];
    int nk0 = span_copy(kp0, kn0, 8, k0, sizeof k0);
    int ours = 0;
    for (int i = 0; i < nk0; i++) ours += is_ours(k0[i]);
    if (ours && ours == nk0 && g_receiver_callback_offset) {
        void **cb = (void **)((uint8_t *)a + g_receiver_callback_offset);
        void *saved = *cb;
        *cb = NULL;
        g_orig_response(a, b, c, d, e, f, g, h);
        *cb = saved;
    } else {
        g_orig_response(a, b, c, d, e, f, g, h);
    }
    static __thread int recording;  // separate guard: replies may arrive inside our own Tick work
    if (ours && !recording) {
        recording = 1;
        static Il2CppObject *v0[4096];
        int nv0 = decoded_values((Il2CppObject *)a, v0, 4096);
        probe_record(k0, nk0, v0, nv0);
        recording = 0;
    }
    if (ours == nk0 && nk0) return;  // keep tap.log for UI traffic only
    if (t_busy || !g_tap) return;
    t_busy = 1;
    u64 x[8] = {a, b, c, d, e, f, g, h};
    const void *kp, *gp, *bp;
    int kn, gn, bn;
    span_arg(x, 1, &kp, &kn);
    span_arg(x, 3, &gp, &gn);
    span_arg(x, 5, &bp, &bn);
    static u64 keys[1024];
    static uint32_t gens[1024];
    static Il2CppObject *vals[4096];
    uint8_t raw[48];
    int nk = span_copy(kp, kn, 8, keys, sizeof keys);
    int ng = span_copy(gp, gn, 4, gens, sizeof gens);
    int nr = span_copy(bp, bn, 1, raw, sizeof raw);
    int nv = decoded_values((Il2CppObject *)a, vals, 4096);
    char rh[100];
    hex(raw, nr, rh, sizeof rh);
    tap_printf("R keys=%d bytes=%d values=%d raw=%s\n", kn, bn, nv, rh);
    char val[512], cc[16];
    for (int i = 0; i < nk; i++) {
        fourcc(key_get(keys[i]), cc);
        tap_printf("  %016llx %s gen=%u\n", keys[i], cc, i < ng ? gens[i] : 0);
    }
    for (int i = 0; i < nv && i < 400; i++) {
        value_string(vals[i], val, sizeof val);
        tap_printf("    [%d] %s\n", i, val);
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
    if (!t_busy && g_tap) {
        t_busy = 1;
        u64 x[8] = {a, b, c, d, e, f, g, h};
        const void *tp, *cp;
        int tn, cn;
        span_arg(x, 1, &tp, &tn);
        span_arg(x, 3, &cp, &cn);
        uint8_t target[96];
        static uint8_t ch[16 * 1024];  // ChannelData { u32 property; u32 generation; u64 key; }
        char th[200], cc[16];
        hex(target, span_copy(tp, tn, 1, target, sizeof target), th, sizeof th);
        int n = span_copy(cp, cn, 16, ch, sizeof ch);
        tap_printf("O target(%d)=%s channels=%d\n", tn, th, cn);
        if (!g_plugin) g_plugin = a;
        // target: u8 pair count, then (u32 code, u32 value) pairs
        int tcount = tn > 0 ? target[0] : 0;
        for (int p = 0; p < tcount && 1 + 8 * p + 8 <= (int)sizeof target && 1 + 8 * p + 8 <= tn; p++) {
            uint32_t code, val;
            memcpy(&code, target + 1 + 8 * p, 4);
            memcpy(&val, target + 1 + 8 * p + 4, 4);
            if (code == CODE_GUID && val) g_guid = val;
            if (code == CODE_011 && tcount == 3) g_record_type = val;
            if (code == CODE_FERD && (val >> 24) == 0x01 && !g_seen_person) g_seen_person = val;
            if (code == CODE_FERD && (val >> 24) == 0x18 && !g_seen_club) g_seen_club = val;
        }
        for (int i = 0; i < n; i++) {
            uint32_t prop, gen;
            u64 key;
            memcpy(&prop, ch + 16 * i, 4);
            memcpy(&gen, ch + 16 * i + 4, 4);
            memcpy(&key, ch + 16 * i + 8, 8);
            key_put(key, prop);
            if (gen > g_max_gen && gen < 0x40000000u) g_max_gen = gen;
            fourcc(prop, cc);
            tap_printf("  %016llx %s gen=%u\n", key, cc, gen);
        }
        fflush(g_tap);
        t_busy = 0;
    }
    g_orig_open(a, b, c, d, e, f, g, h);
}

// --- probe: query every property of one person and one club -------------------
// Runs on the main thread from the Tick hook once a career is loaded. Results
// go to probe.tsv: record <tab> property code <tab> value.
static uint32_t g_props[2][8192];
static int g_nprops[2];
static uint32_t g_probe_ids[2];
static int g_probe_state, g_probe_target, g_probe_next, g_probe_wait, g_probe_attempts;
static FILE *g_probe;

static void probe_record(const u64 *keys, int nk, Il2CppObject **vals, int nv) {
    if (!g_probe) return;
    char val[1024], cc[16];
    for (int i = 0; i < nk; i++) {
        if (!is_ours(keys[i])) continue;
        int t = (int)((keys[i] >> 32) & 0xffff), p = (int)(keys[i] & 0xffffffff);
        if (t > 1 || p >= g_nprops[t]) continue;
        if (i < nv) value_string(vals[i], val, sizeof val);
        else strlcpy(val, "?", sizeof val);
        fourcc(g_props[t][p], cc);
        fprintf(g_probe, "%s\t%08x\t%s\t%08x\t%s\n", t ? "club" : "person", g_probe_ids[t], cc, g_props[t][p], val);
        g_probe_hits++;
    }
    fflush(g_probe);
}

static int make_target(uint8_t *out, uint32_t record_id) {
    uint32_t pairs[6] = {CODE_011, g_record_type, CODE_GUID, g_guid, CODE_FERD, record_id};
    out[0] = 3;
    memcpy(out + 1, pairs, sizeof pairs);
    return 1 + (int)sizeof pairs;
}

#define OUR_ROOT_KEY (OUR_KEY_PREFIX | 0x0000FFFF00000000ull)

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

static void probe_open_batch(void) {
    int t = g_probe_target;
    uint8_t target[32];
    int tlen = make_target(target, g_probe_ids[t]);
    static uint8_t ch[16 * 128];
    int n = 0;
    for (; n < 128 && g_probe_next < g_nprops[t]; n++, g_probe_next++) {
        uint32_t prop = g_props[t][g_probe_next], gen = ++g_max_gen;
        u64 key = OUR_KEY_PREFIX | ((u64)t << 32) | (u64)g_probe_next;
        memcpy(ch + 16 * n, &prop, 4);
        memcpy(ch + 16 * n + 4, &gen, 4);
        memcpy(ch + 16 * n + 8, &key, 8);
    }
    if (!n) return;
    // Register each key as a node of the engine's binding tree (as the UI does)
    // before opening it: AddNode(key, parent) + SetPropertyID(key, property).
    static uint8_t acts[24 * 2 * 130];
    int na = 0;
    static int root_added;
    u64 parent = g_probe_attempts == 1 ? 0 : OUR_ROOT_KEY;
    if (parent && !root_added) { put_action(acts, na++, OUR_ROOT_KEY, 0, g_add_node_mode); root_added = 1; }
    for (int i = 0; i < n; i++) {
        uint32_t prop;
        u64 key;
        memcpy(&prop, ch + 16 * i, 4);
        memcpy(&key, ch + 16 * i + 8, 8);
        put_action(acts, na++, key, parent, g_add_node_mode);
        put_action(acts, na++, key, (u64)prop, g_set_property_mode);
    }
    run_actions(acts, na);
    g_orig_open(g_plugin, (u64)target, (u64)tlen, (u64)ch, (u64)n, (u64)g_open_method, 0, 0);
}

static void probe_close_all(void) {
    if (g_close_mode < 0 || !g_orig_actions || g_action_size < 20) return;
    static uint8_t actions[24 * 2 * 8192];
    for (int t = 0; t < 2; t++) {
        int n = 0;
        for (int p = 0; p < g_nprops[t]; p++) {
            u64 key = OUR_KEY_PREFIX | ((u64)t << 32) | (u64)p;
            put_action(actions, n++, key, 0, g_close_mode);
            if (g_remove_node_mode >= 0) put_action(actions, n++, key, 0, g_remove_node_mode);
        }
        run_actions(actions, n);
    }
}

static void probe_step(void) {
    switch (g_probe_state) {
    case 0:  // wait for a loaded career with a person and a club on screen
        if (!g_plugin || !g_guid || !g_record_type || !g_seen_person || !g_seen_club) return;
        if (++g_probe_wait < 300) return;  // let the career screen settle
        g_probe_ids[0] = g_seen_person;
        g_probe_ids[1] = g_seen_club;
        g_nprops[0] = catalog_properties("FM.UI", "PersonReference", g_props[0], 8192);
        g_nprops[1] = catalog_properties("FM.UI", "ClubReference", g_props[1], 8192);
        g_probe = data_file("probe.tsv", "w");
        log_line("probe: person %08x (%d props), club %08x (%d props), guid %08x, type %08x", g_probe_ids[0],
                 g_nprops[0], g_probe_ids[1], g_nprops[1], g_guid, g_record_type);
        g_probe_state = 1;
        return;
    case 1:  // open our channels, one batch per frame
        probe_open_batch();
        if (g_probe_next >= g_nprops[g_probe_target]) {
            g_probe_next = 0;
            if (++g_probe_target > 1) { g_probe_state = 2; g_probe_wait = 0; }
        }
        return;
    case 2:  // collect responses
        if (++g_probe_wait < 600) return;
        probe_close_all();
        log_line("probe: attempt done, %d values received, channels closed", g_probe_hits);
        g_probe_state = 3;
        g_probe_wait = 0;
        return;
    case 3:  // retry (with fresh generations) while nothing has come back
        if (g_probe_hits || g_probe_attempts >= 3) return;
        if (++g_probe_wait < 900) return;
        g_probe_attempts++;
        g_probe_target = g_probe_next = 0;
        g_probe_state = 1;
        log_line("probe: retry %d (generations from %u)", g_probe_attempts, g_max_gen + 1);
        return;
    default:
        return;
    }
}

// PerformChannelActions(Span<ChannelAction> actions): logged to learn the UI's protocol.
static void hook_actions(u64 a, u64 b, u64 c, u64 d, u64 e, u64 f, u64 g, u64 h) {
    if (!t_busy && g_tap && g_action_size >= 20) {
        t_busy = 1;
        u64 x[8] = {a, b, c, d, e, f, g, h};
        const void *ap;
        int an;
        span_arg(x, 1, &ap, &an);
        static uint8_t acts[24 * 2048];
        int n = span_copy(ap, an, (size_t)g_action_size, acts, sizeof acts);
        tap_printf("A actions=%d\n", an);
        for (int i = 0; i < n && i < 400; i++) {
            u64 key, extra;
            int32_t mode;
            memcpy(&key, acts + (size_t)g_action_size * i, 8);
            memcpy(&extra, acts + (size_t)g_action_size * i + 8, 8);
            memcpy(&mode, acts + (size_t)g_action_size * i + 16, 4);
            char cc[16];
            fourcc(key_get(key), cc);
            tap_printf("  %016llx %s mode=%d extra=%016llx\n", key, cc, mode, extra);
        }
        fflush(g_tap);
        t_busy = 0;
    }
    g_orig_actions(a, b, c, d, e, f, g, h);
}

static void hook_tick(u64 a, u64 b, u64 c, u64 d, u64 e, u64 f, u64 g, u64 h) {
    g_orig_tick(a, b, c, d, e, f, g, h);
    if (t_busy) return;
    t_busy = 1;
    if (!g_plugin) g_plugin = a;
    probe_step();
    t_busy = 0;
}

// --- vtable patching -----------------------------------------------------------
static int safe_read(const void *addr, void *out, size_t n) {
    mach_vm_size_t got = 0;
    return mach_vm_read_overwrite(mach_task_self(), (mach_vm_address_t)addr, n, (mach_vm_address_t)out, &got) ==
               KERN_SUCCESS && got == n;
}

// Replaces every vtable entry {methodPtr, method == m} in klass. Returns count.
static int patch_vtable(Il2CppClass *klass, const MethodInfo *m, void *hook, Fn8 *orig) {
    void *const *mi = (void *const *)m;
    int patched = 0;
    for (size_t off = 0; off < 0x4000; off += 8) {
        void *pair[2];
        if (!safe_read((uint8_t *)klass + off, pair, sizeof pair)) break;
        if (pair[1] != m || (pair[0] != mi[0] && pair[0] != mi[1])) continue;
        void **slot = (void **)((uint8_t *)klass + off);
        if (!*orig) *orig = (Fn8)pair[0];
        if (!hook) return 1;  // lookup only
        mach_vm_protect(mach_task_self(), (mach_vm_address_t)slot, 8, false, VM_PROT_READ | VM_PROT_WRITE);
        *slot = hook;
        patched++;
        log_line("tap: patched %s.%s at class+0x%zx", il2cpp_class_get_name(klass), il2cpp_method_get_name(m), off);
    }
    return patched;
}

static void learn_span_layout(const MethodInfo *m) {
    Il2CppClass *k = il2cpp_class_from_type(il2cpp_method_get_param(m, 0));
    g_span.size = il2cpp_class_value_size(k, NULL);
    void *it = NULL;
    FieldInfo *f;
    while ((f = il2cpp_class_get_fields(k, &it))) {
        if (il2cpp_field_get_flags(f) & FIELD_ATTRIBUTE_STATIC) continue;
        const char *n = il2cpp_field_get_name(f);
        int off = (int)il2cpp_field_get_offset(f) - 0x10;  // value-type offsets include the object header
        if (!strcmp(n, "_reference") || !strcmp(n, "_pointer")) g_span.ptr_off = off;
        else if (!strcmp(n, "_length")) g_span.len_off = off;
        else if (!strcmp(n, "_pinnable")) g_span.pinnable_off = off;
        else if (!strcmp(n, "_byteOffset")) g_span.byte_off = off;
        log_line("tap: Span field %s @%d", n, off);
    }
    log_line("tap: Span size %d", g_span.size);
}

static const MethodInfo *method(Il2CppClass *k, const char *name, int argc) {
    const MethodInfo *m = k ? il2cpp_class_get_method_from_name(k, name, argc) : NULL;
    if (!m) log_line("tap: method %s.%s/%d not found", k ? il2cpp_class_get_name(k) : "?", name, argc);
    return m;
}

void tap_install(void) {
    Il2CppClass *receiver = il2cpp_find_class("FM.GamePlugin.dll", "FM.GamePlugin", "GameReceiver");
    Il2CppClass *wrapper = il2cpp_find_class("FM.GamePlugin.dll", "", "FM_GamePlugin_IGamePlugin_NativeWrapper");
    Il2CppClass *typed_value = il2cpp_find_class("SI.Core.dll", "SI.Core", "TypedValue");

    const MethodInfo *response = method(receiver, "SendDataResponse", 3);
    const MethodInfo *event = method(receiver, "SendEvent", 2);
    const MethodInfo *open = method(wrapper, "OpenChannels", 2);
    const MethodInfo *tick = method(wrapper, "Tick", 0);
    const MethodInfo *actions = method(wrapper, "PerformChannelActions", 1);
    g_open_method = open;
    g_actions_method = actions;
    Il2CppClass *mode_class = il2cpp_find_class("FM.GamePlugin.dll", "FM.GamePlugin", "ChannelActionMode");
    FieldInfo *close_field = mode_class ? il2cpp_class_get_field_from_name(mode_class, "CloseChannel") : NULL;
    if (close_field) il2cpp_field_static_get_value(close_field, &g_close_mode);
    const char *mode_names[3] = {"AddNode", "RemoveNode", "SetPropertyID"};
    int32_t *mode_vals[3] = {&g_add_node_mode, &g_remove_node_mode, &g_set_property_mode};
    for (int i = 0; i < 3 && mode_class; i++) {
        FieldInfo *mf = il2cpp_class_get_field_from_name(mode_class, mode_names[i]);
        if (mf) il2cpp_field_static_get_value(mf, mode_vals[i]);
    }
    Il2CppClass *action_class = il2cpp_find_class("FM.GamePlugin.dll", "FM.GamePlugin", "ChannelAction");
    if (action_class) g_action_size = il2cpp_class_value_size(action_class, NULL);
    log_line("tap: modes AddNode=%d RemoveNode=%d CloseChannel=%d SetPropertyID=%d, ChannelAction size=%d",
             g_add_node_mode, g_remove_node_mode, g_close_mode, g_set_property_mode, g_action_size);
    g_typed_value_as_string = method(typed_value, "AsString", 0);
    if (!response || !event || !open || !tick || !actions || !g_typed_value_as_string) {
        log_line("tap: not installed (missing classes or methods)");
        return;
    }

    void *it = NULL;
    FieldInfo *f;
    while ((f = il2cpp_class_get_fields(receiver, &it)))
    {
        if (!strcmp(il2cpp_field_get_name(f), "m_resusableTypedList")) g_receiver_list_offset = il2cpp_field_get_offset(f);
        if (!strcmp(il2cpp_field_get_name(f), "m_onChannelDataChange")) g_receiver_callback_offset = il2cpp_field_get_offset(f);
    }
    learn_span_layout(response);
    if (g_span.len_off < 0 || (g_span.ptr_off < 0 && (g_span.pinnable_off < 0 || g_span.byte_off < 0)) ||
        (g_span.size <= 16 && g_span.ptr_off < 0) || !g_receiver_list_offset) {
        log_line("tap: not installed (unexpected Span/receiver layout)");
        return;
    }

    g_tap = data_file("tap.log", "w");
    int n = patch_vtable(receiver, response, (void *)hook_response, &g_orig_response);
    n += patch_vtable(receiver, event, (void *)hook_event, &g_orig_event);
    n += patch_vtable(wrapper, open, (void *)hook_open, &g_orig_open);
    n += patch_vtable(wrapper, actions, (void *)hook_actions, &g_orig_actions);  // also used to close our channels
    if (g_orig_actions && g_close_mode >= 0 && g_receiver_callback_offset)
        n += patch_vtable(wrapper, tick, (void *)hook_tick, &g_orig_tick);
    else
        log_line("tap: probe disabled (close action or receiver callback not found)");
    log_line("tap: installed (%d vtable slots)", n);
}

#else
void tap_install(void) { log_line("tap: only supported on Apple Silicon"); }
#endif
