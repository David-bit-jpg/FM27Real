// FM27Real bridge — stage 0.
//
// Injected into Football Manager 27 (macOS) with DYLD_INSERT_LIBRARIES.
// Waits for the IL2CPP runtime to finish booting, then writes an inventory of
// the game's classes (and the fields/methods of FM's own assemblies) so later
// stages can locate game data by name.
//
// Read-only: it never touches game objects or save files.
//
// Output folder: ~/Library/Application Support/Sports Interactive/Football Manager 27/FM27Real/
//   bridge.log    progress and diagnostics
//   classes.tsv   assembly <tab> namespace <tab> class <tab> parent
//   members.txt   fields (with offsets) and methods of FM.* / SI.* classes

#include <dlfcn.h>
#include <mach-o/dyld.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

typedef void Il2CppDomain, Il2CppAssembly, Il2CppImage, Il2CppClass, Il2CppThread,
    Il2CppType, FieldInfo, MethodInfo;

#define IL2CPP_API(ret, name, args) static ret(*p_##name) args;
IL2CPP_API(const Il2CppImage *, get_corlib, (void))
IL2CPP_API(Il2CppDomain *, domain_get, (void))
IL2CPP_API(const Il2CppAssembly **, domain_get_assemblies, (const Il2CppDomain *, size_t *))
IL2CPP_API(const Il2CppImage *, assembly_get_image, (const Il2CppAssembly *))
IL2CPP_API(const char *, image_get_name, (const Il2CppImage *))
IL2CPP_API(size_t, image_get_class_count, (const Il2CppImage *))
IL2CPP_API(const Il2CppClass *, image_get_class, (const Il2CppImage *, size_t))
IL2CPP_API(const char *, class_get_name, (const Il2CppClass *))
IL2CPP_API(const char *, class_get_namespace, (const Il2CppClass *))
IL2CPP_API(Il2CppClass *, class_get_parent, (const Il2CppClass *))
IL2CPP_API(bool, class_is_generic, (const Il2CppClass *))
IL2CPP_API(FieldInfo *, class_get_fields, (const Il2CppClass *, void **))
IL2CPP_API(const MethodInfo *, class_get_methods, (const Il2CppClass *, void **))
IL2CPP_API(const char *, field_get_name, (const FieldInfo *))
IL2CPP_API(const Il2CppType *, field_get_type, (const FieldInfo *))
IL2CPP_API(size_t, field_get_offset, (const FieldInfo *))
IL2CPP_API(int, field_get_flags, (const FieldInfo *))
IL2CPP_API(char *, type_get_name, (const Il2CppType *))
IL2CPP_API(void, free, (void *))
IL2CPP_API(const char *, method_get_name, (const MethodInfo *))
IL2CPP_API(uint32_t, method_get_param_count, (const MethodInfo *))
IL2CPP_API(const Il2CppType *, method_get_return_type, (const MethodInfo *))
IL2CPP_API(Il2CppThread *, thread_attach, (Il2CppDomain *))
IL2CPP_API(void, thread_detach, (Il2CppThread *))

#define FIELD_ATTRIBUTE_STATIC 0x0010

static char g_dir[1024];
static FILE *g_log;
static pthread_mutex_t g_log_lock = PTHREAD_MUTEX_INITIALIZER;
static char g_game_assembly[1024];
static atomic_bool g_game_assembly_seen;

static void logf_(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void logf_(const char *fmt, ...) {
    if (!g_log) return;
    pthread_mutex_lock(&g_log_lock);
    time_t t = time(NULL);
    char ts[32];
    strftime(ts, sizeof ts, "%H:%M:%S", localtime(&t));
    fprintf(g_log, "[%s] ", ts);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fputc('\n', g_log);
    fflush(g_log);
    pthread_mutex_unlock(&g_log_lock);
}

// Called by dyld for every image, including ones already loaded. Runs under
// the dyld lock, so it only records the path; the worker does the dlopen.
static void on_image_added(const struct mach_header *mh, intptr_t slide) {
    (void)slide;
    if (atomic_load(&g_game_assembly_seen)) return;
    Dl_info info;
    if (!dladdr(mh, &info) || !info.dli_fname) return;
    const char *base = strrchr(info.dli_fname, '/');
    if (base && strcmp(base + 1, "GameAssembly.dylib") == 0) {
        strlcpy(g_game_assembly, info.dli_fname, sizeof g_game_assembly);
        atomic_store(&g_game_assembly_seen, true);
    }
}

static void on_exit_(void) { logf_("game process exiting (exit() called)"); }

static int resolve_all(void *h) {
    int missing = 0;
#define R(name)                                                              \
    p_##name = (__typeof__(p_##name))dlsym(h, "il2cpp_" #name);              \
    if (!p_##name) { logf_("missing export il2cpp_%s", #name); missing++; }
    R(get_corlib) R(domain_get) R(domain_get_assemblies) R(assembly_get_image)
    R(image_get_name) R(image_get_class_count) R(image_get_class) R(class_get_name)
    R(class_get_namespace) R(class_get_parent) R(class_is_generic) R(class_get_fields) R(class_get_methods)
    R(field_get_name) R(field_get_type) R(field_get_offset) R(field_get_flags)
    R(type_get_name) R(free) R(method_get_name) R(method_get_param_count)
    R(method_get_return_type) R(thread_attach) R(thread_detach)
#undef R
    return missing;
}

static void type_name(const Il2CppType *t, char *buf, size_t n) {
    char *s = t ? p_type_get_name(t) : NULL;
    strlcpy(buf, s ? s : "?", n);
    if (s) p_free(s);  // allocated by il2cpp's allocator; system free() aborts
}

static int is_fm_image(const char *name) {
    return strncmp(name, "FM.", 3) == 0 || strncmp(name, "SI.", 3) == 0;
}

static void dump_members(FILE *out, const Il2CppClass *k) {
    // Open generic definitions (e.g. List`1) cannot be laid out; asking for
    // their fields makes the runtime abort. Skip them.
    if (p_class_is_generic(k) || strchr(p_class_get_name(k), '`')) {
        fprintf(out, "    (generic definition, members skipped)\n");
        return;
    }
    char tn[512];
    void *it = NULL;
    FieldInfo *f;
    while ((f = p_class_get_fields(k, &it))) {
        type_name(p_field_get_type(f), tn, sizeof tn);
        if (p_field_get_flags(f) & FIELD_ATTRIBUTE_STATIC)
            fprintf(out, "    static %s %s\n", tn, p_field_get_name(f));
        else
            fprintf(out, "    [0x%zx] %s %s\n", p_field_get_offset(f), tn, p_field_get_name(f));
    }
    it = NULL;
    const MethodInfo *m;
    while ((m = p_class_get_methods(k, &it))) {
        type_name(p_method_get_return_type(m), tn, sizeof tn);
        fprintf(out, "    %s %s(%u)\n", tn, p_method_get_name(m), p_method_get_param_count(m));
    }
}

static void dump_inventory(void) {
    Il2CppDomain *domain = p_domain_get();
    Il2CppThread *thread = p_thread_attach(domain);
    size_t n = 0;
    const Il2CppAssembly **assemblies = p_domain_get_assemblies(domain, &n);

    char path[1200];
    snprintf(path, sizeof path, "%s/classes.tsv", g_dir);
    FILE *classes = fopen(path, "w");
    snprintf(path, sizeof path, "%s/members.txt", g_dir);
    FILE *members = fopen(path, "w");
    if (!classes || !members) {
        logf_("cannot open output files in %s", g_dir);
        if (classes) fclose(classes);
        if (members) fclose(members);
        p_thread_detach(thread);
        return;
    }

    size_t total = 0, detailed = 0;
    for (size_t a = 0; a < n; a++) {
        const Il2CppImage *img = p_assembly_get_image(assemblies[a]);
        const char *img_name = p_image_get_name(img);
        size_t count = p_image_get_class_count(img);
        int detail = is_fm_image(img_name);
        logf_("  %-40s %6zu classes%s", img_name, count, detail ? "  (members dumped)" : "");
        total += count;
        for (size_t c = 0; c < count; c++) {
            const Il2CppClass *k = p_image_get_class(img, c);
            const char *ns = p_class_get_namespace(k);
            const Il2CppClass *parent = p_class_get_parent(k);
            fprintf(classes, "%s\t%s\t%s\t%s\n", img_name, ns ? ns : "", p_class_get_name(k),
                    parent ? p_class_get_name(parent) : "");
            if (detail) {
                fprintf(members, "%s.%s  : %s  [%s]\n", ns && *ns ? ns : "-", p_class_get_name(k),
                        parent ? p_class_get_name(parent) : "-", img_name);
                dump_members(members, k);
                detailed++;
            }
        }
        fflush(members);  // keep partial output if something goes wrong mid-dump
    }
    fclose(classes);
    fclose(members);
    logf_("inventory done: %zu assemblies, %zu classes, %zu with members", n, total, detailed);
    p_thread_detach(thread);
}

static void *worker(void *arg) {
    (void)arg;
    // 1. Wait for UnityPlayer to load GameAssembly.dylib (up to 10 minutes).
    for (int i = 0; i < 1200 && !atomic_load(&g_game_assembly_seen); i++) usleep(500000);
    if (!atomic_load(&g_game_assembly_seen)) { logf_("GameAssembly never loaded; giving up"); return NULL; }
    void *h = dlopen(g_game_assembly, RTLD_NOLOAD | RTLD_LAZY);
    if (!h) { logf_("dlopen(NOLOAD) failed: %s", dlerror()); return NULL; }
    logf_("GameAssembly loaded: %s", g_game_assembly);
    if (resolve_all(h)) { logf_("giving up: il2cpp exports missing"); return NULL; }

    // 2. il2cpp_get_corlib only reads a global set during runtime init, so it is
    //    safe to poll. Calling domain/assembly APIs before that could create a
    //    second domain.
    int waited = 0;
    while (!p_get_corlib()) {
        usleep(250000);
        if (++waited > 2400) { logf_("runtime never initialised; giving up"); return NULL; }
    }
    logf_("runtime initialised (corlib after %.1fs); letting the game settle", waited / 4.0);

    // 3. Wait until the assembly list stops growing.
    size_t last = 0, stable = 0;
    for (int i = 0; i < 240 && stable < 10; i++) {
        size_t n = 0;
        p_domain_get_assemblies(p_domain_get(), &n);
        stable = (n == last && n > 0) ? stable + 1 : 0;
        last = n;
        usleep(500000);
    }
    logf_("%zu assemblies loaded; waiting 15s before dumping", last);
    sleep(15);
    dump_inventory();
    return NULL;
}

__attribute__((constructor)) static void fm27real_init(void) {
    const char *home = getenv("HOME");
    if (!home) return;
    snprintf(g_dir, sizeof g_dir, "%s/Library/Application Support/Sports Interactive/Football Manager 27/FM27Real", home);
    mkdir(g_dir, 0755);
    char path[1200];
    snprintf(path, sizeof path, "%s/bridge.log", g_dir);
    g_log = fopen(path, "w");
    logf_("FM27Real bridge injected into pid %d", getpid());
    atexit(on_exit_);
    _dyld_register_func_for_add_image(on_image_added);
    pthread_t t;
    if (pthread_create(&t, NULL, worker, NULL) == 0) pthread_detach(t);
}
