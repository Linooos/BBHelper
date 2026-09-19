# 开发与调试循环

> 本文记录本项目开发期实测有效的调试方式，以及踩过的坑。
> 游戏侧的界面事实见 [game_recon.md](./game_recon.md)。

## 0. 一句话版本

| 场景 | 用什么 |
| --- | --- |
| 纯 OCR / TemplateMatch / Click 流程 | **MaaMCP** 的 `run_pipeline` |
| 涉及 `CustomRecognition` / `CustomAction` 的流程 | **`tools/dev_run.py`**（自己起 Agent） |
| 最终用户运行 | MFAAvalonia / MaaPiCli（正式产物走 CI 打包） |

---

## 1. pipeline JSON 必须是无注释的严格 JSON ⚠️

`assets/interface.json` 可以是 **jsonc**（可以写 `//` 注释）——模板本身就是这么写的，
MaaFramework 用 jsonc 解析器读它。

**但 `assets/resource/pipeline/**/*.json` 必须是严格 JSON**。原因：
MaaMCP 在加载 pipeline 文件时会先用严格 JSON 解析器解析一遍，
带注释会直接报
`Pipeline JSON 解析失败: Expecting property name enclosed in double quotes`。

节点级的说明请用 schema 允许的 `doc` / `desc` 字段承载（`jsonComments` 白名单里
只允许 `$` 前缀、`*_code`、`*_doc`/`doc`、`*_desc`/`desc`）。
**不要用 `"//"` 当注释键**——它不在白名单里，会违反 `unevaluatedProperties: false`。

校验：

```bash
node -e 'const fs=require("fs");for(const f of fs.readdirSync("assets/resource/pipeline")){try{JSON.parse(fs.readFileSync("assets/resource/pipeline/"+f,"utf8"));console.log("OK "+f)}catch(e){console.log("FAIL "+f+" "+e.message)}}'
```

语义检查（`next` 引用完整性、interface 的 entry 是否存在）：

```bash
npx @nekosu/maa-tools check     # EXIT=0 即通过
```

---

## 2. MaaMCP 的 Resource 缓存陷阱 ⚠️

MaaMCP 把 Resource 缓存在一个 Tasker 上。**改完 pipeline 文件后**：

- `clear_pipeline_resources()` **不够**——它重置了 Resource，但 Tasker 仍绑在旧的
  （现在是空的）Resource 上，表现为 `Context::get_pipeline_data task not found`，
  任务瞬间失败且**不报 OCR 错误**。
- **正确的做法是重新 `connect_adb_device()`**，这会带出新的 Tasker + Resource 对。

另外注意：`run_pipeline` 内部是用 `override_pipeline` 把节点灌进去的，而
**`pipeline_override` 只能覆盖已存在的节点，不能凭空创建节点**。所以新加的节点
必须先随 Resource bundle 一起加载（即文件要放在 `assets/resource/pipeline/` 下），
重连之后才可见。

> 症状对照：任务 `status: failed`、`nodes` 里只有一个空名节点 → 十有八九是节点没找到，
> 不是识别参数问题。先去 `%LOCALAPPDATA%\MaaXYZ\MaaMCP\debug\maafw.log` 里搜
> `task not found` 确认。

---

## 3. OCR 模型

MaaFramework 从 `<resource_root>/model/ocr/` 读 OCR 模型。本项目：

```bash
mkdir -p assets/resource/model/ocr
cp -r assets/MaaCommonAssets/OCR/ppocr_v6/small/. assets/resource/model/ocr/
```

`assets/resource/model/.gitignore` 里只有一行 `ocr`，所以模型不会入库——
这是模板的既定约定，正式发布由 `tools/configure.py` 在打包时注入。

> 模型缺失时 OCR 静默失败：日志里是 `Failed to load det or rec` + `ocrer_ is null`，
> 任务侧只表现为识别不到。

MaaMCP 自己还有一份独立模型（`%LOCALAPPDATA%\MaaXYZ\MaaMCP\resource\model\ocr`），
可用 `check_and_download_ocr()` 补；它只影响 MaaMCP 自己的 `ocr` 工具。

---

## 4. Agent（Custom\* 节点）—— 用 `tools/dev_run.py`

### 问题

MaaMCP 的 `run_pipeline(start_agent=True)` 在本项目的开发布局下**无法建立 Agent 会话**：
MaaFramework 日志报 `session_.recognition=false`，紧接着 `recognition is null`，
所有 Custom 节点静默失败。

根因是路径语义：`interface.json` 的 `agent.child_exec` / `child_args` 按
**interface.json 所在目录**解析，而开发布局下 interface.json 在 `assets/` 下，
`agent/` 却在项目根。模板默认的 `./agent/main.py` 只在**发布布局**
（`install.py` 把 interface.json 和 agent/ 平铺到 `install/` 根）下成立。

**因此 `interface.json` 的 `agent` 块按发布布局写（`./agent/main.py`），
不要为了迁就 MaaMCP 改成 `../`** —— `tools/dev_run.py` 自己拉起 agent 子进程、
根本不读这个块，改了只会让发布产物出错。

### 解决

`tools/dev_run.py` 绕开 MaaMCP，直接驱动 MaaFramework，行为等价于 MFAAvalonia：

```bash
# 只连接并列出已注册的自定义识别/动作（快速自检）
.venv/Scripts/python.exe tools/dev_run.py --list-only

# 跑指定入口
.venv/Scripts/python.exe tools/dev_run.py Daily_CleanUp
```

它会：找 ADB 设备（**自动排除真机 PJZ110**）→ 加载 `assets/resource` →
拉起 `agent/main.py` 子进程并建立 socket → 打印注册表 → 执行入口 → 打印每个
节点的识别框与分数。

前置：项目内 venv 与 MaaFw

```bash
uv venv && uv pip install MaaFw
# 开发期额外装 Pillow，用于从实机截图裁剪 TemplateMatch 模板（见下）
uv pip install Pillow
```

> **Pillow 只是开发期工具**：用来把实机截图裁成 `assets/resource/image/` 下的模板图。
> 它不被 `agent/` 导入，也不进发布产物，所以不影响打包。
>
> ⚠️ `AgentClient` 的 `identifier` / `connected` / `custom_recognition_list` /
> `custom_action_list` 都是 **property，不是方法**；`connect()` / `bind()` /
> `disconnect()` 才是方法。`TaskDetail.status` 是 `Status` 对象，用
> `.succeeded`（property）判成败。

### 重定向输出时注意

`python ... | tail` 会缓冲到进程结束才输出，看起来像卡死。
要么直接 `> file 2>&1`，要么加 `-u`，并配 `PYTHONIOENCODING=utf-8`
（Windows 控制台默认 GBK，中文日志会乱码）。

---

### 4.5 VS Code 的 Maa Pipeline Support 插件（推荐优先用它）

插件（`nekosu.maa-support`）**能**启动 Agent，而且是仓库文档明确推荐的调试方式
——「只有 Maa Pipeline Support 插件支持在运行 pipeline 时自动启动 debug session
并自动插入 socket id」。有断点、能单节点调试，比 `dev_run.py` 好用。

> 更正一个容易误解的说法：**「MaaMCP 起不了 Agent」只限制 MaaMCP 自己**，
> 不影响插件。三者分工：插件（交互调试）＞ `dev_run.py`（命令行批量验证）＞
> MaaMCP（纯 OCR/模板匹配的快速探查）。

### ⚠️ 开发期保持 `interface.json` 的 `agent` 块为**注释状态**

这是最容易踩的坑，而且我踩过：给 `agent` 块填上 `child_exec` 之后，
插件反而连环报 `Python was not found` / `ModuleNotFoundError: No module named 'maa'`。
**取消注释才是错的。**

原因在插件源码 `buildRuntime`：

```js
result.agent = [];
for (const agent of agents) {
    if (!agent.child_exec) continue;   // 没填 child_exec 就整个跳过
    ...
}
```

- **不填 `agent` 块** → 插件**跳过**它，改走自己的机制：VS Code 的 `maa-launch`
  debug session，用你（或 Python 扩展）选定的解释器启动 AgentServer。**本来就能用。**
- **填了 `child_exec`** → 被拽去「用终端命令启动」那条路，而那条路依赖系统 PATH 上
  有可用的 `python`。本机没有系统 Python，PATH 上的
  `C:\Users\...\AppData\Local\Microsoft\WindowsApps\python.exe` 又只是个
  **Microsoft Store 转接存根**（`AppInstallerPythonRedirector.exe`），于是：
  - 没命中任何解释器 → `Python was not found`
  - 命中了别的解释器 → `No module named 'maa'`

**两种报错同一个根因：走了不该走的那条启动路径。**

> 同时也要注意 **开发布局与发布布局不一致**：调用方以 interface.json 所在目录为 CWD，
> 开发布局下 `./agent/main.py` 会解析成 `assets/agent/main.py`（不存在）。
> 表现就是插件去跑了一份 `agent/` 的**副本**。**千万别复制 agent/ 到 assets/ 下** ——
> 两份代码以后必然漂移，改了这份跑那份，极难排查。

### 发布产物需要 `agent` 块

MFAAvalonia 靠它启动 AgentServer，没有的话所有 `CustomRecognition` /
`CustomAction` 节点都会失效。所以由 `tools/install.py` 在打包时写入
（`install_resource` 里，与改写 `version` 放在一起）：

```python
interface["agent"] = {
    "child_exec": "python",
    "child_args": ["./agent/main.py"],
}
```

> 插件另有个 `{PROJECT_DIR}` 变量（= interface.json 所在目录）可用，
> 但那是**插件私有**变量，MaaFramework 本体不认，会把 interface.json 弄成插件专用。
> 本项目不用它。

---

## 5. 坐标系

**1280 × 720 横屏**，短边即 720，所以坐标空间就是 1280×720，
与 MaaMCP 截图完全一致。MaaFramework 日志里也确认
`[image_raw_width_=1280] [image_raw_height_=720]`。

所有 `roi` / `target` / 模板图都按这个尺度写。

### 取模板图

`screencap(region=..., resolution=null)` 拿到的是**原生像素**；
**不要用 `resolution=720` 之类**——它会把小裁剪图放大
（实测 56×56 被缩成 18×18），TemplateMatch 必然失配。

裁剪 → `Read` 视觉确认 → `save_captured_image(bundle_root="assets/resource", subcategory=..., name=...)`
→ 落到 `assets/resource/image/<subcategory>/<name>.png`，
pipeline 里用 `"template": "<subcategory>/<name>.png"` 引用。

> 已入库模板：
>
> - `Lobby/TaskCenterIcon.png` —— 大厅左侧任务中心图标（实测 score 1.0）。
>   裁剪时已剔除右上角通知红点，避免红点出现/消失导致失配。
> - `Battle/VictoryFigure.png` —— 通关的「胜利」图案（兜底判据，见 game_recon.md）。
>   裁剪时刻意避开右边缘的玩家角色与下方文字。

**从已有的截图文件裁剪**（比如用户手动截的图）：用 venv 里的 Pillow，
注意 Windows 的 Python 不认 MSYS 风格路径（`/tmp/...`），要传 `D:\...` 这样的原生路径。

```python
from PIL import Image
src = Image.open(r"C:\...\某张1280x720的截图.png")
src.crop((x0, y0, x1, y1)).save(r"D:\...\debug\tpl\name.png")
```

裁完先 `Read` 出来**视觉确认**，再用 `save_captured_image` 存进资源包。
裁剪时务必注意：**模板里混进背景或玩家角色 = 换关卡就失配**。
优先裁「图案自身不透明、且不含角色」的区域。

---

## 5.5 `next` / `[JumpBack]` 的真实语义 ⚠️ 最容易踩的坑

用最小实验实测得到（`T_A.next=[T_B, T_C]`，`T_B.next=[]`）：
**`T_B` 命中后 `PipelineTask::run` 直接 leave，`T_C` 根本没跑。** 即

> **`next: []` 的节点 = 「下降」终点，会终止整条任务。**

而 `[JumpBack]X` 是**子程序调用**：X 的链走完后**返回调用方，调用方从头重新求值整个 `next` 列表**。

这条规则解释了本项目遇到过的全部怪现象：

| 现象 | 原因 |
| --- | --- |
| `[JumpBack]Common_PopupHandle` 反复执行、靠 `max_hit` 才刹住 | 它内部是 `DirectHit`（恒命中），子程序返回后父节点重新求值，又命中它 → 死循环 |
| `Summon_DismissReward`（普通成员）→ `Common_NoticeConfirm`（`next: []`）→ 任务突然结束 | 普通成员是「下降」，碰到空 next 就终止；`[JumpBack]` 边界才能「返回」 |
| `[JumpBack]Nav_GotoLobby` → `Nav_LobbyTab`（`next: []`）却不终止、只是循环 | 它处在 `[JumpBack]` 子程序内，空 next 表示「子程序结束、返回调用方」 |

**由此得到三条硬性写法：**

1. **共享节点（`Common_*`）一律以 `[JumpBack]` 调用**，并且**必须由 OCR 门控**
   （有弹窗才命中）。不要把 `DirectHit` 包一层再拿出去反复调用。
2. **节点做完了要继续走，就必须有非空 `next`**。想让它「结束并返回」，
   就把它作为 `[JumpBack]` 子程序的终点（`next: []`）。
3. **恒命中的节点（DirectHit）不要放进会被反复求值的 `next` 列表**
   ——要么加 `max_hit` 兜底，要么改成 OCR 门控，要么用 `[JumpBack]`。

诊断手段：把 MaaFramework 日志目录打开（`tools/dev_run.py` 已经设好，
日志落在 `debug/maafw.log`），搜
`recognize_list] [cur_node_=` 与 `reco hit`，就能看到真实的候选列表与命中顺序
——比看 `TaskDetail.nodes` 可靠得多（后者**包含被尝试但未命中的节点**）。

### 5.6 跨页导航：用「线性下降链」，**不要**用 `[JumpBack]` 降级链 ⚠️

早期版本把导航写成「链式降级」的 `[JumpBack]` 子程序
（`Nav_GotoMail → Nav_GotoSocial → Nav_GotoLobby`），结果**反复空转**：
子程序返回后调用方从头重新求值，又命中同一个导航节点，如此往复，
最后把 `max_hit` 耗尽、任务直接散架（`status: failed`）。

正确做法基于一个简单事实：

> **顶部六个标签（首页/战斗/装备/商店/社交/其他）在任何页面都可见。**

所以「点首页」从哪儿都能回大厅；回到大厅后任务中心图标必然可匹配。
也就是说**根本不需要降级分支**——一条直线就够了，每步的 `next` 指向下一步：

```text
<Phase>_Entry.next = ["<Phase>_Work", "<Phase>_Nav"]     # 先试活，已在目标页就直接干
<Phase>_Nav:          DirectHit → next: ["<Phase>_Nav01Lobby"]
<Phase>_Nav01Lobby:   OCR「首页」→ Click → next: ["<Phase>_Nav02TaskCenter"]
<Phase>_Nav02TaskCenter: TemplateMatch 图标 → Click → next: ["<Phase>_Nav03Tab"]
<Phase>_Nav03Tab:     OCR 目标标签 → Click → next: ["<Phase>_Loop"]
```

要点：

1. **每一步的 `next` 都必须非空**（空 next 是「下降」终点，会终止整条任务）。
2. **导航只在阶段入口走一次**，循环体（`<Phase>_Loop`）里绝不放导航节点，
   否则「活干完了、识别不到」时会把导航反复重跑。
3. **导航节点按阶段复制**，不共享。共享的导航点击节点要求静态 `next`，
   而它的下一步是阶段相关的——共享会逼你回到 `[JumpBack]` 的老路。
4. 恒命中的按钮（顶部标签、常驻的「一键领取」等）**必须限 `max_hit`**：
   OCR 读得到文字 ≠ 按钮可点（置灰也读得到），不限次就会死循环。

改完之后整个日常流程的命中轨迹变成**每个节点恰好一次、零空转**：

```text
Daily_CleanUp → Mail_Entry → Mail_Loop → Mail_PhaseDone
→ Summon_Entry → Summon_Nav(+3步) → Summon_Loop → Summon_Settle → Summon_PhaseDone
→ Presence_Entry → Presence_Nav(+3步) → Presence_Loop → Presence_ClaimMilestone
→ …三个 Gate… → Presence_PhaseDone
→ Daily_Stamina → … → Daily_Finish
```

---

### 5.7 `timeout` 是「父节点等 next 命中」的预算 ⚠️

这条很容易误读。`timeout` **不是**「本节点自己的识别超时」，而是
**「本节点在放弃之前，愿意花多久等它的 `next` 候选命中」**。

踩坑现场：进关卡时

```jsonc
"Battle_StartBattle": {          // 点「开战」
    "next": ["Battle_WaitLoaded"],
    "timeout": 3000              // ← 以为「3 秒内识别不到就重试」，其实是「3 秒后放弃」
}
"Battle_WaitLoaded": {           // 等战斗界面加载完
    "timeout": 30000             // ← 根本轮不到生效
}
```

日志报 `Task timeout [pretask.name=Battle_StartBattle] [reco_timeout=3000ms]`，
而关卡加载要十几秒 → 整条链直接放弃。

**规则**：如果某个节点的 `next` 要等一个**慢事件**（关卡加载、页面切换、弹窗出现），
**必须把 `timeout` 设在那条链的父节点上，且覆盖慢事件的完整耗时**。

### 5.8 读日志定位问题比猜快得多

本次三个问题全是靠 `debug/maafw.log` 一行定位的：

| 现象 | 日志关键行 |
| --- | --- |
| 摇杆开局向下漂 | （靠坐标复测 + 准星对比发现 y 偏 5px） |
| 双倍券没点上 | `TemplateMatcher::analyze best_result_={"score":0.748974}` → 模板对「未勾选」误匹配超阈值 |
| 进关卡后卡住 | `Task timeout [pretask.name=Battle_StartBattle] [reco_timeout=3000ms]` |

常用过滤：

```bash
# 真实命中顺序
tr -cd '\11\12\15\40-\176\200-\377' < debug/maafw.log \
  | grep -aoE "reco hit \[result.name=[A-Za-z_0-9]+" | sed 's/.*name=//' | uniq

# 某节点的识别详情
tr -cd '\11\12\15\40-\176\200-\377' < debug/maafw.log | grep "OCRer::analyze"
```

### 5.9 两条会「炸整条链」的语义 ⚠️⚠️

这两条都是实机踩出来的，代价是一次失控刷关。

**① `[JumpBack]X` 返回时是「直接重新进入 X」，不会重新求值调用方的识别**

所以**挂在调用方身上的 `max_hit` 永远不会累加**：

```jsonc
"Loop": {                                    // ❌ 永远停在 1 次
    "next": ["[JumpBack]Round"],
    "max_hit": 2                             // 想刷 2 次，实际无限刷
}
```

日志证据：整轮跑完 `Loop` 只命中 1 次、`Finish` 命中 0 次，脚本无限循环刷关。

**要做计数循环，必须让每轮干完活用普通 `next` 回到一个「汇合点」**，
汇合点重新求值候选列表时计数器才会再命中一次：

```jsonc
"Cycle": { "next": ["Count", "Finish"] },    // ✅ 汇合点
"Count": { "next": ["Round"], "max_hit": 2 } // max_hit 正常累加
// 每轮链的末尾： "SettleDetect": { "next": ["Cycle"] }
```

**② 一个节点命中后下降进去，若它的 `next` 候选全部失败 → 整条任务报错终止**

不是「返回父节点继续试别的」。日志报
`Task timeout [pretask.name=X]` + `invalid node id, handle error`。

```jsonc
"Gate": { "next": ["Sub"] }      // ❌ Sub 一旦失配，整条任务炸
```

**只有同一个 `next` 列表内**的候选失配才会顺延——所以要**把条件分支拍平到一个列表里**：

```jsonc
"Round": {
    "next": ["ResultTicket", "Again", "Confirm", "PickStage", "DetailTicket", "OpenHelper", "PickHelper"]
    // 用 enabled 开关，而不是 Gate 节点 + next:[子节点]
}
```

> 本项目其它地方的 `*_Gate` 之所以没事，是因为它们链上**全是恒命中的 `DirectHit`**。
> 一旦后面挂了会失配的节点，就会炸。**归纳：Gate 只适合串恒命中节点；带条件的
> 分支一律拍平 + `enabled`。**

**③ 附带教训**：`TouchDown` 的 `auto_up` 默认 `false`。任务被外部强杀（`taskkill`）
时不会自动抬手指。所有 `TouchDown` 都要显式 `auto_up: true`，否则会留下一个
一直按着的触点（角色举着武器不动）。

---

## 6. 写 pipeline 的几条本项目约定

1. **全部节点用 v2 object 形态**（`recognition: {type, param}` / `action: {type, param}`）。
   混用 v1 平铺形态会导致 `pipeline_override` 深合并时把整个 `action` 替换掉，
   `custom_action` 注册名直接丢失。
2. **开关用 `*_Gate` 空节点当切点**（`DirectHit` + `DoNothing`），
   靠 `interface.json` 的 `pipeline_override` 改它的 `next`。
   不要用 Flag 节点，也不要为开关写 Python 判断。
3. **`switch` 的 case 名必须是 `Yes` / `No`**（schema 硬约束，且恰好两项）。
4. **导航写成链式降级**：`Nav_GotoXxx` 依次尝试
   「本页直接命中 → 返回上一层页面再试」。命中的正确性由下游节点的识别隐式验证。
   ⚠️ 但注意：**恒能命中的节点（如点击顶部导航标签）不能放在会被反复求值的
   `next` 列表里**，否则会死循环重复点击——给这类节点加 `max_hit` 兜底。
5. 探测型节点要显式设短 `timeout`（本项目用 1500~3000ms），
   否则失败时要干等默认的 20 秒。

---

## 7. 安全红线（主号，不可违反）

- ❌ **邮件页「一键删除」(788,144)** —— 会真实删除邮件。
  `mail.json` 里所有节点的 ROI 都不得覆盖 x∈[740,845]。
  领取节点 ROI 分别锁死在 x∈[1055,1210]（一键领取）与 x∈[880,1010]（体力领取）。
  **改 ROI 前务必重新核对这条。**
- ❌ 抽卡、分解装备、消耗水晶、未在配置中显式允许的购买。
- ⚠️ 中央 `(640,483)` 的「确定」既可能是普通提示，**也可能是消耗确认框**。
  高危流程（购买/刷新）必须先把通用 `Common_NoticeConfirm` 从 `next` 里摘掉，
  改用带文案校验的专属确认节点，并挂 `guard_consumable` 安全闸。
- ✅ 消耗性选项在 `interface.json` 里一律**默认关闭**，需要用户显式打开。
