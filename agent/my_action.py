import json
import math
import re
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
        run_cap = int(param.get("run_cap", 20))
        count_node = param.get("count_node", "Battle_ClearStage_Count")
        base_node = param.get("base_node", OCR_BASE_NODE)

        per_run = int(param.get("stamina_per_run") or 0)

        if per_run <= 0:
            roi = param.get("roi") or [900, 630, 380, 60]
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
