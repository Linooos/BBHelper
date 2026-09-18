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
```

> ⚠️ `AgentClient` 的 `identifier` / `connected` / `custom_recognition_list` /
> `custom_action_list` 都是 **property，不是方法**；`connect()` / `bind()` /
> `disconnect()` 才是方法。`TaskDetail.status` 是 `Status` 对象，用
> `.succeeded`（property）判成败。

### 重定向输出时注意

`python ... | tail` 会缓冲到进程结束才输出，看起来像卡死。
要么直接 `> file 2>&1`，要么加 `-u`，并配 `PYTHONIOENCODING=utf-8`
（Windows 控制台默认 GBK，中文日志会乱码）。

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

> 已入库模板：`Lobby/TaskCenterIcon.png`（大厅左侧任务中心图标，TemplateMatch 实测 score 1.0）。
> 裁剪时已剔除右上角通知红点，避免红点出现/消失导致失配。

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
