#include "json.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void grow(JsonBuf *j, size_t add) {
    if (j->len + add + 1 <= j->cap) return;
    size_t cap = j->cap ? j->cap : 256;
    while (cap < j->len + add + 1) cap *= 2;
    j->data = realloc(j->data, cap);
    j->cap = cap;
}

static void put(JsonBuf *j, const char *s, size_t n) {
    grow(j, n);
    memcpy(j->data + j->len, s, n);
    j->len += n;
    j->data[j->len] = 0;
}

void json_raw(JsonBuf *j, const char *s) { put(j, s, strlen(s)); }

static void str_n(JsonBuf *j, const char *s, size_t n) {
    put(j, "\"", 1);
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        char esc[8];
        if (c == '"' || c == '\\') { esc[0] = '\\'; esc[1] = (char)c; put(j, esc, 2); }
        else if (c == '\n') put(j, "\\n", 2);
        else if (c == '\t') put(j, "\\t", 2);
        else if (c < 0x20) { snprintf(esc, sizeof esc, "\\u%04x", c); put(j, esc, 6); }
        else put(j, (const char *)&c, 1);
    }
    put(j, "\"", 1);
}

void json_str(JsonBuf *j, const char *s) { str_n(j, s, strlen(s)); }

int json_is_number(const char *s) {
    if (!*s) return 0;
    char *end;
    strtod(s, &end);
    return *end == 0 && strspn(s, "0123456789+-.eE") == strlen(s);
}

static int b64v(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

// Finds the "ferd" (record id) pair in a base64 serialized reference.
static int link_record(const char *b64, size_t n, uint32_t *out) {
    uint8_t raw[512];
    size_t o = 0;
    uint32_t acc = 0;
    int bits = 0;
    for (size_t i = 0; i < n && o < sizeof raw; i++) {
        int v = b64v(b64[i]);
        if (v < 0) break;
        acc = (acc << 6) | (uint32_t)v;
        bits += 6;
        if (bits >= 8) { bits -= 8; raw[o++] = (uint8_t)(acc >> bits); }
    }
    for (size_t i = 0; i + 8 <= o; i++)
        if (!memcmp(raw + i, "ferd", 4)) { memcpy(out, raw + i + 4, 4); return 1; }
    return 0;
}

void json_fm_text(JsonBuf *j, const char *s) {
    if (!strchr(s, '\x01')) { json_str(j, s); return; }
    JsonBuf text = {0}, links = {0};
    json_raw(&links, "[");
    int nlinks = 0;
    const char *p = s;
    int in_link_text = 0;  // between \x02 and the closing \x01 of a link
    while (*p) {
        if (*p == '\x01') {
            p++;
            if (in_link_text) { in_link_text = 0; continue; }
            const char *b = p;
            while (*p && b64v(*p) >= 0) p++;
            while (*p == '=') p++;
            uint32_t rec;
            if (link_record(b, (size_t)(p - b), &rec)) {
                char id[16];
                snprintf(id, sizeof id, "%s\"%08x\"", nlinks++ ? "," : "", rec);
                json_raw(&links, id);
            }
            if (*p == '\x02') { p++; in_link_text = 1; }
            continue;
        }
        const char *start = p;
        while (*p && *p != '\x01') p++;
        put(&text, start, (size_t)(p - start));
    }
    json_raw(&links, "]");
    json_raw(j, "{\"text\":");
    str_n(j, text.data ? text.data : "", text.len);
    json_raw(j, ",\"links\":");
    json_raw(j, links.data);
    json_raw(j, "}");
    free(text.data);
    free(links.data);
}
