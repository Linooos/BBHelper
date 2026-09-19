"""BBHelper 阶段完成状态记录。

只做一件事：把「每个日常阶段跑到哪一步」落到磁盘，供事后查看。
**不做流程控制** —— 流程仍然由 pipeline 状态机负责（这是本项目的既定分工，
见 my_reco.py 开头那段）。

═══ 为什么是「两次写」而不是三态枚举 ═══
    入口节点写 started，收尾节点写 done。
跑完后文件里**是 started 却没有 done** 的阶段，就是中途失败的那个 ——
不需要在 pipeline 里做任何错误捕获。任务报错时不会有任何节点执行，
根本没有地方去写一个 failed 出来。

═══ 文件位置 ═══
    <项目根>/state/YYYY-MM-DD.json
**按日期分桶**是必须的：否则第二天会读到昨天的记录，误以为今天已经做完了。

═══ 可靠性取舍 ═══
写状态属于**旁路记录**，失败不应该打断日常 —— 所有异常都吞掉并打日志，
动作永远返回 True。宁可丢一条记录，也不要因为磁盘满/权限问题把整条日常搞崩。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

# agent/my_state.py -> agent/ -> 项目根
ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _path() -> Path:
    return STATE_DIR / f"{_today()}.json"


def load() -> dict[str, Any]:
    """读今天的记录。文件不存在或损坏时返回空壳，不抛异常。"""
    p = _path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"date": _today(), "phases": {}}


def mark(phase: str, status: str) -> dict[str, Any]:
    """记录一个阶段的状态，返回写入后的完整记录。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    d = load()
    d.setdefault("date", _today())
    d.setdefault("phases", {})[phase] = {
        "status": status,
        "at": time.strftime("%H:%M:%S"),
    }
    d["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _path().write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


class _PhaseAction(CustomAction):
    """phase_begin / phase_end 的公共实现，只有 STATUS 不同。"""

    STATUS = ""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = json.loads(argv.custom_action_param or "{}") or {}
        phase = param.get("phase")

        if not phase:
            print(f"[my_state] {self.STATUS}: 缺少 phase 参数", file=sys.stderr)
            return True  # 记录出问题不该打断日常

        try:
            mark(str(phase), self.STATUS)
            print(f"[my_state] {phase} -> {self.STATUS}")
        except Exception as e:  # noqa: BLE001
            print(f"[my_state] 写状态失败（忽略，继续跑）: {e}", file=sys.stderr)

        return True


@AgentServer.custom_action("phase_begin")
class PhaseBegin(_PhaseAction):
    STATUS = "started"


@AgentServer.custom_action("phase_end")
class PhaseEnd(_PhaseAction):
    STATUS = "done"
