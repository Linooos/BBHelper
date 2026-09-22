import glob
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

# 复用的 OCR 基座节点名（定义在 pipeline/common.json）
OCR_BASE_NODE = "Common_OcrBase"


# AgentServer 会把 stdout 重定向掉，print 在父进程看不见 —— 调试信息写文件。
# 落在 <项目根>/debug/stamina_plan.log（debug/ 已 gitignore）。
_DEBUG_LOG = Path(__file__).resolve().parent.parent / "debug" / "stamina_plan.log"


def _dbg(msg: str) -> None:
    try:
        import time

        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG.open("a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:  # noqa: BLE001
        pass


@AgentServer.custom_action("plan_stamina_runs")
class PlanStaminaRunsAction(CustomAction):
    """读关卡详情页的「消耗体力 N」，用「体力目标」折算要刷几次，
    然后把结果写进 Battle_ClearStage_Count.max_hit。

    为什么必须是 CustomAction，而不能用纯 pipeline：
        最后一步是「按计算结果改写另一个节点的参数」——
        纯 pipeline 只能写死 max_hit，没法做除法。

    custom_action_param:
        target_stamina   体力目标（180 / 200 / 300）—— 由界面「体力目标」选项覆盖
        stamina_per_run   每次消耗。**不填就现场 OCR** 关卡详情页的「消耗体力 N」
        roi / expected    现场 OCR 的参数（关卡详情页上那一行）
        run_cap           次数上限兜底，默认 20
        count_node        要改写的计数节点，默认 Battle_ClearStage_Count

    ⚠️ 目前**没有扣掉今天已经消耗的部分** —— 直接按 target 算。后果是可能多刷几次
       （比如今天已消耗 60，仍会按满额 300 算）。等接上「读存在感进度」再改。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, argv)
        except Exception as e:  # noqa: BLE001
            import traceback

            _dbg("异常: %r\n%s" % (e, traceback.format_exc()))
            return True  # 折算失败不该拖垮整条任务，按原来的 max_hit 跑

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = json.loads(argv.custom_action_param or "{}") or {}
        _dbg("enter, param=%r" % (param,))

        target = int(param.get("target_stamina", 300))
        run_cap = int(param.get("run_cap", 40))
        count_node = param.get("count_node", "Battle_ClearStage_Count")
        base_node = param.get("base_node", OCR_BASE_NODE)

        per_run = int(param.get("stamina_per_run") or 0)

        if per_run <= 0:
            roi = param.get("roi") or [600, 515, 320, 60]
            expected = param.get("expected") or ["消耗体力"]

            # ⚠️ CustomAction 的 argv **没有 image**（那是 CustomRecognition 的 AnalyzeArg 才有的）。
            # 用控制器的缓存的截图 —— 动作跑之前 Maa 刚为节点识别截过图，所以它是新的。
            image = getattr(context.tasker.controller, "cached_image", None)
            if image is None:
                _dbg("拿不到截图（cached_image 为空），不折算")
                return True

            detail = context.run_recognition(
                base_node,
                image,
                pipeline_override={
                    base_node: {"recognition": {"param": {"roi": list(roi), "expected": list(expected)}}}
                },
            )
            if detail is None or not detail.hit:
                _dbg("没读到「消耗体力」，不折算，按原 max_hit 跑")
                return True  # 保守：不改 max_hit，就用原来的值

            text = str(getattr(max(detail.filtered_results, key=lambda r: getattr(r, "score", 0) or 0), "text", "") or "")
            m = re.search(r"(\d+)", text)
            _dbg("OCR 读到: %r" % text)
            if m is None:
                _dbg("「%s」里没抠出数字，按原 max_hit 跑" % text)
                return True
            per_run = int(m.group(1))

        if per_run <= 0:
            return True

        runs = min(int(math.ceil(target / per_run)), run_cap)
        _dbg("体力目标 %d ÷ 每次 %d = 刷 %d 次" % (target, per_run, runs))

        try:
            context.override_pipeline({count_node: {"max_hit": runs}})
        except Exception as e:  # noqa: BLE001
            _dbg("改写 %s 失败（忽略，按原值跑）: %s" % (count_node, e))

        return True


@AgentServer.custom_action("click_target")
class ClickTargetAction(CustomAction):
    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:

        print("ClickTargetAction is running!")

        param = json.loads(argv.custom_action_param or "{}") or {}
        target = param.get("target")
        if not isinstance(target, list) or len(target) != 2:
            return False

        # 执行点击
        click_job = context.tasker.controller.post_click(target[0], target[1])
        # 等待点击完成，这一步必须有！
        click_job.wait()

        return True


@AgentServer.custom_action("plan_stage_page")
class PlanStagePageAction(CustomAction):
    """读关卡列表底部的「N / M」，算出**往回点几次、往前进几次**，
    把结果写进两个翻页节点的 enabled / max_hit。

    为什么必须是 CustomAction：
        「跳到第 N 页」= 先退回第 1 页、再前进 N-1 次 —— 这要读 OCR 出来的
        「2 / 2」做减法和判断。纯 pipeline 只能匹配文本，算不了数，
        也没法按结果决定一个节点跑几次。

    ═══ 为什么要先退回第 1 页 ═══
    活动关卡列表的分页是**滞留**的（实测：退出去再进来还停在上次那页，
    2026-09-20 在「休诊时间」上复现）。不知道当前在第几页就没法算前进次数，
    所以统一先退到底再前进 —— 这样结果只由「目标页数」决定，和进来时停在哪无关。

    custom_action_param:
        target_page   目标页数（界面「页数」选项覆盖这里）
        roi           读页码的 ROI
        base_node     复用的 OCR 基座节点名（默认 Common_OcrBase）
        back_node     往回翻的节点名
        fwd_node      往前翻的节点名
        page_cap      页数上限保护：读出来的页数超过它就当读失败
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, argv)
        except Exception as e:  # noqa: BLE001
            import traceback

            _dbg("plan_stage_page 异常: %r\n%s" % (e, traceback.format_exc()))
            return True

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = json.loads(argv.custom_action_param or "{}") or {}
        _dbg("plan_stage_page 进入, param=%r" % (param,))

        target = int(param.get("target_page", 1))
        roi = param.get("roi") or [590, 635, 100, 55]
        base = param.get("base_node", OCR_BASE_NODE)
        back_node = param.get("back_node", "Stamina_Custom_PageBack")
        fwd_node = param.get("fwd_node", "Stamina_Custom_PageFwd")
        cap = int(param.get("page_cap", 30))

        def give_up(why: str) -> bool:
            """读不出来就别翻页 —— 两个都关掉，让流程自己往下走（会走 GiveUp）。"""
            _dbg("%s；两个翻页节点都关掉" % why)
            try:
                context.override_pipeline(
                    {back_node: {"enabled": False}, fwd_node: {"enabled": False}}
                )
            except Exception as e:  # noqa: BLE001
                _dbg("关掉翻页节点失败: %s" % e)
            return True

        image = getattr(context.tasker.controller, "cached_image", None)
        if image is None:
            return give_up("拿不到截图（cached_image 为空）")

        detail = context.run_recognition(
            base,
            image,
            pipeline_override={
                base: {
                    "recognition": {
                        "param": {"roi": list(roi), "expected": ["/"]}
                    }
                }
            },
        )
        if detail is None or not detail.hit:
            return give_up("页码 ROI 里没读到带「/」的文字")

        best = max(detail.filtered_results, key=lambda r: getattr(r, "score", 0) or 0)
        text = str(getattr(best, "text", "") or "")
        m = re.search(r"(\d+)\s*/\s*(\d+)", text)
        _dbg("OCR 读到页码: %r" % text)
        if m is None:
            return give_up("「%s」里抠不出 N/M" % text)

        current, total = int(m.group(1)), int(m.group(2))
        if not (1 <= current <= total <= cap):
            return give_up("页码 %d/%d 不合理（上限 %d）" % (current, total, cap))

        target = max(1, min(target, total))
        back = current - 1
        fwd = target - 1
        _dbg(
            "当前 %d/%d，目标第 %d 页 → 后退 %d 次、前进 %d 次"
            % (current, total, target, back, fwd)
        )

        try:
            context.override_pipeline(
                {
                    # max_hit 是整条任务范围的累加器；enabled 决定这一趟翻不翻。
                    # 注意 max_hit 必须 ≥1，否则节点一次都跑不了。
                    back_node: {"enabled": back > 0, "max_hit": max(back, 1)},
                    fwd_node: {"enabled": fwd > 0, "max_hit": max(fwd, 1)},
                }
            )
        except Exception as e:  # noqa: BLE001
            _dbg("改写翻页节点失败（按默认跑）: %s" % e)

        return True


# ── 抢进图的轮次计数 ────────────────────────────────────────────────
# 为什么需要 Python：`max_hit` 是**整条任务范围累加**的（不是每次进入重置），
# 拿它当「三轮」的刹车 ⟹ 第一个 episode 用掉 3 次之后，后面每一轮都是 0，
# 抢进图等于只能用一次。而一次日常要跑几十轮。所以只能在 Python 里自己记数，
# 每次进入抢进图时归零。
class _RushState:
    rounds = 0


@AgentServer.custom_action("rush_reset")
class RushReset(CustomAction):
    """每次**进入**抢进图时调一次：轮次归零，并把循环节点重新打开。

    必须在每个入口都调（首次进关、以及每次「再次挑战」之后），
    否则上一轮把循环关掉之后就再也打不开了。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            param = json.loads(argv.custom_action_param or "{}") or {}
            hub = param.get("hub_node", "Battle_RushAgain")
            _RushState.rounds = 0
            try:
                context.override_pipeline({hub: {"enabled": True}})
            except Exception as e:  # noqa: BLE001
                _dbg("rush_reset 打开 %s 失败: %s" % (hub, e))
            _dbg("rush_reset：轮次归零，%s 重新打开" % hub)
        except Exception as e:  # noqa: BLE001
            _dbg("rush_reset 异常: %r" % (e,))
        return True


@AgentServer.custom_action("rush_tick")
class RushTick(CustomAction):
    """抢进图**每跑完一轮**调一次：计数，到上限就把循环节点关掉。

    关掉之后 Battle_RushGate 的候选会顺延到 Battle_OpenHelperList（老路）。
    ⚠️ 这里用 enabled 而不是 max_hit —— 见上面 _RushState 的说明。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            param = json.loads(argv.custom_action_param or "{}") or {}
            hub = param.get("hub_node", "Battle_RushAgain")
            cap = int(param.get("rounds", 3))
            _RushState.rounds += 1
            _dbg("rush_tick：第 %d/%d 轮" % (_RushState.rounds, cap))
            if _RushState.rounds >= cap:
                try:
                    context.override_pipeline({hub: {"enabled": False}})
                except Exception as e:  # noqa: BLE001
                    _dbg("rush_tick 关掉 %s 失败: %s" % (hub, e))
                _dbg("rush_tick：已跑满 %d 轮，关掉循环，交给老路" % cap)
        except Exception as e:  # noqa: BLE001
            _dbg("rush_tick 异常: %r" % (e,))
        return True


# ── 对齐 MuMu 的 ABI 配置文件（移植自用户那份 PowerShell 脚本）────────────
# 原脚本：C:\Users\...\SelfDocument\Shared\bb\adb修改x86文件一键脚本对齐字符.ps1
# 干的事：pull 出 MuMu 的 ABI 选择配置 → 把游戏那一行的 ABI 改成 x86、
#         按固定列对齐 → push 回去 → 重启模拟器生效。
ABI_REMOTE_DEFAULT = "/data/system/etc/mumu-configs/abi-select-android12.config"
# 配置文件里「应用」那一栏**本身就是正则**，所以点是转义的 —— 照抄原脚本
ABI_PKG = r"com\.miHoYo\.HSoDv2Original"
ABI_TARGET = " x86"     # 注意前导空格，原脚本就是这么写的
ABI_POS = 49            # ABI 的起始列（1-based）
ABI_HASH_POS = 67       # 注释 # 的起始列
MUMU_MANAGER = r"C:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe"
# 真机黑名单（与 tools/dev_run.py 一致）—— 万一 adb devices 里出现这些序列号，绝不碰
REAL_DEVICE_MARKERS = ("PJZ110", "f34e7c1e")


def _find_adb() -> str:
    """在磁盘上找 MuMu 自带的 adb（版本号那层目录用通配，不写死 15.0）。

    ⚠️ 这里**不能**用 `maa.toolkit.Toolkit.find_adb_devices()` ——
    AgentServer 子进程里 Toolkit 不可用（见 FixAbiConfigAction 的说明）。
    """
    for pat in (
        r"C:\Program Files\Netease\MuMu\nx_main\adb.exe",
        r"C:\Program Files\Netease\MuMu\nx_device\*\shell\adb.exe",
    ):
        for hit in sorted(glob.glob(pat), reverse=True):
            if Path(hit).exists():
                return hit
    return shutil.which("adb") or ""


def _pick_loopback_device(adb: str) -> str:
    """从 `adb devices` 里挑出模拟器那条（`127.0.0.1:xxxx`）。

    ⚠️ **只认 loopback** —— 真机的序列号是硬件串号（或局域网 IP），
    结构上就进不来这一条。这是「绝不碰真机」这条红线在这里的实现方式。
    """
    if not adb:
        return ""
    try:
        r = subprocess.run([adb, "devices"], capture_output=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        _dbg("adb devices 跑不起来: %r" % (e,))
        return ""
    out = (r.stdout or b"").decode("utf-8", "ignore")
    for ln in out.splitlines()[1:]:
        parts = ln.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        serial = parts[0]
        if not serial.startswith("127.0.0.1:"):
            continue
        if any(m in serial for m in REAL_DEVICE_MARKERS):
            continue
        return serial
    _dbg("adb devices 里没有可用的 loopback 设备:\n%s" % out)
    return ""


@AgentServer.custom_action("fix_abi_config")
class FixAbiConfigAction(CustomAction):
    """把游戏的运行架构改成 x86（对齐 MuMu 的 ABI 配置文件）。

    ⚠️ 这是**直接改模拟器系统分区里的文件**，原脚本就是这么干的。
    改完必须重启模拟器才生效 —— 所以本动作最后一步是重启。

    ⚠️ 重启会把 ADB 连接掐断，MaaFramework 的控制器随之失效。所以：
      · 重启是**最后一步**，之后本任务立刻结束
      · 这一步失败也不报错，而是打开界面提示节点让用户手动重启

    custom_action_param:
        remote        配置文件远端路径
        prompt_node   需要手动重启时打开哪个节点（带 focus 通知）
        target_index  模拟器实例编号；不传就取控制器 info 里的，再不行按端口换算
        restart       false = 只改文件不重启（调试用）
        adb / address 手工指定 adb 路径 / 设备地址（默认自动解析）

    ⚠️ 设备信息**不能**用 `maa.toolkit.Toolkit` —— AgentServer 子进程里它不可用，
    发行版上必炸。详见 `_resolve_device` 的注释。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            self._run(context, argv)
        except Exception as e:  # noqa: BLE001
            import traceback

            _dbg("fix_abi_config 异常: %r\n%s" % (e, traceback.format_exc()))
            # 出任何意外都让用户来处理 —— 不要静默收工
            self._prompt(context, argv, "配置修改过程出错")
        return True

    # ---------- 内部 ----------

    def _param(self, argv) -> dict:
        return json.loads(argv.custom_action_param or "{}") or {}

    def _prompt(self, context: Context, argv, why: str) -> None:
        """打开界面提示节点（它带 focus 通知，会在 UI 上弹出来）。"""
        node = self._param(argv).get("prompt_node", "Config_ManualRestartPrompt")
        _dbg("需要用户手动重启：%s" % why)
        try:
            context.override_pipeline(
                {
                    node: {
                        "enabled": True,
                        # 顺带把原因写进通知正文 —— 提示节点上写死的那句是通用兜底
                        "focus": {
                            "Node.Recognition.Succeeded": {
                                "content": "⚠️ x86 配置任务需要你处理（%s）。请手动重启模拟器，否则设置不生效。" % why,
                                "display": ["log", "toast"],
                            }
                        },
                    }
                }
            )
        except Exception as e:  # noqa: BLE001
            _dbg("打开提示节点 %s 失败: %s" % (node, e))

    def _resolve_device(self, context, p) -> tuple[str, str, int, str]:
        """拿到 (adb 路径, 设备地址, MuMu 实例号, MuMuManager 路径)。

        ═══ ⚠️ 为什么不能用 maa.toolkit.Toolkit ═══
        这里原来是 `Toolkit.find_adb_devices()`，在**开发机**上跑得好好的，
        但在**发行版**里必炸 —— AgentServer 子进程里 Toolkit 不可用：

            ValueError: Toolkit is not available in AgentServer context.
              maa/library.py:134 in toolkit
              maa/toolkit.py:314 in _assign_api_properties

        2026-09-22 用户在打包版上实测：界面弹「配置修改过程出错」，
        agent 日志里是这个 traceback（debug/stamina_plan.log）。

        改成两条路，第一条不通就走第二条：
          1. 问**当前连着的控制器** —— `context.tasker.controller.info`
             实测返回：{"adb_path": "...nx_main/adb.exe",
                       "adb_serial": "127.0.0.1:16384",
                       "config": {"extras": {"mumu": {"index": 0,
                                                      "path": "C:/Program Files/Netease/MuMu"}}}}
             ⟹ adb 路径、地址、**MuMu 实例号**全都有了（实例号连端口换算都省了）
          2. 问不到就自己找：磁盘上的 MuMu adb + `adb devices` 里的 loopback 那条
             （第 2 条只认 127.0.0.1:xxxx ⟹ 真机天然排除，见 _pick_loopback_device）
        """
        adb = str(p.get("adb") or "")
        addr = str(p.get("address") or "")
        index = p.get("target_index")
        manager = MUMU_MANAGER

        if not (adb and addr):
            try:
                info = context.tasker.controller.info or {}
                adb = adb or str(info.get("adb_path") or "")
                addr = addr or str(info.get("adb_serial") or "")
                mumu = ((info.get("config") or {}).get("extras") or {}).get("mumu") or {}
                if index is None and mumu.get("index") is not None:
                    index = int(mumu["index"])
                root = str(mumu.get("path") or "")
                if root:
                    cand = Path(root) / "nx_main" / "MuMuManager.exe"
                    if cand.exists():
                        manager = str(cand)
                _dbg("控制器 info: adb=%r addr=%r index=%r" % (adb, addr, index))
            except Exception as e:  # noqa: BLE001
                _dbg("读控制器 info 失败（改走磁盘查找）: %r" % (e,))

        if not adb:
            adb = _find_adb()
            _dbg("磁盘上找到 adb: %r" % adb)
        if not addr:
            addr = _pick_loopback_device(adb)
            _dbg("adb devices 选中: %r" % addr)
        if not adb or not addr:
            raise RuntimeError("拿不到 adb 路径 / 设备地址（adb=%r addr=%r）" % (adb, addr))

        if index is None:
            try:
                index = max(0, (int(addr.rsplit(":", 1)[-1]) - 16384) // 32)
            except Exception:  # noqa: BLE001
                index = 0
        return adb, addr, int(index), manager

    def _run(self, context: Context, argv) -> None:
        p = self._param(argv)
        remote = p.get("remote", ABI_REMOTE_DEFAULT)
        do_restart = bool(p.get("restart", True))

        adb, addr, mumu_index, manager = self._resolve_device(context, p)
        _dbg("设备 %s（MuMu 实例 %s）" % (addr, mumu_index))

        tmp = _DEBUG_LOG.parent / "abi-select.config"
        tmp.parent.mkdir(parents=True, exist_ok=True)

        def run_adb(*args, timeout=20):
            return subprocess.run([adb, "-s", addr, *args], capture_output=True, timeout=timeout)

        # [1/4] pull
        run_adb("connect", addr)
        r = run_adb("pull", remote, str(tmp))
        if r.returncode != 0:
            _dbg("pull 失败，重启 adb 再试一次: %s" % r.stderr[:200])
            subprocess.run([adb, "kill-server"], capture_output=True, timeout=20)
            subprocess.run([adb, "start-server"], capture_output=True, timeout=30)
            run_adb("connect", addr)
            r = run_adb("pull", remote, str(tmp))
            if r.returncode != 0:
                raise RuntimeError("pull 配置文件失败：%s" % r.stderr[:200])

        # [2/4] 找行（newline="" 保留原始换行符，push 回去时不会把 LF 变成 CRLF）
        with tmp.open("r", encoding="utf-8", errors="surrogateescape", newline="") as f:
            lines = f.read().split("\n")
        # ⚠️ ABI_PKG 里**反斜杠是字面字符**（配置文件就这么写的，点是转义的）。
        # 所以必须用 re.escape 找**字面串** —— 直接拿它当正则会去找真的点，一行都匹配不到。
        # 2026-09-22 实测踩过：pull 下来 178 行，这个正则命中 0 行。
        idx = [i for i, ln in enumerate(lines) if re.search(re.escape(ABI_PKG), ln)]
        if not idx:
            raise RuntimeError("配置里找不到 %s" % ABI_PKG)
        i = idx[0]
        old_line = lines[i]
        _dbg("原行: %r" % old_line)

        # [3/4] 改行（列对齐算法**照抄原脚本**，包括它的 max(…) 边界）
        parts = old_line.split()
        old_abi = parts[1] if len(parts) > 1 else ""
        if not old_abi:
            raise RuntimeError("从这一行里解析不出 ABI：%r" % old_line)
        _dbg("当前 ABI: %r" % old_abi)

        # ⚠️ 必须**去掉空格**再比：ABI_TARGET 带前导空格（原脚本就这么写的），
        #    而 split() 出来的 parts[1] 没有 ⟹ 直接 `old_abi == ABI_TARGET`
        #    永远为假，已经改好的机器也会被重写一遍、白重启一次模拟器。
        #    2026-09-22 实测踩到（第二次跑仍然「已写入」）。
        if old_abi.strip() == ABI_TARGET.strip():
            _dbg("已经是 %r，无需修改，也不必重启" % ABI_TARGET)
            tmp.unlink(missing_ok=True)
            return

        m = re.search(r"\s+(#.*)$", old_line)
        comment = m.group(1) if m else ""
        pkg_space = ABI_PKG + " "
        prefix = pkg_space + " " * max(0, ABI_POS - 1 - len(pkg_space)) + ABI_TARGET
        new_line = prefix + " " * max(1, ABI_HASH_POS - len(prefix)) + comment
        _dbg("新行: %r" % new_line)
        lines[i] = new_line + ("\r" if old_line.endswith("\r") else "")
        with tmp.open("w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write("\n".join(lines))

        # [4/4] push
        r = run_adb("push", str(tmp), remote)
        if r.returncode != 0:
            _dbg("push 失败，重启 adb 再试一次: %s" % r.stderr[:200])
            subprocess.run([adb, "kill-server"], capture_output=True, timeout=20)
            subprocess.run([adb, "start-server"], capture_output=True, timeout=30)
            run_adb("connect", addr)
            r = run_adb("push", str(tmp), remote)
            if r.returncode != 0:
                raise RuntimeError("push 配置文件失败：%s" % r.stderr[:200])
        tmp.unlink(missing_ok=True)
        _dbg("已写入 %s" % remote)

        if not do_restart:
            _dbg("restart=false，跳过重启")
            return

        # 最后一步：重启模拟器（会掐断 ADB，之后控制器失效 —— 所以放在最后）
        mi = mumu_index
        if not Path(manager).exists():
            self._prompt(context, argv, "找不到 MuMuManager.exe（%s）" % manager)
            return
        try:
            rr = subprocess.run(
                [manager, "control", "--vmindex", str(mi), "restart"],
                capture_output=True, timeout=90,
            )
        except Exception as e:  # noqa: BLE001
            self._prompt(context, argv, "调用 MuMuManager 失败: %r" % (e,))
            return
        out = (rr.stdout or b"").decode("utf-8", "ignore") + (rr.stderr or b"").decode("utf-8", "ignore")
        _dbg("MuMuManager restart 返回 %d: %s" % (rr.returncode, out[:300]))
        if rr.returncode != 0:
            self._prompt(context, argv, "MuMuManager 重启失败（返回 %d）" % rr.returncode)
        else:
            _dbg("已发出重启指令（实例 %s），配置将在模拟器重启后生效" % mi)


@AgentServer.custom_action("stop_stage_runs")
class StopStageRuns(CustomAction):
    """把刷关次数归零 —— 体力不足、又选了「什么都不做」时用。

    归零（max_hit: 0）之后 Battle_ClearStage_Count 永远失配，
    Battle_ClearStage_Cycle 会自然顺延到 Battle_ClearStage_Finish 收尾，
    整个通用通关任务就干净地结束了。

    ⚠️ 为什么不能用 pipeline_override 静态写死：那是**条件触发**的
    （只有「体力不足 + 什么都不做」这一种情况才归零），静态覆盖做不到。

    custom_action_param:
        count_node  要归零的计数节点，默认 Battle_ClearStage_Count
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            param = json.loads(argv.custom_action_param or "{}") or {}
            node = param.get("count_node", "Battle_ClearStage_Count")
            context.override_pipeline({node: {"max_hit": 0}})
            _dbg("stop_stage_runs：把 %s.max_hit 归零，本轮到此为止" % node)
        except Exception as e:  # noqa: BLE001
            _dbg("stop_stage_runs 失败: %r" % (e,))
        return True
