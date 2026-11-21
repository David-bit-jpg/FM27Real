# FM27Real

让《足球经理 27》（Mac 版）变成真实的主教练生活：会记仇的球员、断章取义的记者、在社交媒体上骂你换人的球迷、随战绩波动的博彩赔率，还有你自己的合同和下课压力。

A macOS mod for Football Manager 27 that turns the game into a living manager career: relationships with memory, press conferences, an interactive social feed, in-world betting odds and your own contract — driven by a world engine, with an AI model API for dialogue.

> 开发中，目前处于阶段 0（注入桥）。完整方案见 [docs/PLAN.md](docs/PLAN.md)。

## 组成

| 目录 | 内容 |
| --- | --- |
| `bridge/` | 注入 FM27 进程的小型动态库，通过 il2cpp 接口读写游戏数据 |
| `docs/` | 方案和设计文档 |

## 构建

需要 Xcode 命令行工具。

```bash
make -C bridge
```

产物在 `build/libfm27real.dylib`。

## 环境

- macOS（Apple Silicon 已验证可注入），Steam 版 FM27 `27.1.0.2505117`
- 只读阶段不修改游戏文件和存档
