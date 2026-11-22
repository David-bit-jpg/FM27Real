# FM27Real · Windows 安装教程

让《足球经理 27》变成真实的主教练生活：会记仇的球员、追着你问的记者、社交媒体上的球迷和转会传闻、后台真实改变游戏数据的谈话和发布会。

> **实验版**：Windows 版和 Mac 版是同一套代码，但还没在 Windows 真机上测试过。遇到问题请把 `bridge.log` 和 `engine.log`（位置见下）发到 GitHub Issues。

## 需要

- Windows 10 / 11（64 位）
- Steam 版《Football Manager 27》
- Python 3.9+（安装脚本可以用 winget 帮你装；或者到 [python.org](https://www.python.org/downloads/) 下载，安装时勾选 **Add Python to PATH**）
- 一个 AI 模型：推荐 [Claude Code](https://claude.com/claude-code)（已登录即可，默认用最省 token 的 Claude Haiku 5.5）；也可以在面板「设置」里改用 DeepSeek、Anthropic API、智谱、本地 Ollama 等

## 一键安装

1. 下载本仓库：装了 Git 的话运行 `git clone https://github.com/David-bit-jpg/FM27Real.git`；没装就在 GitHub 页面点 **Code → Download ZIP** 并解压。
2. 打开 `FM27Real\Windows` 文件夹，**双击 `install.bat`**。

安装脚本会检查 Python、在 Steam 库里找到游戏、准备好注入桥（`bin\fm27real.dll`，已预编译），并在桌面创建 **FM27Real** 快捷方式。

## 开始玩

1. 打开 Steam。
2. 双击桌面上的 **FM27Real**。不要从 Steam 里点「开始」，那样不会加载 FM27Real。
3. 游戏启动后，手机面板会在浏览器里打开（消息、动态、社交、设置）。可以把浏览器窗口放在副屏，或和游戏并排。

## 文件在哪里

FM 在 Windows 上的用户文件夹在「文档」里：

```
文档\Sports Interactive\Football Manager 27\FM27Real\
```

里面有 `settings.json`（模型设置和 API key，只保存在本机）、`bridge.log`、`engine.log` 和世界记忆。

## 常见问题

- **杀毒软件拦截**：启动器会把 `fm27real.dll` 加载进游戏进程，有些杀毒软件会误报。可以把 `FM27Real\Windows\bin` 加入白名单。
- **双击 install.bat 一闪而过 / 报执行策略错误**：右键 `install.bat` →「以管理员身份运行」，或在 PowerShell 里运行 `powershell -ExecutionPolicy Bypass -File install.ps1`。
- **找不到游戏**：安装脚本会让你粘贴游戏 `.exe` 的完整路径（在 `steamapps\common\Football Manager 27\` 里）。
- **换模型 / 填 API key**：面板 →「设置」。
- **会不会改坏存档？** 改动都通过 FM 自己的对话、发布会和游戏内编辑器（需要正版 In-Game Editor）进行，在「设置」里可以关掉「真实修改游戏」。重要存档请先备份。

## 更新

`git pull`（或重新下载 ZIP），然后再双击一次 `install.bat`。

## 自己编译注入桥（可选）

在 macOS / Linux 上用 mingw-w64 交叉编译：

```bash
make -C bridge -f Makefile.win     # 输出到 Windows/bin/fm27real.dll
```
