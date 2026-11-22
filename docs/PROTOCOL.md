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

种类就是 `FM.UI.DatabaseTableType` 的值（不是按声明顺序）：`01` 人物，`03` 俱乐部，`0a` 国家，`18` 球队（一线队、预备队等），`19` 赛事。

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
| `VNco` | 球队 | 俱乐部全称 | 「莱斯特城足球俱乐部」 |
| `  51` | 人物 | 年龄 | 21 |
| `  81` | 人物 | 身高（厘米） | 185 |
| `Ytan` / `Ttan` | 人物 | 国籍 / 全部国籍 | 「荷兰」/「荷兰, 英格兰」 |
| `SspP` | 人物 | 位置 | 「门将」 |
| `tnsP` | 人物 | 性格 | 「平衡」 |
| `cpsl` | 人物 | 语言 | 「英语(流利), 荷兰语(流利)」 |
| `aC5W` / `cdbp` | 人物 | 简介 / 生涯介绍 | |
| `maeT` | 人物 | 所属球队（引用） | `18000230` |
| `lpLF` | 球队 | 一线队球员名单（列表） | 31 人 |
| ` Vj5` | 球队 | 职员名单（主教练在前） | 48 人 |

`tools/fm27.py` 里的 `PERSON_FIELDS` 有完整的已对照字段。

## 动态引用（DynamicReference）

很多值（社交圈、偏爱的人、幸福度、球队动态……）是 `SI.Bindable.DynamicReference`：它本身就是 `Dictionary<uint, TypedValue>`（属性代码 → 值），值随回复一起到达，不需要再查询。桥输出 `{"dyn": "<类型 ID>", "props": {...}}`，逐层展开。

字典条目 24 字节：`hashCode@0, next@4, key@8, value@16`（注意：数组接口对这个共享泛型报告的元素大小是 8，不能用）。

其他 `InteropReference` 子对象输出 `{"target": "<hex>"}`，可以用 `/query?target=<hex>&props=...` 继续查询。

社交相关代码见 [SOCIAL.md](SOCIAL.md)。

## 日期

`GameDate` 打包为 `年 << 17 | 一年中的第几天（从 1 开始）<< 8 | 时刻`。例：`262948864` = 2006-03-13。桥输出 `YYYY-MM-DD`。

## 文本格式

带链接的文字：`\x01` + base64（被链接对象的序列化引用，含 `ferd`）+ `\x02显示文字\x01`。

## Matches and news (2026-11-21)

- A player's last-five form: `ZaLo` / `E2cn` / `phIw` (LastFiveMatchesFormReference) → `XkGV` rows with
  `QSIW` = FixtureReference target, `AB5g` rating, `gaVE` substitution text.
- FixtureReference (target type 50): `etdf` date, `Tmhf` home team, `Twaf` away team, `shxf`/`saxf` home/away
  goals (text; empty before the match), `srxf` "4 - 0", `poNF` competition, `kT1 ` home ratings and `zKyn`
  away ratings (`rylp` person, `  52` rating), `jTAa` our team's ratings, `6dsa` goals (`rylp` scorer, `I2f0`
  assister, `t2lQ` minute), `LmjO.yanz` player of the match (`XcoC` rating), `ilf2` scorers text, `nzXO`
  match news items. FM text carries zero-width spaces (U+200B).
- Manager `SWEN`: inbox, a list of NewsItemReference targets (type 70; pair `dinu` = news id). Props: `ltit`
  title, `ydob` body (links = people/clubs), `AMqC` date, `spin` sender.

## Press conference (recorded 2026-11-21, rec-press-conference.jsonl)

- Same SpeakTo system as player conversations. `itca.N2w4` = tone groups (`DwoR.lWh9` 积极/中立/消极,
  `izOE` 1/2/4), `T3E4` = answers (`lWh9` text, `p3fs` id, `neve` = event id 943147586).
- `itca.nzfT` = the journalist asking: `h1J ` person, `xj09[].nrtS` question (FM text with links),
  `gmyy.Xr0u` journalist body language. `itca.j1J` = journalists with questions.
- `1VLL` extra actions: 无可奉告 (event 2001817170), 暴怒离场 (event 827217993).
- Start: event 863384693; answer: event 943147586 to the SpeakTo target, data record
  {`h1J ` journalist ref, `Vpcz` -1, `p3fs` option id, `pQuB` 2}. Leaving early auto-answers the remaining
  questions (a burst of answer events, p3fs 5). Leave: event 31.
- Live session: PressConferenceEditSessionReference (target type 81, last_target): `gM8V` journalists present
  (InteractionPersonAndBodyLanguage: `srep` person, `s1P2` body language), `85GP` current journalist,
  `U0DX` manager body language. Journalist outlet: person `SdeM` (MediaSourceReference, `eman`).

## Safety notes

- Only query targets the interface itself opened (last_target, values the game returned). A hand-made
  target (e.g. another SpeakTo kind with a borrowed key) can crash the game inside its engine.
- Value encoding runs under a fault guard (tap.c encode_guarded): an unreadable object costs that one
  value (null), not the game.
