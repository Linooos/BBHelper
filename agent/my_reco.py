"""BBHelper 自定义识别。

设计原则：这里**只做单节点级的识别**，不做流程编排（流程由 pipeline 状态机负责）。
"""

import json
import re
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_recognition import CustomRecognition


# 复用的 OCR 基座节点名（定义在 pipeline/common.json）。
# 它自身不带 roi / expected，只作为 context.run_recognition 的临时覆盖目标。
OCR_BASE_NODE = "Common_OcrBase"


def _ocr(
    context: Context,
    image: Any,
    roi: list[int],
    expected: list[str],
    base_node: str = OCR_BASE_NODE,
) -> list[Any]:
    """在指定 roi 内做一次 OCR，返回命中 expected 的结果列表。

    通过 pipeline_override 临时覆盖基座节点的 roi 与 expected，
    不修改资源文件本身。
    """
    detail = context.run_recognition(
        base_node,
        image,
        pipeline_override={
            base_node: {
                "recognition": {
                    "param": {
                        "roi": list(roi),
                        "expected": list(expected),
                    }
                }
            }
        },
    )
    if detail is None or not detail.hit:
        return []
    return list(detail.filtered_results or [])


@AgentServer.custom_recognition("presence_find_task_row")
class PresenceFindTaskRow(CustomRecognition):
    """在存在感任务列表里按标题找到对应行，返回**同一行**「前往」按钮的 box。

    为什么必须是 CustomRecognition，而不能用纯 pipeline：
        「前往」按钮在右列（x≈1001），任务标题在左列（x≈403），两者相隔约 600px。
        纯 pipeline 只能用 target_offset 把点击点从识别框平移过去，而平移量取决于
        识别框宽度——标题长度不同（「战力补强」4 字 vs 「勤劳的魔女」5 字）会导致
        平移后落在按钮外。这里必须把「标题行的 y」与「按钮列的 y」做跨 ROI 的行对齐。

    行对齐策略（对行高变化鲁棒）：
        1. OCR 出所有目标标题的框
        2. OCR 出所有「前往」按钮的框
        3. 对每个标题，取**其下方最近的那个**按钮，且间距不超过 max_row_gap
           —— 这样即使某行描述是两行、行高变大，也不会错配到相邻行。

    返回 box=None 表示「没找到」或「该任务已完成（没有前往按钮）」，
    调用方应据此跳过该任务。
    """

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> CustomRecognition.AnalyzeResult:
        param = json.loads(argv.custom_recognition_param or "{}") or {}

        titles = param.get("title")
        if isinstance(titles, str):
            titles = [titles]
        titles = list(titles or [])
        if not titles:
            return CustomRecognition.AnalyzeResult(
                box=None, detail={"error": "title 参数为空"}
            )

        list_roi = param.get("list_roi") or [0, 0, 1280, 720]
        button_text = param.get("button_text") or ["前往"]
        max_row_gap = int(param.get("max_row_gap", 160))
        base_node = param.get("base_node", OCR_BASE_NODE)

        title_hits = _ocr(context, argv.image, list_roi, titles, base_node)
        if not title_hits:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"error": "未找到任务标题", "title": titles},
            )

        button_hits = _ocr(context, argv.image, list_roi, button_text, base_node)
        if not button_hits:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={
                    "error": "未找到「前往」按钮（该任务可能已完成或列表已滚出）",
                    "title": titles,
                },
            )

        # 取最靠上的一个标题（正常情况下列表里同一任务只会出现一次）
        title_hit = min(title_hits, key=lambda r: r.box[1])
        t_x, t_y, t_w, t_h = title_hit.box
        title_bottom = t_y + t_h

        # 行对齐：取该标题下方最近的一个按钮
        below = [r for r in button_hits if r.box[1] >= t_y]
        if not below:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"error": "标题下方没有「前往」按钮", "title_bottom": title_bottom},
            )
        button_hit = min(below, key=lambda r: r.box[1])
        gap = button_hit.box[1] - title_bottom
        if gap > max_row_gap:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={
                    "error": f"最近按钮间隔 {gap}px 超出 max_row_gap={max_row_gap}，判定为跨行",
                    "title": str(title_hit.text),
                },
            )

        return CustomRecognition.AnalyzeResult(
            box=list(button_hit.box),
            detail={
                "matched_title": str(title_hit.text),
                "button": list(button_hit.box),
                "title_box": [t_x, t_y, t_w, t_h],
                "gap": gap,
            },
        )


@AgentServer.custom_recognition("resonance_free_left")
class ResonanceFreeLeft(CustomRecognition):
    """读共鸣屋的「剩余次数 N次」，判断免费刷新还剩几次。

    为什么必须是 CustomRecognition，而不能用纯 pipeline：
        纯 pipeline 只能表达「文本命中 / 不命中」，**没法做数字比较**。
        而「剩余次数归零 → 刷新要花零时之种」这个判断，本质就是 N 与某个下限比大小。
        OCR 出来的整串是「剩余次数2次」，得把数字抠出来才能比。

    custom_recognition_param:
        roi        要 OCR 的区域，默认盖住「剩余次数 N次」那一行
        min_left   下限（含）。默认 1 = 「至少还剩 1 次免费」
        base_node  OCR 基座节点

    返回:
        box = 命中的文本框（N >= min_left 时），否则 box=None。
        调用方把 None 当作「没有免费次数」处理。

    ⚠️ 解析失败（没找到、抠不出数字）一律返回 None，即保守地判成「没免费次数」。
       宁可少刷新一次，也不要错判成有次数而按下去 —— 那会消耗零时之种。
    """

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> CustomRecognition.AnalyzeResult:
        param = json.loads(argv.custom_recognition_param or "{}") or {}

        roi = param.get("roi") or [870, 128, 210, 36]
        min_left = int(param.get("min_left", 1))
        base_node = param.get("base_node", OCR_BASE_NODE)

        hits = _ocr(context, argv.image, roi, ["剩余次数"], base_node)
        if not hits:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"error": "未找到「剩余次数」（可能不在共鸣屋页）"},
            )

        hit = max(hits, key=lambda r: getattr(r, "score", 0.0) or 0.0)
        text = str(getattr(hit, "text", "") or "")

        m = re.search(r"(\d+)", text)
        if m is None:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"error": "「剩余次数」里没抠出数字", "text": text},
            )

        left = int(m.group(1))
        if left < min_left:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"free_left": left, "min_left": min_left, "text": text},
            )

        return CustomRecognition.AnalyzeResult(
            box=list(hit.box),
            detail={"free_left": left, "min_left": min_left, "text": text},
        )
