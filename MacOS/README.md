# FM27Real · macOS 安装教程

让《足球经理 27》变成真实的主教练生活：会记仇的球员、追着你问的记者、社交媒体上的球迷和转会传闻、后台真实改变游戏数据的谈话和发布会。

## 需要

- Apple Silicon 的 Mac（M1/M2/M3/M4…）
- Steam 版《Football Manager 27》
- Python 3.9+（没有的话安装脚本会提示安装 Xcode 命令行工具，自带 Python）
- 一个 AI 模型：推荐 [Claude Code](https://claude.com/claude-code)（已登录即可，默认用最省 token 的 Claude Haiku 5.5）；也可以在面板「设置」里改用 DeepSeek、Anthropic API、智谱、本地 Ollama 等

## 一键安装

打开「终端」，粘贴运行：

```bash
git clone https://github.com/David-bit-jpg/FM27Real.git && bash FM27Real/MacOS/install.sh
```

安装脚本会检查 Python 和游戏、准备好注入桥（`bin/libfm27real.dylib`，已预编译），并在桌面放一个 **FM27Real.command**。

## 开始玩

1. 打开 Steam。
2. 双击桌面上的 **FM27Real.command**（或运行 `FM27Real/MacOS/launch.sh`）。不要从 Steam 里点「开始」，那样不会加载 FM27Real。
3. 读入存档后，按 **⌘J** 打开手机面板：消息、动态、社交、设置。

游戏装在别的位置时：`FM27_APP="/路径/fm.app" FM27Real/MacOS/launch.sh`。

## 常见问题

- **面板显示「连不上引擎」**：运行 `FM27Real/MacOS/restart-engine.sh`。
- **换模型 / 填 API key**：面板 →「设置」。API key 只保存在本机（`~/Library/Application Support/Sports Interactive/Football Manager 27/FM27Real/settings.json`，仅本人可读），只发给你选的模型服务。
- **会不会改坏存档？** 改动都通过 FM 自己的对话、发布会和游戏内编辑器（需要正版 In-Game Editor）进行，在「设置」里可以关掉「真实修改游戏」。重要存档请先备份。
- **日志**：`~/Library/Application Support/Sports Interactive/Football Manager 27/FM27Real/`（`bridge.log`、`engine.log`）。

## 更新

```bash
cd FM27Real && git pull && bash MacOS/install.sh
```

## 自己编译注入桥（可选）

```bash
xcode-select --install
make -C FM27Real/bridge        # 输出到 MacOS/bin/libfm27real.dylib
```
