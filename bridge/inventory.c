// Writes classes.tsv (every class) and members.txt (fields and method
// signatures of FM's own FM.* / SI.* assemblies).
#include "inventory.h"

#include <string.h>

#include "il2cpp_api.h"
#include "log.h"

static int is_fm_image(const char *name) {
    return strncmp(name, "FM.", 3) == 0 || strncmp(name, "SI.", 3) == 0;
}

static void dump_members(FILE *out, const Il2CppClass *k) {
    // Open generic definitions (e.g. List`1) cannot be laid out; asking for
    // their fields makes the runtime abort.
    if (il2cpp_class_is_generic(k) || strchr(il2cpp_class_get_name(k), '`')) {
        fprintf(out, "    (generic definition, members skipped)\n");
        return;
    }
    char tn[512];
    void *it = NULL;
    FieldInfo *f;
    while ((f = il2cpp_class_get_fields(k, &it))) {
        il2cpp_type_name(il2cpp_field_get_type(f), tn, sizeof tn);
        if (il2cpp_field_get_flags(f) & FIELD_ATTRIBUTE_STATIC)
            fprintf(out, "    static %s %s\n", tn, il2cpp_field_get_name(f));
        else
            fprintf(out, "    [0x%zx] %s %s\n", il2cpp_field_get_offset(f), tn, il2cpp_field_get_name(f));
    }
    it = NULL;
    const MethodInfo *m;
    while ((m = il2cpp_class_get_methods(k, &it))) {
        il2cpp_type_name(il2cpp_method_get_return_type(m), tn, sizeof tn);
        uint32_t flags = il2cpp_method_get_flags(m, NULL);
        fprintf(out, "    %s%s%s %s(", (flags & METHOD_ATTRIBUTE_STATIC) ? "static " : "",
                (flags & METHOD_ATTRIBUTE_VIRTUAL) ? "virtual " : "", tn, il2cpp_method_get_name(m));
        uint32_t np = il2cpp_method_get_param_count(m);
        for (uint32_t i = 0; i < np; i++) {
            il2cpp_type_name(il2cpp_method_get_param(m, i), tn, sizeof tn);
            fprintf(out, "%s%s", i ? ", " : "", tn);
        }
        fputs(")\n", out);
    }
}

void inventory_dump(void) {
    size_t n = 0;
    const Il2CppAssembly **assemblies = il2cpp_domain_get_assemblies(il2cpp_domain_get(), &n);
    FILE *classes = data_file("classes.tsv", "w");
    FILE *members = data_file("members.txt", "w");
    if (!classes || !members) {
        log_line("inventory: cannot open output files in %s", g_data_dir);
        if (classes) fclose(classes);
        if (members) fclose(members);
        return;
    }
    size_t total = 0, detailed = 0;
    for (size_t a = 0; a < n; a++) {
        const Il2CppImage *img = il2cpp_assembly_get_image(assemblies[a]);
        const char *img_name = il2cpp_image_get_name(img);
        size_t count = il2cpp_image_get_class_count(img);
        int detail = is_fm_image(img_name);
        total += count;
        for (size_t c = 0; c < count; c++) {
            const Il2CppClass *k = il2cpp_image_get_class(img, c);
            const char *ns = il2cpp_class_get_namespace(k);
            const Il2CppClass *parent = il2cpp_class_get_parent(k);
            fprintf(classes, "%s\t%s\t%s\t%s\n", img_name, ns ? ns : "", il2cpp_class_get_name(k),
                    parent ? il2cpp_class_get_name(parent) : "");
            if (detail) {
                fprintf(members, "%s.%s  : %s  [%s]\n", ns && *ns ? ns : "-", il2cpp_class_get_name(k),
                        parent ? il2cpp_class_get_name(parent) : "-", img_name);
                dump_members(members, k);
                detailed++;
            }
        }
        fflush(members);  // keep partial output if something goes wrong mid-dump
    }
    fclose(classes);
    fclose(members);
    log_line("inventory: %zu assemblies, %zu classes, %zu with members", n, total, detailed);
}
