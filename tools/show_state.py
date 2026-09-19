"""查看某天的日常阶段完成情况。

用法
----
    .venv/Scripts/python.exe tools/show_state.py              # 今天
    .venv/Scripts/python.exe tools/show_state.py 2026-09-19   # 指定日期

判读
----
    done      跑完了（**游戏里真的做成了**）
    skipped   流程走到了，但**这一步没产生实际效果** —— 最典型的是「世界那么大」：
              要打虚轴之庭，但「快捷战斗」按钮是灰的（未解锁 20 亿伤害），只能跳过。
              它**不等于完成了**，别被 done 和它搞混。
    started   进去了但没出来 —— **中途失败/卡死的阶段就是它**
    （空白）   没跑到，或者界面开关关着被跳过了

为什么用 started/done 两次写而不是三态枚举，见 agent/my_state.py 开头。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"

# 日常的执行顺序。写死在这里而不是排序 —— 顺序本身就是信息。
PHASES = [
    "启动",
    "邮件",
    "使魔探险",
    "使魔的爱",
    "世界那么大",
    "战力补强",
    "体力消耗",
    "共鸣之力",
    "收尾领奖",
    "每周任务",
]


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y-%m-%d")
    path = STATE_DIR / f"{date}.json"

    if not path.exists():
        print(f"没有 {date} 的记录：{path}")
        print("（状态文件由脚本运行时写出；没跑过就不会有。）")
        return 1

    data = json.loads(path.read_text(encoding="utf-8"))
    phases = data.get("phases", {})

    print(f"日常阶段完成情况  {date}   更新于 {data.get('updated', '?')}")
    print("-" * 52)

    for name in PHASES:
        rec = phases.get(name)
        if not rec:
            print(f"  {name:<8} {'—':<10} （未跑到 / 已跳过）")
            continue
        status = rec.get("status", "?")
        flag = ""
        if status == "started":
            flag = "  ← 未完成（中途失败）"
        elif status == "skipped":
            flag = "  ← 跳过，游戏里没做成"
        print(f"  {name:<8} {status:<10} {rec.get('at', '')}{flag}")

    # 记录里出现但不在 PHASES 里的（比如以后加了新阶段忘了登记）
    extra = [k for k in phases if k not in PHASES]
    if extra:
        print()
        print("  ⚠️ 记录里有未登记的阶段（tools/show_state.py 的 PHASES 需要补上）：")
        for k in extra:
            print(f"     {k}: {phases[k]}")

    unfinished = [n for n in PHASES if phases.get(n, {}).get("status") == "started"]
    print("-" * 52)
    if unfinished:
        print("未完成：" + "、".join(unfinished))
        return 2

    done = [n for n in PHASES if phases.get(n, {}).get("status") == "done"]
    skipped = [n for n in PHASES if phases.get(n, {}).get("status") == "skipped"]
    print(f"跑完 {len(done)} 个阶段", end="")
    if skipped:
        print(f"，其中 {len(skipped)} 个是**跳过的、游戏里没做成**：" + "、".join(skipped))
    else:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
