// Property catalog: every UI reference type in FM (PersonReference,
// ClubReference, ...) can list its properties and describe them through static
// methods. This writes them all to catalog.tsv:
//
//   type <tab> type-id <tab> property code <tab> property id <tab> kind <tab> list
//        <tab> referenced type-id <tab> value type <tab> description
#include "catalog.h"

#include <string.h>

#include "il2cpp_api.h"
#include "log.h"

static const MethodInfo *g_object_to_string;

static void fourcc(uint32_t v, char out[16]) {
    char c[4] = {(char)(v & 0xff), (char)(v >> 8), (char)(v >> 16), (char)(v >> 24)};
    for (int i = 0; i < 4; i++)
        if (c[i] < 0x20 || c[i] > 0x7e) { snprintf(out, 16, "#%08x", v); return; }
    snprintf(out, 16, "%c%c%c%c", c[0], c[1], c[2], c[3]);
}

static Il2CppObject *invoke(const MethodInfo *m, void *obj, void **args) {
    Il2CppException *exc = NULL;
    Il2CppObject *r = il2cpp_runtime_invoke(m, obj, args, &exc);
    return exc ? NULL : r;
}

static void object_string(Il2CppObject *o, char *buf, size_t n) {
    if (!o) { strlcpy(buf, "", n); return; }
    const MethodInfo *m = il2cpp_object_get_virtual_method(o, g_object_to_string);
    il2cpp_utf8((Il2CppString *)invoke(m ? m : g_object_to_string, o, NULL), buf, n);
}

static void clean(char *s) {
    for (; *s; s++) if (*s == '\t' || *s == '\n' || *s == '\r') *s = ' ';
}

// Returns property ids via the type's static GetPropertiesInternal(List<PropertyID>).
static int list_properties(const MethodInfo *get_props, uint32_t *out, int cap) {
    Il2CppClass *list_class = il2cpp_class_from_type(il2cpp_method_get_param(get_props, 0));
    Il2CppObject *list = il2cpp_object_new(list_class);
    const MethodInfo *ctor = il2cpp_class_get_method_from_name(list_class, ".ctor", 0);
    if (!list || !ctor) return 0;
    invoke(ctor, list, NULL);
    void *args[1] = {list};
    Il2CppException *exc = NULL;
    il2cpp_runtime_invoke(get_props, NULL, args, &exc);
    if (exc) return 0;
    size_t items_off = 0, size_off = 0;
    void *it = NULL;
    FieldInfo *f;
    while ((f = il2cpp_class_get_fields(list_class, &it))) {
        if (!strcmp(il2cpp_field_get_name(f), "_items")) items_off = il2cpp_field_get_offset(f);
        if (!strcmp(il2cpp_field_get_name(f), "_size")) size_off = il2cpp_field_get_offset(f);
    }
    if (!items_off || !size_off) return 0;
    uint8_t *arr = *(uint8_t **)((uint8_t *)list + items_off);
    int32_t n = *(int32_t *)((uint8_t *)list + size_off);
    if (!arr || n <= 0) return 0;
    if (n > cap) n = cap;
    memcpy(out, arr + 0x20, sizeof(uint32_t) * (size_t)n);  // PropertyID is a 4-byte struct
    return n;
}

static int catalog_class(FILE *out, const Il2CppClass *k) {
    const MethodInfo *get_props = il2cpp_class_get_method_from_name(k, "GetPropertiesInternal", 1);
    const MethodInfo *describe = il2cpp_class_get_method_from_name(k, "GetPropertyDescriptionInternal", 1);
    const MethodInfo *type_of = il2cpp_class_get_method_from_name(k, "GetPropertyTypeInternal", 1);
    const MethodInfo *identifier = il2cpp_class_get_method_from_name(k, "get_Identifier", 0);
    if (!get_props || !describe || !type_of) return 0;
    if (!(il2cpp_method_get_flags(get_props, NULL) & METHOD_ATTRIBUTE_STATIC)) return 0;

    char name[256];
    const char *ns = il2cpp_class_get_namespace(k);
    snprintf(name, sizeof name, "%s%s%s", ns && *ns ? ns : "", ns && *ns ? "." : "", il2cpp_class_get_name(k));
    fprintf(out, "# %s\n", name);  // progress marker: shows the culprit if a type crashes
    fflush(out);

    uint32_t type_id = 0;
    Il2CppObject *boxed_id = identifier ? invoke(identifier, NULL, NULL) : NULL;
    if (boxed_id) memcpy(&type_id, (uint8_t *)boxed_id + 0x10, 4);

    static uint32_t props[8192];
    int n = list_properties(get_props, props, 8192);
    for (int i = 0; i < n; i++) {
        uint32_t id = props[i];
        void *args[1] = {&id};
        char desc[512], vtype[256], code[16];
        il2cpp_utf8((Il2CppString *)invoke(describe, NULL, args), desc, sizeof desc);
        clean(desc);
        // BindingKind { Kind @0x10, ReferenceID @0x14, System.Type @0x18, IsList @0x20 } (boxed)
        Il2CppObject *kind = invoke(type_of, NULL, args);
        int32_t kind_v = 0;
        uint32_t ref_id = 0;
        uint8_t is_list = 0;
        Il2CppObject *type_obj = NULL;
        if (kind) {
            memcpy(&kind_v, (uint8_t *)kind + 0x10, 4);
            memcpy(&ref_id, (uint8_t *)kind + 0x14, 4);
            memcpy(&type_obj, (uint8_t *)kind + 0x18, sizeof type_obj);
            memcpy(&is_list, (uint8_t *)kind + 0x20, 1);
        }
        object_string(type_obj, vtype, sizeof vtype);
        clean(vtype);
        fourcc(id, code);
        fprintf(out, "%s\t%08x\t%s\t%08x\t%s\t%s\t%08x\t%s\t%s\n", name, type_id, code, id,
                kind_v == 1 ? "value" : kind_v == 2 ? "reference" : "invalid", is_list ? "list" : "", ref_id,
                vtype, desc);
    }
    return n;
}

int catalog_properties(const char *ns, const char *name, uint32_t *out, int cap) {
    Il2CppClass *k = il2cpp_find_class("FM.UI.dll", ns, name);
    const MethodInfo *get_props = k ? il2cpp_class_get_method_from_name(k, "GetPropertiesInternal", 1) : NULL;
    return get_props ? list_properties(get_props, out, cap) : 0;
}

void catalog_dump(void) {
    Il2CppClass *object_class = il2cpp_find_class("mscorlib.dll", "System", "Object");
    g_object_to_string = object_class ? il2cpp_class_get_method_from_name(object_class, "ToString", 0) : NULL;
    if (!g_object_to_string) { log_line("catalog: System.Object.ToString not found"); return; }

    FILE *out = data_file("catalog.tsv", "w");
    if (!out) { log_line("catalog: cannot open catalog.tsv"); return; }
    size_t n_assemblies = 0, types = 0, props = 0;
    const Il2CppAssembly **assemblies = il2cpp_domain_get_assemblies(il2cpp_domain_get(), &n_assemblies);
    for (size_t a = 0; a < n_assemblies; a++) {
        const Il2CppImage *img = il2cpp_assembly_get_image(assemblies[a]);
        const char *img_name = il2cpp_image_get_name(img);
        if (strncmp(img_name, "FM.", 3) && strncmp(img_name, "SI.", 3)) continue;
        size_t count = il2cpp_image_get_class_count(img);
        for (size_t c = 0; c < count; c++) {
            const Il2CppClass *k = il2cpp_image_get_class(img, c);
            if (il2cpp_class_is_generic(k) || strchr(il2cpp_class_get_name(k), '`')) continue;
            int n = catalog_class(out, k);
            if (n > 0) { types++; props += (size_t)n; }
        }
    }
    fclose(out);
    log_line("catalog: %zu reference types, %zu properties", types, props);
}
