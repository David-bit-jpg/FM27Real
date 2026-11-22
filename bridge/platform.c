#include "platform.h"

#if defined(_WIN32)
#include <bcrypt.h>

int platform_safe_read(const void *addr, void *out, size_t n) {
    SIZE_T got = 0;
    return ReadProcessMemory(GetCurrentProcess(), addr, out, n, &got) && got == n;
}

void platform_make_writable(void *slot) {
    DWORD old;
    VirtualProtect(slot, 8, PAGE_READWRITE, &old);
}

void platform_random(uint8_t *out, size_t n) {
    if (BCryptGenRandom(NULL, out, (ULONG)n, BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0) {
        LARGE_INTEGER t;  // never expected; still better than a fixed token
        QueryPerformanceCounter(&t);
        for (size_t i = 0; i < n; i++) out[i] = (uint8_t)(t.QuadPart >> ((i % 8) * 8)) ^ (uint8_t)(i * 131);
    }
}

#else
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <stdlib.h>

int platform_safe_read(const void *addr, void *out, size_t n) {
    mach_vm_size_t got = 0;
    return mach_vm_read_overwrite(mach_task_self(), (mach_vm_address_t)addr, n, (mach_vm_address_t)out, &got) ==
               KERN_SUCCESS && got == n;
}

void platform_make_writable(void *slot) {
    mach_vm_protect(mach_task_self(), (mach_vm_address_t)slot, 8, 0, VM_PROT_READ | VM_PROT_WRITE);
}

void platform_random(uint8_t *out, size_t n) { arc4random_buf(out, n); }
#endif
