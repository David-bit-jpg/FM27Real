#!/bin/bash
# FM27Real one-step installer for macOS.
#   git clone https://github.com/David-bit-jpg/FM27Real.git && bash FM27Real/MacOS/install.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
GAME="${FM27_APP:-$HOME/Library/Application Support/Steam/steamapps/common/Football Manager 27/fm.app}"
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1"; exit 1; }

echo "FM27Real — 安装 / install (macOS)"

[ "$(uname -s)" = "Darwin" ] || fail "这是 macOS 安装脚本；Windows 请用 Windows/install.bat"
if [ "$(uname -m)" = "arm64" ]; then ok "Apple Silicon"; else warn "Intel Mac：注入桥目前只支持 Apple Silicon，游戏内功能不可用"; fi

# Python 3.9+ (comes with the Xcode command line tools)
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    warn "需要 Python 3.9+，正在打开 Xcode 命令行工具安装（装好后重新运行本脚本）"
    xcode-select --install 2>/dev/null || true
    exit 1
fi
ok "Python $(python3 -c 'import platform; print(platform.python_version())')"

[ -d "$GAME" ] && ok "找到 Football Manager 27" || warn "没在默认 Steam 位置找到 FM27；如果装在别处，启动前设置 FM27_APP=/path/to/fm.app"

# The bridge: prebuilt in bin/, rebuilt only if missing and Xcode tools exist.
if [ ! -f "$HERE/bin/libfm27real.dylib" ]; then
    command -v xcrun >/dev/null 2>&1 || fail "缺少 bin/libfm27real.dylib，且没有 Xcode 命令行工具可编译（xcode-select --install）"
    make -C "$REPO/bridge"
fi
xattr -dr com.apple.quarantine "$HERE/bin" 2>/dev/null || true
codesign -f -s - "$HERE/bin/libfm27real.dylib" >/dev/null 2>&1 || true
ok "注入桥 bin/libfm27real.dylib"
chmod +x "$HERE"/*.sh

# AI model: Claude Code (signed in) is used by default; otherwise pick one in the panel's settings.
if command -v claude >/dev/null 2>&1 || ls "$HOME/Library/Application Support/Claude/claude-code" >/dev/null 2>&1; then
    ok "检测到 Claude Code（默认模型 Claude Haiku 5.5，最省 token）"
else
    warn "没检测到 Claude Code：进游戏后在面板「设置」里选模型并填 API key（DeepSeek / Anthropic / 本地 Ollama 等）"
fi

# Desktop launcher
LAUNCHER="$HOME/Desktop/FM27Real.command"
cat > "$LAUNCHER" <<LAUNCH
#!/bin/sh
exec "$HERE/launch.sh"
LAUNCH
chmod +x "$LAUNCHER"
ok "桌面启动器：$LAUNCHER"

echo
echo "完成！先打开 Steam，然后双击桌面上的 FM27Real.command（或运行 $HERE/launch.sh）。"
echo "进入游戏后按 ⌘J 打开手机面板。"
