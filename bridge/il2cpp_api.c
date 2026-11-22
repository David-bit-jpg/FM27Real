#include "il2cpp_api.h"

#if defined(_WIN32)
#include <windows.h>
#define dlsym(h, name) ((void *)GetProcAddress((HMODULE)(h), name))
#else
#include <dlfcn.h>
#endif
#include <stdlib.h>
#include <string.h>

#include "log.h"

#define IL2CPP_DEFINE(ret, name, args) ret(*il2cpp_##name) args;
IL2CPP_FUNCS(IL2CPP_DEFINE)
#undef IL2CPP_DEFINE

int il2cpp_resolve(void *handle) {
    int missing = 0;
#define IL2CPP_RESOLVE(ret, name, args)                                         \
    il2cpp_##name = (__typeof__(il2cpp_##name))dlsym(handle, "il2cpp_" #name);  \
    if (!il2cpp_##name) {                                                       \
        log_line("missing export il2cpp_%s", #name);                            \
        missing++;                                                              \
    }
    IL2CPP_FUNCS(IL2CPP_RESOLVE)
#undef IL2CPP_RESOLVE
    return missing;
}

const Il2CppImage *il2cpp_find_image(const char *name) {
    size_t n = 0;
    const Il2CppAssembly **assemblies = il2cpp_domain_get_assemblies(il2cpp_domain_get(), &n);
    for (size_t i = 0; i < n; i++) {
        const Il2CppImage *img = il2cpp_assembly_get_image(assemblies[i]);
        if (strcmp(il2cpp_image_get_name(img), name) == 0) return img;
    }
    return NULL;
}

Il2CppClass *il2cpp_find_class(const char *image, const char *ns, const char *name) {
    const Il2CppImage *img = il2cpp_find_image(image);
    if (!img) return NULL;
    Il2CppClass *k = il2cpp_class_from_name(img, ns, name);
    if (k) return k;
    // Nested types are not reachable through class_from_name; scan instead.
    size_t count = il2cpp_image_get_class_count(img);
    for (size_t i = 0; i < count; i++) {
        const Il2CppClass *c = il2cpp_image_get_class(img, i);
        if (strcmp(il2cpp_class_get_name(c), name) == 0) return (Il2CppClass *)c;
    }
    return NULL;
}

void il2cpp_type_name(const Il2CppType *t, char *buf, size_t n) {
    char *s = t ? il2cpp_type_get_name(t) : NULL;
    strlcpy(buf, s ? s : "?", n);
    if (s) il2cpp_free(s);  // allocated by il2cpp; the system free() aborts
}

char *il2cpp_utf8(Il2CppString *s, char *buf, size_t n) {
    size_t o = 0;
    if (s && n) {
        const uint16_t *c = il2cpp_string_chars(s);
        int32_t len = il2cpp_string_length(s);
        for (int32_t i = 0; i < len && o + 4 < n; i++) {
            uint32_t cp = c[i];
            if (cp >= 0xD800 && cp < 0xDC00 && i + 1 < len) {  // surrogate pair
                cp = 0x10000 + ((cp - 0xD800) << 10) + (c[++i] - 0xDC00);
            }
            if (cp < 0x80) {
                buf[o++] = (char)cp;
            } else if (cp < 0x800) {
                buf[o++] = (char)(0xC0 | (cp >> 6));
                buf[o++] = (char)(0x80 | (cp & 0x3F));
            } else if (cp < 0x10000) {
                buf[o++] = (char)(0xE0 | (cp >> 12));
                buf[o++] = (char)(0x80 | ((cp >> 6) & 0x3F));
                buf[o++] = (char)(0x80 | (cp & 0x3F));
            } else {
                buf[o++] = (char)(0xF0 | (cp >> 18));
                buf[o++] = (char)(0x80 | ((cp >> 12) & 0x3F));
                buf[o++] = (char)(0x80 | ((cp >> 6) & 0x3F));
                buf[o++] = (char)(0x80 | (cp & 0x3F));
            }
        }
    }
    if (n) buf[o < n ? o : n - 1] = 0;
    return buf;
}
