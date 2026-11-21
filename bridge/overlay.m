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

static NSView *g_panel;      // container (rounded, shadowed)
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
    if (!g_visible || !g_panel) return NO;
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
            "<header>FM27Real · 面板测试<small>⌘J 开关 · Esc 关闭 · 输入中文和空格，看游戏会不会被触发</small></header>"
            "<div id='log'><div class='msg'>在下面输入一句话试试。</div></div>"
            "<form onsubmit=\"var i=document.getElementById('t');if(!i.value)return false;"
            "var d=document.createElement('div');d.className='msg me';d.textContent=i.value;"
            "document.getElementById('log').appendChild(d);i.value='';return false\">"
            "<input id='t' placeholder='输入消息…' autofocus><button>发送</button></form>"
            "</body></html>";
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
