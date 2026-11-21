// FM27Real bridge — stage 0 proof of concept.
// Injected into Football Manager 27 (macOS) with DYLD_INSERT_LIBRARIES.
// Waits for the IL2CPP runtime, then writes a class inventory to a log.
// Read-only: it does not touch game objects or saves.
#include <dlfcn.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <stdarg.h>

typedef void Il2CppDomain;
typedef void Il2CppAssembly;
typedef void Il2CppImage;
typedef void Il2CppClass;
typedef void Il2CppThread;

static Il2CppDomain *(*p_domain_get)(void);
static const Il2CppAssembly **(*p_domain_get_assemblies)(const Il2CppDomain *, size_t *);
static const Il2CppImage *(*p_assembly_get_image)(const Il2CppAssembly *);
static const char *(*p_image_get_name)(const Il2CppImage *);
static size_t (*p_image_get_class_count)(const Il2CppImage *);
static const Il2CppClass *(*p_image_get_class)(const Il2CppImage *, size_t);
static const char *(*p_class_get_name)(const Il2CppClass *);
static const char *(*p_class_get_namespace)(const Il2CppClass *);
static Il2CppThread *(*p_thread_attach)(Il2CppDomain *);
static void (*p_thread_detach)(Il2CppThread *);

static char g_dir[1024];
static FILE *g_log;

static void logf_(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void logf_(const char *fmt, ...) {
    if (!g_log) return;
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
}

#define RESOLVE(var, name) (var = (__typeof__(var))dlsym(RTLD_DEFAULT, name))

static void *worker(void *arg) {
    (void)arg;
    // 1. Wait for UnityPlayer to load GameAssembly.dylib.
    for (int i = 0; i < 600 && !RESOLVE(p_domain_get, "il2cpp_domain_get"); i++) sleep(1);
    if (!p_domain_get) { logf_("GameAssembly never loaded; giving up"); return NULL; }
    logf_("GameAssembly loaded; waiting for runtime init");

    RESOLVE(p_domain_get_assemblies, "il2cpp_domain_get_assemblies");
    RESOLVE(p_assembly_get_image, "il2cpp_assembly_get_image");
    RESOLVE(p_image_get_name, "il2cpp_image_get_name");
    RESOLVE(p_image_get_class_count, "il2cpp_image_get_class_count");
    RESOLVE(p_image_get_class, "il2cpp_image_get_class");
    RESOLVE(p_class_get_name, "il2cpp_class_get_name");
    RESOLVE(p_class_get_namespace, "il2cpp_class_get_namespace");
    RESOLVE(p_thread_attach, "il2cpp_thread_attach");
    RESOLVE(p_thread_detach, "il2cpp_thread_detach");
    if (!p_domain_get_assemblies || !p_assembly_get_image || !p_image_get_name || !p_image_get_class_count ||
        !p_image_get_class || !p_class_get_name || !p_class_get_namespace || !p_thread_attach || !p_thread_detach) {
        logf_("missing il2cpp exports; giving up");
        return NULL;
    }

    // 2. il2cpp has no exported "is initialized" check; calling into it too early can
    //    create a second domain. Give the game time to finish booting first.
    sleep(45);

    Il2CppDomain *domain = p_domain_get();
    Il2CppThread *thread = p_thread_attach(domain);
    size_t n = 0;
    const Il2CppAssembly **assemblies = p_domain_get_assemblies(domain, &n);
    logf_("runtime ready: %zu assemblies", n);

    char path[1200];
    snprintf(path, sizeof path, "%s/classes.txt", g_dir);
    FILE *out = fopen(path, "w");
    size_t total = 0;
    for (size_t a = 0; a < n; a++) {
        const Il2CppImage *img = p_assembly_get_image(assemblies[a]);
        const char *img_name = p_image_get_name(img);
        size_t count = p_image_get_class_count(img);
        total += count;
        logf_("  %s: %zu classes", img_name, count);
        if (!out) continue;
        for (size_t c = 0; c < count; c++) {
            const Il2CppClass *k = p_image_get_class(img, c);
            const char *ns = p_class_get_namespace(k);
            fprintf(out, "%s\t%s%s%s\n", img_name, ns && *ns ? ns : "", ns && *ns ? "." : "", p_class_get_name(k));
        }
    }
    if (out) fclose(out);
    logf_("wrote %zu classes to %s", total, path);
    p_thread_detach(thread);
    return NULL;
}

__attribute__((constructor)) static void fm27real_init(void) {
    const char *home = getenv("HOME");
    if (!home) return;
    snprintf(g_dir, sizeof g_dir, "%s/Library/Application Support/Sports Interactive/Football Manager 27/FM27Real", home);
    mkdir(g_dir, 0755);
    char path[1200];
    snprintf(path, sizeof path, "%s/loader.log", g_dir);
    g_log = fopen(path, "w");
    logf_("FM27Real bridge injected into pid %d", getpid());
    pthread_t t;
    if (pthread_create(&t, NULL, worker, NULL) == 0) pthread_detach(t);
}
