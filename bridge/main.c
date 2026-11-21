// FM27Real bridge — entry point.
//
// Injected into Football Manager 27 (macOS) with DYLD_INSERT_LIBRARIES (see
// scripts/launch.sh). Waits for the IL2CPP runtime to boot, optionally writes
// the class inventory, then installs the data tap.
//
// Output folder: ~/Library/Application Support/Sports Interactive/Football Manager 27/FM27Real/
#include <dlfcn.h>
#include <mach-o/dyld.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "il2cpp_api.h"
#include "catalog.h"
#include "http.h"
#include "inventory.h"
#include "log.h"
#include "overlay.h"
#include "tap.h"

static char g_game_assembly[1024];
static atomic_bool g_game_assembly_seen;

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

static void on_exit_(void) { log_line("game process exiting (exit() called)"); }

// True when FM27REAL_DUMP=1 or the file does not exist yet.
static int dump_wanted(const char *file) {
    const char *env = getenv("FM27REAL_DUMP");
    if (env && *env == '1') return 1;
    char path[1200];
    snprintf(path, sizeof path, "%s/%s", g_data_dir, file);
    struct stat st;
    return stat(path, &st) != 0;
}

static void *worker(void *arg) {
    (void)arg;
    for (int i = 0; i < 1200 && !atomic_load(&g_game_assembly_seen); i++) usleep(500000);
    if (!atomic_load(&g_game_assembly_seen)) { log_line("GameAssembly never loaded; giving up"); return NULL; }
    void *h = dlopen(g_game_assembly, RTLD_NOLOAD | RTLD_LAZY);
    if (!h) { log_line("dlopen(NOLOAD) failed: %s", dlerror()); return NULL; }
    if (il2cpp_resolve(h)) { log_line("giving up: il2cpp exports missing"); return NULL; }

    // il2cpp_get_corlib only reads a global set during runtime init, so it is
    // safe to poll; domain/assembly calls before that could create a second domain.
    for (int i = 0; !il2cpp_get_corlib(); i++) {
        if (i > 2400) { log_line("runtime never initialised; giving up"); return NULL; }
        usleep(250000);
    }
    // Wait until the assembly list stops growing, then let the game settle.
    size_t last = 0, stable = 0;
    for (int i = 0; i < 240 && stable < 10; i++) {
        size_t n = 0;
        il2cpp_domain_get_assemblies(il2cpp_domain_get(), &n);
        stable = (n == last && n > 0) ? stable + 1 : 0;
        last = n;
        usleep(500000);
    }
    log_line("runtime ready: %zu assemblies", last);
    sleep(10);

    Il2CppThread *thread = il2cpp_thread_attach(il2cpp_domain_get());
    if (dump_wanted("members.txt")) inventory_dump();
    if (dump_wanted("catalog.tsv")) catalog_dump();
    tap_install();
    http_start();
    overlay_install();
    il2cpp_thread_detach(thread);
    return NULL;
}

__attribute__((constructor)) static void fm27real_init(void) {
    log_open();
    log_line("FM27Real bridge injected into pid %d", getpid());
    atexit(on_exit_);
    _dyld_register_func_for_add_image(on_image_added);
    pthread_t t;
    if (pthread_create(&t, NULL, worker, NULL) == 0) pthread_detach(t);
}
