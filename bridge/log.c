#include "log.h"

#include <pthread.h>
#include <stdarg.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <time.h>

char g_data_dir[1024];
static FILE *g_log;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;

void log_open(void) {
    const char *home = getenv("HOME");
    if (!home) return;
    snprintf(g_data_dir, sizeof g_data_dir,
             "%s/Library/Application Support/Sports Interactive/Football Manager 27/FM27Real", home);
    mkdir(g_data_dir, 0755);
    g_log = data_file("bridge.log", "w");
}

void log_line(const char *fmt, ...) {
    if (!g_log) return;
    pthread_mutex_lock(&g_lock);
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
    pthread_mutex_unlock(&g_lock);
}

FILE *data_file(const char *name, const char *mode) {
    char path[1200];
    snprintf(path, sizeof path, "%s/%s", g_data_dir, name);
    return fopen(path, mode);
}
