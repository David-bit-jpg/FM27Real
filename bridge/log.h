// Logging and output files under the FM27Real data folder.
#pragma once
#include <stdio.h>

// ~/Library/Application Support/Sports Interactive/Football Manager 27/FM27Real
extern char g_data_dir[1024];

void log_open(void);
void log_line(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

// Opens a file in the data folder ("w" or "a").
FILE *data_file(const char *name, const char *mode);
