# FM27 数据通道协议（逆向笔记）

FM27 的游戏数据库和模拟在原生引擎 `game_plugin.bundle` 里（符号已剥离）。C# 界面通过 `FM.GamePlugin` 的「数据通道」向引擎要数据。FM27Real 的桥在这个接口上监听和发起查询。以下内容都在 macOS Steam 版 `27.1.0.2505117` 上实测。

## 挂钩方式

游戏启用了强化运行时，不能改机器码。桥只替换类的虚函数表项（普通堆内存）：

| 类 | 方法 | 方向 |
| --- | --- | --- |
| `FM_GamePlugin_IGamePlugin_NativeWrapper` | `OpenChannels(Span<byte> target, Span<ChannelData>)` | 界面 → 引擎 |
| 同上 | `PerformChannelActions(Span<ChannelAction>)` | 界面 → 引擎 |
| 同上 | `Tick()` | 每帧，主线程 |
| `FM.GamePlugin.GameReceiver` | `SendDataResponse(Span<ulong> keys, Span<uint> generations, Span<byte> data)` | 引擎 → 界面 |
| 同上 | `SendEvent(uint id, Span<byte>)` | 引擎 → 界面 |

调用约定（arm64）：`x0 = this`；每个 `Span<T>`（16 字节：`_pointer`、`_length`）占两个寄存器；最后一个参数是 `MethodInfo*`。

## 结构

- `ChannelData`（16 字节）：`u32 property; u32 generation; u64 key;`
- `ChannelAction`（24 字节）：`u64 key; u64 additionalKey; i32 mode;`
- `ChannelActionMode`：AddNode=0, RemoveNode=1, CloseChannel=2, SetTarget=3, UpdateChannelData=4, OpenChannels=5, AddData=6, SetData=7, ReleaseData=8, SetPropertyID=9
- 回复：`keys` 和解码后的值一一对应。值在原方法返回后留在 `GameReceiver.m_resusableTypedList`（`List<TypedValue>`）。

## 目标对象（OpenChannels 的 target）

`u8 对数`，然后是若干 `(u32 代码, u32 值)`。数据库记录：

| 代码 | 含义 | 例 |
| --- | --- | --- |
| `" 011"` | 记录类型 | `0x1c` |
| `Guid` | 当前载入存档的会话 ID（每次载入都变） | `0x00952aab` |
| `ferd` | 记录 ID：高 8 位是种类，低 24 位是索引 | `0x010baef4` |

种类：`01` 人物，`18` 俱乐部，`19` 赛事，`03` 国家。

C# 端对应 `FM.UI.DatabaseRecordReference`：`m_index` 在 `0x1c`，`Type` 在 `0x20`。

## 打开一个通道（主动查询）

界面的流程，桥照做：

1. `AddNode(key, parent)`：把 key 登记到引擎的绑定树
2. `SetPropertyID(key, property)`
3. （界面还会 `SetTarget(key, dataKey)`；查询数据库记录时不需要）
4. `OpenChannels(target, [{property, generation, key}])`：generation 要比界面用过的都大
5. 用完 `CloseChannel` + `RemoveNode`

桥的 key 都以 `0xF27E` 开头。回给这些 key 的数据在调用原方法时临时摘掉 `GameReceiver.m_onChannelDataChange`，不让界面收到不认识的 key。

## 属性代码

属性 ID 是 4 字节代码，多数是英文单词倒写。`catalog.tsv` 有全部 1,031 种引用类型、16,258 个属性和值类型（正式版没有文字说明）。

已确认：

| 代码 | 对象 | 含义 | 例 |
| --- | --- | --- | --- |
| `eman` | 人物/俱乐部 | 名字（带链接的富文本） | 「David Wang」「莱斯特城」 |
| `dinu` | 人物/俱乐部 | 唯一 ID（UID），对应 `graphics/faces/face_<UID>.png` | 2002170222、673 |
| `u36b` | 人物 | 名字缩写 | 「DW」 |
| `VNco` | 俱乐部 | 全称 | 「莱斯特城足球俱乐部」 |
| `maeT` | 人物 | 所属球队（引用） | |
| `Guid` | 目标 | 存档会话 | |

## 文本格式

带链接的文字：`\x01` + base64（被链接对象的序列化引用，含 `ferd`）+ `\x02显示文字\x01`。
