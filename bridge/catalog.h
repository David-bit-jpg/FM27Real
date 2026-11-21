#pragma once

// Writes catalog.tsv: every reference type's properties with descriptions.
// Call on an attached thread.
void catalog_dump(void);

#include <stdint.h>
// Lists the property ids of an FM.UI reference type (e.g. "FM.UI", "PersonReference").
int catalog_properties(const char *ns, const char *name, uint32_t *out, int cap);
