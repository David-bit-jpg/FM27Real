// Local query API: a minimal HTTP/1.1 server on 127.0.0.1 only.
//
// Every request needs the token written to api.json in the data folder, as an
// "X-Token" header or a "token" query parameter.
//
//   GET /status                         {"ready": bool, "guid": "..."}
//   GET /seen                           records the interface showed recently (newest last)
//   GET /events?since=N                 UI -> engine events after sequence N
//   GET /target?type=23                 most recent target the UI opened for that record type
//   GET /focus                          {"person": "01049638" | null} — the profile being viewed
//   GET /query?id=010baef4&props=eman,dinu
//   GET /query?id=010baef4&type=PersonReference     every property of that type
//   GET /query?target=<hex>&props=...               any serialized target (from a "target" value)
//   GET /send?id=N&target=<hex>&data=<hex>          send an event to the engine as the interface does
//   GET /editor                                     In-Game Editor fields on screen: [{label, value, key}]
//   GET /editor/set?key=<hex>&data=<hex>            write a field value on the UI's own channel
//
// Property codes are 4 characters ("eman"; URL-encode spaces as %20) or
// "#" + 8 hex digits.
#include "http.h"

#include "platform.h"

#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#if defined(_WIN32)
#include <ws2tcpip.h>
#define net_close closesocket
#define net_read(fd, b, n) recv((SOCKET)(fd), (char *)(b), (int)(n), 0)
#define net_write(fd, b, n) send((SOCKET)(fd), (const char *)(b), (int)(n), 0)
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#define net_close close
#define net_read read
#define net_write write
#endif

#include "catalog.h"
#include "il2cpp_api.h"
#include "json.h"
#include "log.h"
#include "tap.h"

#define API_PORT 27270
static char g_token[40];

static void respond(int fd, int status, const char *body) {
    const char *reason = status == 200 ? "OK" : status == 401 ? "Unauthorized" : status == 404 ? "Not Found"
                       : status == 503 ? "Service Unavailable" : "Bad Request";
    char head[256];
    int n = snprintf(head, sizeof head,
                     "HTTP/1.1 %d %s\r\nContent-Type: application/json; charset=utf-8\r\n"
                     "Content-Length: %zu\r\nConnection: close\r\n\r\n",
                     status, reason, strlen(body));
    net_write(fd, head, (size_t)n);
    net_write(fd, body, strlen(body));
}

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static void url_decode(char *s) {
    char *o = s;
    for (; *s; s++) {
        if (*s == '%' && hexval(s[1]) >= 0 && hexval(s[2]) >= 0) {
            *o++ = (char)(hexval(s[1]) * 16 + hexval(s[2]));
            s += 2;
        } else {
            *o++ = *s == '+' ? ' ' : *s;
        }
    }
    *o = 0;
}

// Copies the decoded value of query parameter `name` into out.
static int param(const char *query, const char *name, char *out, size_t cap) {
    size_t nl = strlen(name);
    for (const char *p = query; p && *p;) {
        const char *amp = strchr(p, '&');
        size_t len = amp ? (size_t)(amp - p) : strlen(p);
        if (len > nl && !strncmp(p, name, nl) && p[nl] == '=') {
            size_t vl = len - nl - 1;
            if (vl >= cap) vl = cap - 1;
            memcpy(out, p + nl + 1, vl);
            out[vl] = 0;
            url_decode(out);
            return 1;
        }
        p = amp ? amp + 1 : NULL;
    }
    return 0;
}

// Property list for a reference type, cached (the catalog call is not free).
static int type_properties(const char *type, uint32_t *out, int cap) {
    static struct { char name[128]; uint32_t *props; int n; } cache[32];
    static int cached;
    for (int i = 0; i < cached; i++)
        if (!strcmp(cache[i].name, type)) {
            int n = cache[i].n < cap ? cache[i].n : cap;
            memcpy(out, cache[i].props, sizeof(uint32_t) * (size_t)n);
            return n;
        }
    int n = catalog_properties("FM.UI", type, out, cap);
    if (n > 0 && cached < 32) {
        strlcpy(cache[cached].name, type, sizeof cache[cached].name);
        cache[cached].props = malloc(sizeof(uint32_t) * (size_t)n);
        memcpy(cache[cached].props, out, sizeof(uint32_t) * (size_t)n);
        cache[cached++].n = n;
    }
    return n;
}

static void handle(int fd) {
    char req[8192];
    size_t got = 0;
    while (got < sizeof req - 1) {
        long r = (long)net_read(fd, req + got, sizeof req - 1 - got);
        if (r <= 0) break;
        got += (size_t)r;
        req[got] = 0;
        if (strstr(req, "\r\n\r\n")) break;
    }
    req[got] = 0;
    char method[8], target[4096];
    if (sscanf(req, "%7s %4095s", method, target) != 2 || strcmp(method, "GET")) {
        respond(fd, 400, "{\"error\":\"GET only\"}");
        return;
    }
    char *query = strchr(target, '?');
    if (query) *query++ = 0;

    char token[64] = "";
    const char *h = strcasestr(req, "\r\nX-Token:");
    if (h) sscanf(h + 10, " %63s", token);
    else if (query) param(query, "token", token, sizeof token);
    if (strcmp(token, g_token)) { respond(fd, 401, "{\"error\":\"bad token\"}"); return; }

    if (!strcmp(target, "/status")) {
        char body[128];
        snprintf(body, sizeof body, "{\"ready\":%s,\"guid\":\"%08x\"}", tap_ready() ? "true" : "false", tap_guid());
        respond(fd, 200, body);
        return;
    }
    if (!strcmp(target, "/events")) {
        char since[32] = "0";
        if (query) param(query, "since", since, sizeof since);
        char *body = tap_events_json(strtoull(since, NULL, 10));
        respond(fd, 200, body);
        free(body);
        return;
    }
    if (!strcmp(target, "/target")) {
        char type[16] = "";
        if (!query || !param(query, "type", type, sizeof type)) { respond(fd, 400, "{\"error\":\"type required\"}"); return; }
        char *hexs = tap_last_target((uint32_t)strtoul(type, NULL, 10));
        char body[256];
        snprintf(body, sizeof body, hexs ? "{\"target\":\"%s\"}" : "{\"target\":null}", hexs ? hexs : "");
        free(hexs);
        respond(fd, 200, body);
        return;
    }
    if (!strcmp(target, "/send")) {
        char id[16] = "", th[200] = "", dh[2100] = "";
        if (!query || !param(query, "id", id, sizeof id) || !param(query, "target", th, sizeof th)) {
            respond(fd, 400, "{\"error\":\"id and target required\"}"); return;
        }
        param(query, "data", dh, sizeof dh);
        uint8_t t[96], d[1024];
        int tl = 0, dl = 0;
        for (const char *p = th; p[0] && p[1] && tl < (int)sizeof t; p += 2) {
            int hi = hexval(p[0]), lo = hexval(p[1]);
            if (hi < 0 || lo < 0) break;
            t[tl++] = (uint8_t)(hi * 16 + lo);
        }
        for (const char *p = dh; p[0] && p[1] && dl < (int)sizeof d; p += 2) {
            int hi = hexval(p[0]), lo = hexval(p[1]);
            if (hi < 0 || lo < 0) break;
            d[dl++] = (uint8_t)(hi * 16 + lo);
        }
        int ok = tap_send_event((uint32_t)strtoul(id, NULL, 10), t, tl, d, dl);
        respond(fd, ok ? 200 : 503, ok ? "{\"sent\":true}" : "{\"sent\":false}");
        return;
    }
    if (!strcmp(target, "/editor")) {
        char *body = tap_editor_json();
        respond(fd, 200, body);
        free(body);
        return;
    }
    if (!strcmp(target, "/editor/open")) {
        char rs[16] = "", kind[16] = "person";
        if (!query || !param(query, "record", rs, sizeof rs)) { respond(fd, 400, "{\"error\":\"record required\"}"); return; }
        param(query, "kind", kind, sizeof kind);
        int r = tap_editor_open((uint32_t)strtoul(rs, NULL, 16), kind);
        char body[64];
        snprintf(body, sizeof body, "{\"result\":%d}", r);
        respond(fd, r == 1 ? 200 : 503, body);
        return;
    }
    if (!strcmp(target, "/ui/open")) {
        char rs[16] = "", kind[16] = "person";
        if (!query || !param(query, "record", rs, sizeof rs)) { respond(fd, 400, "{\"error\":\"record required\"}"); return; }
        param(query, "kind", kind, sizeof kind);
        int r = tap_nav_open((uint32_t)strtoul(rs, NULL, 16), kind);
        char body[64];
        snprintf(body, sizeof body, "{\"result\":%d}", r);
        respond(fd, r == 1 ? 200 : 503, body);
        return;
    }
    if (!strcmp(target, "/editor/tab")) {
        char ix[16] = "";
        int set = query && param(query, "index", ix, sizeof ix);
        int r = tap_editor_tab(set, set ? (int)strtol(ix, NULL, 10) : 0);
        char body[64];
        snprintf(body, sizeof body, "{\"index\":%d}", r);
        respond(fd, r >= 0 ? 200 : 503, body);
        return;
    }
    if (!strcmp(target, "/ui/click")) {
        char text[128] = "", click[8] = "", nth[16] = "-1";
        if (!query || !param(query, "text", text, sizeof text)) { respond(fd, 400, "{\"error\":\"text required\"}"); return; }
        param(query, "click", click, sizeof click);
        param(query, "nth", nth, sizeof nth);
        int r = tap_ui_click(text, click[0] == '1', (int)strtol(nth, NULL, 10));
        char body[64];
        snprintf(body, sizeof body, "{\"result\":%d}", r);
        respond(fd, r >= 0 ? 200 : 503, body);
        return;
    }
    if (!strcmp(target, "/editor/close")) {
        char dry[8] = "";
        if (query) param(query, "dry", dry, sizeof dry);
        int r = tap_editor_close(dry[0] == '1');
        char body[64];
        snprintf(body, sizeof body, "{\"result\":%d}", r);
        respond(fd, r == 1 ? 200 : 503, body);
        return;
    }
    if (!strcmp(target, "/editor/set")) {
        char ks[24] = "", dh[2100] = "";
        if (!query || !param(query, "key", ks, sizeof ks) || !param(query, "data", dh, sizeof dh)) {
            respond(fd, 400, "{\"error\":\"key and data required\"}"); return;
        }
        uint8_t d[1024];
        int dl = 0;
        for (const char *p = dh; p[0] && p[1] && dl < (int)sizeof d; p += 2) {
            int hi = hexval(p[0]), lo = hexval(p[1]);
            if (hi < 0 || lo < 0) break;
            d[dl++] = (uint8_t)(hi * 16 + lo);
        }
        int ok = tap_editor_set(strtoull(ks, NULL, 16), d, dl);
        respond(fd, ok == 1 ? 200 : (ok < 0 ? 404 : 503),
                ok == 1 ? "{\"written\":true}" : (ok < 0 ? "{\"error\":\"not an open editor field\"}" : "{\"written\":false}"));
        return;
    }
    if (!strcmp(target, "/focus")) {
        uint32_t p = tap_focus(0x01);
        char body[64];
        if (p) snprintf(body, sizeof body, "{\"person\":\"%08x\"}", p);
        else snprintf(body, sizeof body, "{\"person\":null}");
        respond(fd, 200, body);
        return;
    }
    if (!strcmp(target, "/seen")) {
        uint32_t seen[256];
        int n = tap_seen(seen, 256);
        JsonBuf j = {0};
        json_raw(&j, "[");
        for (int i = 0; i < n; i++) {
            char id[16];
            snprintf(id, sizeof id, "%s\"%08x\"", i ? "," : "", seen[i]);
            json_raw(&j, id);
        }
        json_raw(&j, "]");
        respond(fd, 200, j.data);
        free(j.data);
        return;
    }
    if (!strcmp(target, "/release")) {
        char id[16] = "";
        if (!query || !param(query, "id", id, sizeof id)) { respond(fd, 400, "{\"error\":\"id required\"}"); return; }
        int ok = tap_release((uint32_t)strtoul(id, NULL, 10));
        respond(fd, ok ? 200 : 404, ok ? "{\"released\":true}" : "{\"released\":false}");
        return;
    }
    if (!strcmp(target, "/query")) {
        char id[32] = "", props[8192] = "", type[128] = "", target_hex[600] = "";
        int has_id = query && param(query, "id", id, sizeof id);
        int has_target = query && param(query, "target", target_hex, sizeof target_hex);
        if (!has_id && !has_target) { respond(fd, 400, "{\"error\":\"id or target required\"}"); return; }
        static uint32_t list[2048];
        int n = 0;
        if (param(query, "type", type, sizeof type)) {
            n = type_properties(type, list, 2048);
        } else if (param(query, "props", props, sizeof props)) {
            for (char *p = props; *p && n < 2048;) {
                char *comma = strchr(p, ',');
                if (comma) *comma = 0;
                list[n++] = fourcc_parse(p);
                if (!comma) break;
                p = comma + 1;
            }
        }
        if (!n) { respond(fd, 400, "{\"error\":\"props or type required\"}"); return; }
        if (!tap_ready()) { respond(fd, 503, "{\"error\":\"no career loaded yet\"}"); return; }
        char *body;
        if (has_target) {
            uint8_t t[300];
            int tl = 0;
            for (const char *p = target_hex; p[0] && p[1] && tl < (int)sizeof t; p += 2) {
                int hi = hexval(p[0]), lo = hexval(p[1]);
                if (hi < 0 || lo < 0) break;
                t[tl++] = (uint8_t)(hi * 16 + lo);
            }
            char hold[8] = "";
            if (param(query, "hold", hold, sizeof hold) && hold[0] == '1') {
                uint32_t hid = 0;
                char *values = tap_hold_target(t, tl, list, n, &hid);
                if (values) {  // {"hold": id, "values": {...}}
                    size_t len = strlen(values) + 64;
                    body = malloc(len);
                    snprintf(body, len, "{\"hold\":%u,\"values\":%s}", hid, values);
                    free(values);
                } else body = NULL;
            } else body = tap_query_target(t, tl, list, n);
        } else {
            body = tap_query((uint32_t)strtoul(id, NULL, 16), list, n);
        }
        respond(fd, body ? 200 : 503, body ? body : "{\"error\":\"query failed\"}");
        free(body);
        return;
    }
    respond(fd, 404, "{\"error\":\"unknown path\"}");
}

static void *serve(void *arg) {
    (void)arg;
    il2cpp_thread_attach(il2cpp_domain_get());  // handlers call into managed code
#if defined(_WIN32)
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
#endif
    int s = (int)socket(AF_INET, SOCK_STREAM, 0);
    int on = 1;
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, (const char *)&on, sizeof on);
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(API_PORT)};
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);  // never reachable from the network
    if (bind(s, (struct sockaddr *)&addr, sizeof addr) || listen(s, 8)) {
        log_line("http: cannot listen on 127.0.0.1:%d", API_PORT);
        net_close(s);
        return NULL;
    }
    log_line("http: listening on 127.0.0.1:%d", API_PORT);
    for (;;) {
        int fd = (int)accept(s, NULL, NULL);
        if (fd < 0) continue;
        handle(fd);
        net_close(fd);
    }
    return NULL;
}

void http_start(void) {
    uint8_t rnd[16];
    platform_random(rnd, sizeof rnd);
    for (int i = 0; i < 16; i++) snprintf(g_token + 2 * i, 3, "%02x", rnd[i]);
    FILE *f = data_file("api.json", "w");
    if (f) {
#if !defined(_WIN32)
        fchmod(fileno(f), 0600);  // token: readable by this user only (Windows: the user's own Documents)
#endif
        fprintf(f, "{\"url\":\"http://127.0.0.1:%d\",\"token\":\"%s\"}\n", API_PORT, g_token);
        fclose(f);
    }
    pthread_t t;
    if (pthread_create(&t, NULL, serve, NULL) == 0) pthread_detach(t);
}
