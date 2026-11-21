// Local query API: a minimal HTTP/1.1 server on 127.0.0.1 only.
//
// Every request needs the token written to api.json in the data folder, as an
// "X-Token" header or a "token" query parameter.
//
//   GET /status                         {"ready": bool, "guid": "..."}
//   GET /seen                           records the interface showed recently (newest last)
//   GET /query?id=010baef4&props=eman,dinu
//   GET /query?id=010baef4&type=PersonReference     every property of that type
//
// Property codes are 4 characters ("eman"; URL-encode spaces as %20) or
// "#" + 8 hex digits.
#include "http.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

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
    write(fd, head, (size_t)n);
    write(fd, body, strlen(body));
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
        ssize_t r = read(fd, req + got, sizeof req - 1 - got);
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
    if (!strcmp(target, "/query")) {
        char id[32] = "", props[8192] = "", type[128] = "";
        if (!query || !param(query, "id", id, sizeof id)) { respond(fd, 400, "{\"error\":\"id required\"}"); return; }
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
        char *body = tap_query((uint32_t)strtoul(id, NULL, 16), list, n);
        respond(fd, body ? 200 : 503, body ? body : "{\"error\":\"query failed\"}");
        free(body);
        return;
    }
    respond(fd, 404, "{\"error\":\"unknown path\"}");
}

static void *serve(void *arg) {
    (void)arg;
    il2cpp_thread_attach(il2cpp_domain_get());  // handlers call into managed code
    int s = socket(AF_INET, SOCK_STREAM, 0);
    int on = 1;
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &on, sizeof on);
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(API_PORT)};
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);  // never reachable from the network
    if (bind(s, (struct sockaddr *)&addr, sizeof addr) || listen(s, 8)) {
        log_line("http: cannot listen on 127.0.0.1:%d", API_PORT);
        close(s);
        return NULL;
    }
    log_line("http: listening on 127.0.0.1:%d", API_PORT);
    for (;;) {
        int fd = accept(s, NULL, NULL);
        if (fd < 0) continue;
        handle(fd);
        close(fd);
    }
    return NULL;
}

void http_start(void) {
    for (int i = 0; i < 16; i++) snprintf(g_token + 2 * i, 3, "%02x", arc4random() & 0xff);
    FILE *f = data_file("api.json", "w");
    if (f) {
        fchmod(fileno(f), 0600);  // token: readable by this user only
        fprintf(f, "{\"url\":\"http://127.0.0.1:%d\",\"token\":\"%s\"}\n", API_PORT, g_token);
        fclose(f);
    }
    pthread_t t;
    if (pthread_create(&t, NULL, serve, NULL) == 0) pthread_detach(t);
}
