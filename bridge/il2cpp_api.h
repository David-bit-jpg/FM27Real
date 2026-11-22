// IL2CPP runtime API, resolved at runtime from GameAssembly.dylib.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef void Il2CppDomain, Il2CppAssembly, Il2CppImage, Il2CppClass, Il2CppThread,
    Il2CppType, FieldInfo, MethodInfo, Il2CppObject, Il2CppString, Il2CppException;

// X(return type, name, parameter list)
#define IL2CPP_FUNCS(X)                                                                        \
    X(const Il2CppImage *, get_corlib, (void))                                                 \
    X(Il2CppDomain *, domain_get, (void))                                                      \
    X(const Il2CppAssembly **, domain_get_assemblies, (const Il2CppDomain *, size_t *))        \
    X(const Il2CppImage *, assembly_get_image, (const Il2CppAssembly *))                       \
    X(const char *, image_get_name, (const Il2CppImage *))                                     \
    X(size_t, image_get_class_count, (const Il2CppImage *))                                    \
    X(const Il2CppClass *, image_get_class, (const Il2CppImage *, size_t))                     \
    X(Il2CppClass *, class_from_name, (const Il2CppImage *, const char *, const char *))       \
    X(const char *, class_get_name, (const Il2CppClass *))                                     \
    X(const char *, class_get_namespace, (const Il2CppClass *))                                \
    X(Il2CppClass *, class_get_parent, (const Il2CppClass *))                                  \
    X(bool, class_is_generic, (const Il2CppClass *))                                           \
    X(FieldInfo *, class_get_fields, (const Il2CppClass *, void **))                           \
    X(const MethodInfo *, class_get_methods, (const Il2CppClass *, void **))                   \
    X(const MethodInfo *, class_get_method_from_name, (const Il2CppClass *, const char *, int)) \
    X(Il2CppClass *, class_from_type, (const Il2CppType *))                                    \
    X(Il2CppClass *, class_get_element_class, (Il2CppClass *))                                 \
    X(int, class_array_element_size, (const Il2CppClass *))                                    \
    X(int32_t, class_value_size, (const Il2CppClass *, uint32_t *))                            \
    X(const char *, field_get_name, (const FieldInfo *))                                       \
    X(const Il2CppType *, field_get_type, (const FieldInfo *))                                 \
    X(size_t, field_get_offset, (const FieldInfo *))                                           \
    X(int, field_get_flags, (const FieldInfo *))                                               \
    X(char *, type_get_name, (const Il2CppType *))                                             \
    X(void, free, (void *))                                                                    \
    X(const char *, method_get_name, (const MethodInfo *))                                     \
    X(uint32_t, method_get_param_count, (const MethodInfo *))                                  \
    X(const Il2CppType *, method_get_param, (const MethodInfo *, uint32_t))                    \
    X(const Il2CppType *, method_get_return_type, (const MethodInfo *))                        \
    X(uint32_t, method_get_flags, (const MethodInfo *, uint32_t *))                            \
    X(Il2CppObject *, runtime_invoke, (const MethodInfo *, void *, void **, Il2CppException **)) \
    X(const MethodInfo *, object_get_virtual_method, (Il2CppObject *, const MethodInfo *))     \
    X(Il2CppClass *, object_get_class, (Il2CppObject *))                                       \
    X(Il2CppObject *, value_box, (Il2CppClass *, void *))                                      \
    X(Il2CppObject *, object_new, (const Il2CppClass *))                                       \
    X(const Il2CppType *, class_get_type, (Il2CppClass *))                                     \
    X(Il2CppObject *, type_get_object, (const Il2CppType *))                                   \
    X(FieldInfo *, class_get_field_from_name, (const Il2CppClass *, const char *))             \
    X(void, field_static_get_value, (FieldInfo *, void *))                                     \
    X(uint16_t *, string_chars, (Il2CppString *))                                              \
    X(int32_t, string_length, (Il2CppString *))                                                \
    X(Il2CppThread *, thread_attach, (Il2CppDomain *))                                         \
    X(void, thread_detach, (Il2CppThread *))

#define IL2CPP_DECLARE(ret, name, args) extern ret(*il2cpp_##name) args;
IL2CPP_FUNCS(IL2CPP_DECLARE)
#undef IL2CPP_DECLARE

#define FIELD_ATTRIBUTE_STATIC 0x0010
#define METHOD_ATTRIBUTE_STATIC 0x0010
#define METHOD_ATTRIBUTE_VIRTUAL 0x0040

// Resolves every function above from an open GameAssembly handle. Returns the
// number of missing exports (0 on success).
int il2cpp_resolve(void *handle);

// Finds a loaded image by file name, e.g. "FM.GamePlugin.dll".
const Il2CppImage *il2cpp_find_image(const char *name);

// Finds a class in an image by (possibly empty) namespace and name. Unlike
// il2cpp_class_from_name this also finds nested types.
Il2CppClass *il2cpp_find_class(const char *image, const char *ns, const char *name);

// Writes a type's display name into buf.
void il2cpp_type_name(const Il2CppType *t, char *buf, size_t n);

// Converts a managed string to UTF-8. Returns buf.
char *il2cpp_utf8(Il2CppString *s, char *buf, size_t n);
