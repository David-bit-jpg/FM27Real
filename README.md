# FM27Real

让《足球经理 27》变成真实的主教练生活：会记仇的球员、追着你问的记者、社交媒体上的球迷和转会传闻、赛前赔率和串关，还有在后台真实改变游戏数据的谈话和发布会。

A mod for Football Manager 27 that turns the game into a living manager career: players with memory and their own voice, press conferences you answer by typing (run on FM's own engine in the background), an Instagram-style social feed with rival fans and transfer rumours, odds, and conversations that really change the game.

## 安装 · Install

| 系统 | 文件夹 | 一键安装 | 教程 |
| --- | --- | --- | --- |
| **macOS**（Apple Silicon） | [`MacOS/`](MacOS) | `bash MacOS/install.sh` | [MacOS/README.md](MacOS/README.md) |
| **Windows** 10/11（实验版） | [`Windows/`](Windows) | 双击 `Windows\install.bat` | [Windows/README.md](Windows/README.md) |

macOS 一行搞定：

```bash
git clone https://github.com/David-bit-jpg/FM27Real.git && bash FM27Real/MacOS/install.sh
```

默认 AI 模型是 Claude Haiku 5.5（通过本机已登录的 Claude Code，最省 token）；也可以在游戏内面板「设置」里换成 DeepSeek、Anthropic API、通义千问、Kimi、智谱、Gemini、OpenRouter、本地 Ollama 等。

## 有什么

- **私聊和群聊**：每个球员按自己的年龄、出身、效力过的球队、性格和更衣室地位说话，记得你说过的话。
- **后台 FM 原版谈话**：你在手机里表扬、批评、安慰球员，推进游戏后会在后台变成 FM 原版的谈话，由 FM 结算士气、满意度和关系。
- **新闻发布会**：在手机上打字回答，记者会追问；后台走 FM 原版流程，影响媒体关系、声望和球员满意度。
- **动态和社交**：比赛、新闻、训练场趣事、八卦、转会传闻（FM 自己的转会市场数据）、场边喊话；球迷、对手球迷、感兴趣球队的球迷都会来评论。
- **点名字跳转**：面板里带下划线的人名和俱乐部，点一下就打开 FM 里对应的页面。
- **真实修改**：在设置里打开后，按规则通过 FM 的游戏内编辑器调整声望、职业素养等（需要正版 In-Game Editor）。

## 目录

| 目录 | 内容 |
| --- | --- |
| `MacOS/` | macOS 安装、启动脚本，预编译注入桥 `bin/libfm27real.dylib` |
| `Windows/` | Windows 安装、启动脚本（含注入器），预编译注入桥 `bin\fm27real.dll` |
| `bridge/` | 注入桥源码（两个平台同一套 C 代码，通过 IL2CPP 接口读写游戏数据）：`make` 编译 macOS，`make -f Makefile.win` 编译 Windows |
| `engine/` | 世界引擎（Python 标准库 + SQLite）和手机面板界面，两个平台通用 |
| `tools/` | 调试工具 |
| `docs/` | 协议笔记、逆向得到的引擎地图 |

## 隐私

- 接口只监听本机 `127.0.0.1`，每次启动生成新令牌。
- API key 只保存在本机 `settings.json`，只发给你选的模型服务。
- 只有在你和人物对话、查看动态和社交时才调用模型；其余一切都是代码规则，跟着游戏时间推进。
