// Minimal JSON writer.
#pragma once
#include <stddef.h>

typedef struct {
    char *data;  // malloc'd, NUL-terminated
    size_t len, cap;
} JsonBuf;

void json_raw(JsonBuf *j, const char *s);
void json_str(JsonBuf *j, const char *s);  // quoted and escaped
int json_is_number(const char *s);

// FM text: link markup (\x01<base64 reference>\x02text\x01) becomes
// {"text": visible text, "links": ["010baef4", ...]} when links are present,
// otherwise a plain JSON string.
void json_fm_text(JsonBuf *j, const char *s);
