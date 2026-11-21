# FM27Real

让《足球经理 27》（Mac 版）变成真实的主教练生活：会记仇的球员、断章取义的记者、在社交媒体上骂你换人的球迷、随战绩波动的博彩赔率，还有你自己的合同和下课压力。

A macOS mod for Football Manager 27 that turns the game into a living manager career: relationships with memory, press conferences, an interactive social feed, in-world betting odds and your own contract — driven by a world engine, with an AI model API for dialogue.

> 开发中。阶段 0（注入桥）和阶段 1（读取游戏数据）已完成，下一步阶段 2。完整方案见 [docs/PLAN.md](docs/PLAN.md)。

## 组成

| 目录 | 内容 |
| --- | --- |
| `bridge/` | 注入 FM27 进程的小型动态库，通过 il2cpp 接口读写游戏数据 |
| `tools/` | `fm27.py`：查询接口的客户端（阵容、人物摘要、原始查询） |
| `docs/` | 方案、协议笔记 |

## 构建

需要 Xcode 命令行工具。

```bash
make -C bridge
```

产物在 `build/libfm27real.dylib`。

## 运行

先打开 Steam，再用启动器启动游戏（不要在 Steam 的「启动选项」里设置，Mac 版 Steam 不支持环境变量写法，会报 OS Error 260）：

```bash
./scripts/launch.sh
```

启动器设置 `SteamAppId`，让游戏不把启动交回 Steam；正版授权仍由运行中的 Steam 验证。注入桥的输出在
`~/Library/Application Support/Sports Interactive/Football Manager 27/FM27Real/`：

- `bridge.log`：运行日志
- `classes.tsv`：所有类（程序集、命名空间、类名、父类）
- `members.txt`：`FM.*` / `SI.*` 类的字段（含偏移）和方法
- `catalog.tsv`：所有引用类型的属性代码和值类型
- `api.json`：查询接口地址和令牌（仅本用户可读）

## 查询数据

读入生涯后：

```bash
python3 tools/fm27.py seen
python3 tools/fm27.py person 01049638
python3 tools/fm27.py squad 18000230
```

接口只监听 `127.0.0.1:27270`，每次启动生成新令牌。

## 环境

- macOS（Apple Silicon 已验证可注入），Steam 版 FM27 `27.1.0.2505117`
- 只读阶段不修改游戏文件和存档
