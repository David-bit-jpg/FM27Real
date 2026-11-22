// In-game panel: a WKWebView inside FM's own window (not a separate window),
// sliding in from the right. ⌘J toggles it, Esc closes it.
//
// Unity reads input in its NSApplication and NSWindow subclasses
// (PlayerApplication / PlayerWindow). While the panel is open, keyboard events
// and mouse events over the panel are handed to AppKit's standard
// implementation instead, so they reach the panel and never the game (typing a
// space must not press FM's "Continue"). This swaps Objective-C method
// implementations at runtime; no machine code is modified.
#import <AppKit/AppKit.h>
#import <WebKit/WebKit.h>
#import <objc/runtime.h>

#include "log.h"
#include "overlay.h"
#include "tap.h"

static NSView *g_panel;      // container (rounded, shadowed)
static NSButton *g_chat_button;  // shown on a player's profile page
static uint32_t g_focus;     // person whose profile is open (0 if none)
static WKWebView *g_web;
static BOOL g_visible;
static BOOL g_press_mode;    // covering FM's answer column during a press conference
static const CGFloat kPanelWidth = 440, kMargin = 12;

static IMP g_app_send_unity, g_win_send_unity;  // Unity's overrides
static IMP g_app_send_base, g_win_send_base;    // NSApplication / NSWindow

static NSWindow *game_window(void) {
    NSWindow *best = nil;
    for (NSWindow *w in NSApp.windows) {
        if (!w.isVisible || !w.contentView) continue;
        if (!best || w.frame.size.width * w.frame.size.height > best.frame.size.width * best.frame.size.height)
            best = w;
    }
    return best;
}

static BOOL panel_has_focus(void) {
    NSResponder *r = g_panel.window.firstResponder;
    return [r isKindOfClass:NSView.class] && [(NSView *)r isDescendantOf:g_panel];
}

static NSRect panel_frame(NSView *host, BOOL shown) {
    NSRect b = host.bounds;
    if (g_press_mode) {  // FM's press conference: the answer column on the right
        CGFloat w = b.size.width * 0.28, x = b.size.width * 0.605;
        CGFloat y = b.size.height * 0.015, h = b.size.height * (1 - 0.13 - 0.015);
        return NSMakeRect(shown ? x : b.size.width + kMargin, y, w, h);
    }
    CGFloat h = b.size.height - 2 * kMargin;
    CGFloat x = shown ? b.size.width - kPanelWidth - kMargin : b.size.width + kMargin;
    return NSMakeRect(x, kMargin, kPanelWidth, h);
}

static void set_visible(BOOL shown) {
    if (!g_panel || g_visible == shown) return;
    g_visible = shown;
    NSView *host = g_panel.superview;
    if (shown) g_panel.hidden = NO;
    [NSAnimationContext runAnimationGroup:^(NSAnimationContext *ctx) {
        ctx.duration = 0.18;
        g_panel.animator.frame = panel_frame(host, shown);
    } completionHandler:^{
        if (!g_visible) g_panel.hidden = YES;
    }];
    g_chat_button.hidden = shown || !g_focus;  // the panel covers the button's spot
    if (shown) [g_panel.window makeFirstResponder:g_web];
    else [g_panel.window makeFirstResponder:g_panel.window.contentView];
    log_line("overlay: %s", shown ? "shown" : "hidden");
}

// YES when the event belongs to the panel rather than the game.
static BOOL route_to_panel(NSEvent *e) {
    if (!g_panel) return NO;
    BOOL button_hit = NO;
    if (g_chat_button && !g_chat_button.hidden && e.window == g_panel.window &&
        e.type != NSEventTypeKeyDown && e.type != NSEventTypeKeyUp && e.type != NSEventTypeFlagsChanged) {
        NSPoint p = [g_chat_button.superview convertPoint:e.locationInWindow fromView:nil];
        button_hit = NSPointInRect(p, g_chat_button.frame);
    }
    if (button_hit) return YES;  // the button handles its own clicks; the game must not see them
    if (!g_visible) return NO;
    switch (e.type) {
    case NSEventTypeKeyDown:
    case NSEventTypeKeyUp:
    case NSEventTypeFlagsChanged:
        return panel_has_focus();
    case NSEventTypeLeftMouseDown:
    case NSEventTypeLeftMouseUp:
    case NSEventTypeLeftMouseDragged:
    case NSEventTypeRightMouseDown:
    case NSEventTypeRightMouseUp:
    case NSEventTypeMouseMoved:
    case NSEventTypeScrollWheel: {
        if (e.window != g_panel.window) return NO;
        NSPoint p = [g_panel.superview convertPoint:e.locationInWindow fromView:nil];
        BOOL inside = NSPointInRect(p, g_panel.frame);
        if (!inside && e.type == NSEventTypeLeftMouseDown && panel_has_focus())
            [g_panel.window makeFirstResponder:g_panel.window.contentView];  // click on the game: give it the keyboard back
        return inside;
    }
    default:
        return NO;
    }
}

static void open_panel(uint32_t person);

// Hotkeys handled before anything else. Returns YES if consumed.
static BOOL handle_hotkey(NSEvent *e) {
    if (e.type != NSEventTypeKeyDown) return NO;
    NSEventModifierFlags mods = e.modifierFlags & NSEventModifierFlagDeviceIndependentFlagsMask;
    if (mods == NSEventModifierFlagCommand && [e.charactersIgnoringModifiers.lowercaseString isEqualToString:@"j"]) {
        if (g_visible) set_visible(NO);
        else open_panel(0);
        return YES;
    }
    if (e.keyCode == 53 /* Esc */ && g_visible && panel_has_focus()) {
        set_visible(NO);
        return YES;
    }
    return NO;
}

static void app_send_event(id self, SEL _cmd, NSEvent *e) {
    if (handle_hotkey(e)) return;
    IMP imp = route_to_panel(e) ? g_app_send_base : g_app_send_unity;
    ((void (*)(id, SEL, NSEvent *))imp)(self, _cmd, e);
}

static void win_send_event(id self, SEL _cmd, NSEvent *e) {
    IMP imp = route_to_panel(e) ? g_win_send_base : g_win_send_unity;
    ((void (*)(id, SEL, NSEvent *))imp)(self, _cmd, e);
}

// Replaces cls's own sendEvent: (if it has one) and remembers both Unity's and
// the base class's implementation.
static BOOL swap_send_event(Class cls, Class base, IMP hook, IMP *unity, IMP *base_imp) {
    *base_imp = class_getMethodImplementation(base, @selector(sendEvent:));
    Method m = class_getInstanceMethod(cls, @selector(sendEvent:));
    if (!m) return NO;
    IMP current = method_getImplementation(m);
    *unity = current;
    if (current == *base_imp) {
        // cls does not override sendEvent:; add our own so only cls is affected.
        class_addMethod(cls, @selector(sendEvent:), hook, method_getTypeEncoding(m));
    } else {
        method_setImplementation(m, hook);
    }
    return YES;
}

// --- engine UI ------------------------------------------------------------------
// The panel shows the world engine's page (engine/ui/index.html), served on
// 127.0.0.1 with the token from engine.json in the data folder.
static NSString *g_engine_url, *g_engine_token;
static BOOL g_ui_loaded;

static NSString *offline_page(void) {
    return @"<!doctype html><html><head><meta charset='utf-8'><style>"
            "body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;"
            "font:14px -apple-system,'PingFang SC',sans-serif;color:#e8ecf4;background:linear-gradient(180deg,#1b2a4e,#121a30)}"
            "div{max-width:320px;line-height:1.6}b{font-size:16px}code{background:#0d1426;padding:2px 6px;border-radius:4px}"
            "small{color:#8ea0c8}</style></head><body><div><b>FM27Real 引擎没有运行</b><br><br>"
            "用 <code>scripts/launch.sh</code> 启动游戏会自动启动引擎；<br>也可以在终端运行 <code>python3 -m engine</code>。"
            "<br><br><small>The FM27Real engine is not running. Start the game with scripts/launch.sh, "
            "or run python3 -m engine.</small><br><br><small>⌘J 关闭 · ⌘J to close</small></div></body></html>";
}

static BOOL read_engine_file(void) {
    NSString *path = [NSString stringWithFormat:@"%s/engine.json", g_data_dir];
    NSData *data = [NSData dataWithContentsOfFile:path];
    NSDictionary *d = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
    if (![d isKindOfClass:NSDictionary.class] || ![d[@"url"] isKindOfClass:NSString.class] ||
        ![d[@"token"] isKindOfClass:NSString.class])
        return NO;
    BOOL changed = g_engine_token && ![g_engine_token isEqualToString:d[@"token"]];
    g_engine_url = d[@"url"];
    g_engine_token = d[@"token"];
    if (changed) g_ui_loaded = NO;  // the engine restarted: the loaded page has a stale token
    return YES;
}

// Loads the engine page (optionally straight into a conversation), or an
// explanation when the engine is not reachable.
static void engine_load_query(NSString *extra);
static void engine_load(uint32_t person) {
    engine_load_query(person ? [NSString stringWithFormat:@"&person=%08x", person] : @"");
}

static void engine_load_query(NSString *extra) {
    if (!read_engine_file()) {
        g_ui_loaded = NO;
        [g_web loadHTMLString:offline_page() baseURL:nil];
        return;
    }
    NSString *base = g_engine_url, *token = g_engine_token;
    // A cheap endpoint: /api/status reads the career from the game and can take seconds.
    NSMutableURLRequest *probe = [NSMutableURLRequest requestWithURL:[NSURL URLWithString:[base stringByAppendingString:@"/api/settings"]]];
    [probe setValue:token forHTTPHeaderField:@"X-Token"];
    probe.timeoutInterval = 10;
    [[NSURLSession.sharedSession dataTaskWithRequest:probe completionHandler:^(NSData *d, NSURLResponse *r, NSError *e) {
        (void)d;
        NSInteger status = [r isKindOfClass:NSHTTPURLResponse.class] ? ((NSHTTPURLResponse *)r).statusCode : 0;
        dispatch_async(dispatch_get_main_queue(), ^{
            if (e || status != 200) {
                log_line("overlay: engine not reachable (%s, HTTP %ld)", e ? e.localizedDescription.UTF8String : "ok",
                         (long)status);
                g_ui_loaded = NO;
                [g_web loadHTMLString:offline_page() baseURL:nil];
                return;
            }
            NSString *url = [NSString stringWithFormat:@"%@/ui?token=%@", base, token];
            url = [url stringByAppendingString:extra];
            [g_web loadRequest:[NSURLRequest requestWithURL:[NSURL URLWithString:url]]];
            g_ui_loaded = YES;
        });
    }] resume];
}

// Shows the panel; with a person, opens that conversation.
static void open_panel(uint32_t person) {
    read_engine_file();
    if (g_ui_loaded && person)
        [g_web evaluateJavaScript:[NSString stringWithFormat:@"openPerson('%08x')", person] completionHandler:nil];
    else if (!g_ui_loaded)
        engine_load(person);
    set_visible(YES);
}

static void chat_clicked(id self, SEL _cmd, id sender) {
    (void)self; (void)_cmd; (void)sender;
    open_panel(g_focus);
}

static void set_button_title(NSString *title) {
    NSDictionary *attrs = @{NSForegroundColorAttributeName : NSColor.whiteColor,
                            NSFontAttributeName : [NSFont systemFontOfSize:13 weight:NSFontWeightSemibold]};
    g_chat_button.attributedTitle = [[NSAttributedString alloc] initWithString:title attributes:attrs];
    NSSize size = [g_chat_button.attributedTitle size];
    NSView *host = g_chat_button.superview;
    CGFloat w = size.width + 32, h = 30;
    g_chat_button.frame = NSMakeRect(host.bounds.size.width - w - 24, host.bounds.size.height - 112 - h, w, h);
}

// Looks up the focused person's name for the button, off the main thread
// (queries wait for the game's main thread, so they must not run on it).
static void load_focus(uint32_t person) {
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        uint32_t props[1] = {0x6e616d65 /* eman */};
        char *json = tap_query(person, props, 1);
        NSString *name = nil;
        if (json) {
            NSDictionary *d = [NSJSONSerialization JSONObjectWithData:[NSData dataWithBytes:json length:strlen(json)]
                                                              options:0 error:nil];
            free(json);
            id n = d[@"eman"];
            name = [n isKindOfClass:NSDictionary.class] ? n[@"text"] : ([n isKindOfClass:NSString.class] ? n : nil);
            name = [name stringByReplacingOccurrencesOfString:@"\u200b" withString:@""];
        }
        dispatch_async(dispatch_get_main_queue(), ^{
            if (g_focus != person) return;
            set_button_title([NSString stringWithFormat:@"💬 和 %@ 私聊", name ?: @"球员"]);
        });
    });
}

static void poll_focus(void) {
    uint32_t p = tap_focus(0x01);
    if (p == g_focus) return;
    g_focus = p;
    g_chat_button.hidden = !p || g_visible;
    if (p) {
        set_button_title(@"💬 私聊");
        load_focus(p);
        log_line("overlay: profile of %08x", p);
    }
}

// Press conferences: the engine says when a question is on screen; the panel then
// covers FM's answer column so the manager types instead of clicking.
static BOOL g_press_inflight;
static void press_changed(BOOL active) {
    NSView *host = g_panel.superview;
    if (active && !g_press_mode) {
        log_line("overlay: press conference question on screen");
        g_press_mode = YES;
        if (g_visible) g_panel.animator.frame = panel_frame(host, YES);
        else g_panel.frame = panel_frame(host, NO);
        if (g_ui_loaded) [g_web evaluateJavaScript:@"openPress()" completionHandler:nil];
        else engine_load_query(@"&press=1");
        set_visible(YES);
        g_chat_button.hidden = YES;
    } else if (!active && g_press_mode) {
        set_visible(NO);
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.3 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            if (g_visible) return;
            g_press_mode = NO;
            g_panel.frame = panel_frame(g_panel.superview, NO);
            if (g_ui_loaded) [g_web evaluateJavaScript:@"openHome()" completionHandler:nil];
        });
    }
}

static void poll_press(void) {
    if (g_press_inflight || !read_engine_file()) return;
    g_press_inflight = YES;
    NSMutableURLRequest *req = [NSMutableURLRequest requestWithURL:
        [NSURL URLWithString:[g_engine_url stringByAppendingString:@"/api/press/active"]]];
    [req setValue:g_engine_token forHTTPHeaderField:@"X-Token"];
    req.timeoutInterval = 3;
    [[NSURLSession.sharedSession dataTaskWithRequest:req completionHandler:^(NSData *d, NSURLResponse *r, NSError *e) {
        NSDictionary *j = (!e && d) ? [NSJSONSerialization JSONObjectWithData:d options:0 error:nil] : nil;
        BOOL active = [j isKindOfClass:NSDictionary.class] && [j[@"active"] isEqual:@YES];
        (void)r;
        dispatch_async(dispatch_get_main_queue(), ^{
            g_press_inflight = NO;
            press_changed(active);
        });
    }] resume];
}

static void install_on_main(int attempt) {
    NSWindow *w = game_window();
    if (!w) {
        if (attempt < 120)
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC), dispatch_get_main_queue(), ^{
                install_on_main(attempt + 1);
            });
        else
            log_line("overlay: no game window found");
        return;
    }
    NSView *host = w.contentView;
    g_panel = [[NSView alloc] initWithFrame:panel_frame(host, NO)];
    g_panel.wantsLayer = YES;
    g_panel.layer.cornerRadius = 12;
    g_panel.layer.masksToBounds = YES;
    g_panel.autoresizingMask = NSViewMinXMargin | NSViewHeightSizable;
    g_panel.hidden = YES;

    WKWebViewConfiguration *cfg = [WKWebViewConfiguration new];
    g_web = [[WKWebView alloc] initWithFrame:g_panel.bounds configuration:cfg];
    g_web.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    [g_panel addSubview:g_web];
    [host addSubview:g_panel positioned:NSWindowAbove relativeTo:nil];

    // Chat button (top right, under FM's bars); hidden until a profile is open.
    Class target_cls = objc_allocateClassPair(NSObject.class, "FM27RealButtonTarget", 0);
    class_addMethod(target_cls, @selector(chatClicked:), (IMP)chat_clicked, "v@:@");
    objc_registerClassPair(target_cls);
    static id button_target;
    button_target = [target_cls new];
    g_chat_button = [NSButton buttonWithTitle:@"" target:button_target action:@selector(chatClicked:)];
    g_chat_button.bordered = NO;
    g_chat_button.wantsLayer = YES;
    g_chat_button.layer.backgroundColor = [NSColor colorWithSRGBRed:0.84 green:0.14 blue:0.43 alpha:1].CGColor;
    g_chat_button.layer.cornerRadius = 15;
    g_chat_button.autoresizingMask = NSViewMinXMargin | NSViewMinYMargin;
    g_chat_button.hidden = YES;
    [host addSubview:g_chat_button positioned:NSWindowAbove relativeTo:nil];
    set_button_title(@"💬 私聊");
    [NSTimer scheduledTimerWithTimeInterval:0.4 repeats:YES block:^(NSTimer *t) { (void)t; poll_focus(); }];
    [NSTimer scheduledTimerWithTimeInterval:1.0 repeats:YES block:^(NSTimer *t) { (void)t; poll_press(); }];

    Class app_cls = object_getClass(NSApp), win_cls = object_getClass(w);
    BOOL a = swap_send_event(app_cls, NSApplication.class, (IMP)app_send_event, &g_app_send_unity, &g_app_send_base);
    BOOL b = swap_send_event(win_cls, NSWindow.class, (IMP)win_send_event, &g_win_send_unity, &g_win_send_base);
    log_line("overlay: installed in %s (%s, content %s), input routing app=%d window=%d",
             NSStringFromClass(win_cls).UTF8String, NSStringFromClass(app_cls).UTF8String,
             NSStringFromClass(host.class).UTF8String, a, b);
}

void overlay_install(void) {
    dispatch_async(dispatch_get_main_queue(), ^{ install_on_main(0); });
}
