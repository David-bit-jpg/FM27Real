// Platform layer: the bridge is the same code on macOS (dylib injected with
// DYLD_INSERT_LIBRARIES) and Windows (DLL injected by Windows/launch.ps1).
// Everything that differs between the two lives here.
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <setjmp.h>

// Span<T> (16 bytes) is passed by pointer in the Windows x64 calling convention.
#define SPAN_SLOTS 1

// Fault guard: a jump buffer that survives being entered from the exception
// handler's trampoline (no SEH unwinding involved).
typedef void *guard_buf[5];
#define GUARD_SET(b) __builtin_setjmp(b)
#define GUARD_JUMP(b) __builtin_longjmp(b, 1)

static inline size_t strlcpy(char *dst, const char *src, size_t n) {
    size_t len = strlen(src);
    if (n) {
        size_t c = len < n - 1 ? len : n - 1;
        memcpy(dst, src, c);
        dst[c] = 0;
    }
    return len;
}
static inline size_t strlcat(char *dst, const char *src, size_t n) {
    size_t d = strnlen(dst, n);
    if (d == n) return n + strlen(src);
    return d + strlcpy(dst + d, src, n - d);
}
static inline char *strcasestr(const char *h, const char *n) {
    size_t ln = strlen(n);
    for (; *h; h++)
        if (_strnicmp(h, n, ln) == 0) return (char *)h;
    return ln ? NULL : (char *)h;
}
#include <process.h>

#else
#include <setjmp.h>

// Span<T> arrives as two registers (pointer, length) on arm64 and x86-64 SysV.
#define SPAN_SLOTS 2

typedef sigjmp_buf guard_buf;
#define GUARD_SET(b) sigsetjmp(b, 1)
#define GUARD_JUMP(b) siglongjmp(b, 1)
#endif

// Index of an argument in a hook's argument list, given how many plain
// (register-sized) arguments and how many spans come before it.
#define ARGPOS(plain, spans) ((plain) + (spans) * SPAN_SLOTS)

// Reads n bytes at addr without faulting; 1 on success.
int platform_safe_read(const void *addr, void *out, size_t n);
// Makes one pointer-sized slot writable (vtable patching).
void platform_make_writable(void *slot);
// 16 random bytes for the API token.
void platform_random(uint8_t *out, size_t n);
