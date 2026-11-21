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
static NSString *g_focus_name, *g_focus_face;  // name and portrait data URL
static WKWebView *g_web;
static BOOL g_visible;
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

// Hotkeys handled before anything else. Returns YES if consumed.
static BOOL handle_hotkey(NSEvent *e) {
    if (e.type != NSEventTypeKeyDown) return NO;
    NSEventModifierFlags mods = e.modifierFlags & NSEventModifierFlagDeviceIndependentFlagsMask;
    if (mods == NSEventModifierFlagCommand && [e.charactersIgnoringModifiers.lowercaseString isEqualToString:@"j"]) {
        set_visible(!g_visible);
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

static NSString *test_page(void) {
    return @"<!doctype html><html><head><meta charset='utf-8'><style>"
            "body{margin:0;font:14px -apple-system,'PingFang SC',sans-serif;color:#e8ecf4;"
            "background:linear-gradient(180deg,#1b2a4e,#121a30);height:100vh;display:flex;flex-direction:column}"
            "header{padding:16px 18px;border-bottom:1px solid #2c3d66;font-weight:600;font-size:16px}"
            "header small{display:block;font-weight:400;color:#8ea0c8;font-size:12px;margin-top:4px}"
            "#log{flex:1;overflow:auto;padding:14px 18px}"
            ".msg{background:#25365f;border-radius:10px;padding:8px 12px;margin:6px 0;max-width:85%}"
            ".me{background:#7b2cbf;margin-left:auto}"
            "form{display:flex;gap:8px;padding:12px;border-top:1px solid #2c3d66}"
            "input{flex:1;border:0;border-radius:8px;padding:10px 12px;background:#0d1426;color:#fff;font-size:14px}"
            "button{border:0;border-radius:8px;padding:0 16px;background:#d6246e;color:#fff;font-weight:600}"
            "</style></head><body>"
            "<header style='display:flex;align-items:center;gap:12px'><img id='face' style='width:56px;height:56px;border-radius:50%;"
            "object-fit:cover;background:#25365f;display:none'><div><span id='who'>主教练手机</span>"
            "<small>⌘J 开关 · Esc 关闭 · 聊天功能在 2d/2e 接上</small></div></header>"
            "<div id='log'><div class='msg'>在下面输入一句话试试。</div></div>"
            "<form onsubmit=\"var i=document.getElementById('t');if(!i.value)return false;"
            "var d=document.createElement('div');d.className='msg me';d.textContent=i.value;"
            "document.getElementById('log').appendChild(d);i.value='';return false\">"
            "<input id='t' placeholder='输入消息…' autofocus><button>发送</button></form>"
            "<script>function setPerson(n,img){document.getElementById('who').textContent=n;"
            "var f=document.getElementById('face');if(img){f.src=img;f.style.display='block'}else{f.style.display='none'}}</script>"
            "</body></html>";
}

static NSString *js_string(NSString *s) {
    NSData *d = [NSJSONSerialization dataWithJSONObject:@[ s ?: @"" ] options:0 error:nil];
    NSString *arr = [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding];
    return [arr substringWithRange:NSMakeRange(1, arr.length - 2)];  // strip [ ]
}

static void show_focus_in_panel(void) {
    NSString *js = g_focus ? [NSString stringWithFormat:@"setPerson(%@,%@)", js_string(g_focus_name), js_string(g_focus_face)]
                           : @"setPerson('主教练手机','')";
    [g_web evaluateJavaScript:js completionHandler:nil];
}

static void chat_clicked(id self, SEL _cmd, id sender) {
    (void)self; (void)_cmd; (void)sender;
    show_focus_in_panel();
    set_visible(YES);
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

// Looks up the focused person's name and portrait off the main thread
// (queries wait for the game's main thread, so they must not run on it).
static void load_focus(uint32_t person) {
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        uint32_t props[2] = {0x6e616d65 /* eman */, 0x756e6964 /* dinu */};
        char *json = tap_query(person, props, 2);
        NSString *name = nil, *face = nil;
        if (json) {
            NSDictionary *d = [NSJSONSerialization JSONObjectWithData:[NSData dataWithBytes:json length:strlen(json)]
                                                              options:0 error:nil];
            free(json);
            id n = d[@"eman"];
            name = [n isKindOfClass:NSDictionary.class] ? n[@"text"] : ([n isKindOfClass:NSString.class] ? n : nil);
            name = [name stringByReplacingOccurrencesOfString:@"\u200b" withString:@""];
            NSNumber *uid = [d[@"dinu"] isKindOfClass:NSNumber.class] ? d[@"dinu"] : nil;
            if (uid) {
                NSString *path = [NSString stringWithFormat:
                    @"%@/Library/Application Support/Sports Interactive/Football Manager 27/graphics/faces/face_%lld.png",
                    NSHomeDirectory(), uid.longLongValue];
                NSData *png = [NSData dataWithContentsOfFile:path];
                if (png) face = [@"data:image/png;base64," stringByAppendingString:[png base64EncodedStringWithOptions:0]];
            }
        }
        dispatch_async(dispatch_get_main_queue(), ^{
            if (g_focus != person) return;
            g_focus_name = name ?: @"球员";
            g_focus_face = face ?: @"";
            set_button_title([NSString stringWithFormat:@"💬 和 %@ 私聊", g_focus_name]);
            if (g_visible) show_focus_in_panel();
        });
    });
}

static void poll_focus(void) {
    uint32_t p = tap_focus(0x01);
    if (p == g_focus) return;
    g_focus = p;
    g_focus_name = g_focus_face = nil;
    g_chat_button.hidden = !p;
    if (p) {
        set_button_title(@"💬 私聊");
        load_focus(p);
        log_line("overlay: profile of %08x", p);
    }
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
    [g_web loadHTMLString:test_page() baseURL:nil];
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
