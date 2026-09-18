"""开发期调试脚本：直接驱动 MaaFramework（含 Agent 子进程），绕开 MaaMCP。

为什么需要它
------------
MaaMCP 的 ``run_pipeline(start_agent=True)`` 在本项目的开发布局下无法建立
Agent 会话（MaaFramework 日志报 ``session.recognition=false``，随后
``recognition is null``），导致所有 CustomRecognition / CustomAction 节点失效。
本脚本等价于 MFAAvalonia / MaaPiCli 的启动行为：自己拉起 ``agent/main.py``
子进程，通过 socket 建立 Agent 会话。

它与 MaaMCP 的分工：
    * 纯 OCR / TemplateMatch / Click 流程 —— 用 MaaMCP 更快
    * 涉及 Custom* 节点的流程 —— 用本脚本

用法
----
    .venv/Scripts/python.exe tools/dev_run.py <入口节点名>
    .venv/Scripts/python.exe tools/dev_run.py <入口> --device MuMu --list-only

``--list-only`` 只连接并列出已注册的自定义识别/动作，不执行任务，用于快速自检。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

# Windows 控制台默认 GBK，中文日志会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
RESOURCE = ROOT / "assets" / "resource"
AGENT_DIR = ROOT / "agent"
AGENT_MAIN = AGENT_DIR / "main.py"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

# 真机型号。当 adb 列表里同时有真机和模拟器时，明确排除真机，避免误操作。
REAL_DEVICE_MARKERS = ("PJZ110", "f34e7c1e")


def log(msg: str) -> None:
    print(f"[dev_run] {msg}", flush=True)


def pick_device(name_hint: str):
    from maa.toolkit import Toolkit

    devs = Toolkit.find_adb_devices()
    if not devs:
        raise SystemExit("没有发现任何 ADB 设备，请确认 MuMu 模拟器已启动")

    log(f"发现 {len(devs)} 个 ADB 设备：")
    for d in devs:
        log(f"    name={getattr(d, 'name', '?')!r} address={getattr(d, 'address', '?')!r}")

    # 明确排除真机
    safe = [
        d
        for d in devs
        if not any(m in str(getattr(d, "name", "")) for m in REAL_DEVICE_MARKERS)
    ]
    pool = safe or devs

    if name_hint:
        pool = [d for d in pool if name_hint.lower() in str(getattr(d, "name", "")).lower()]
        if not pool:
            raise SystemExit(f"没有匹配 {name_hint!r} 的设备")

    dev = pool[0]
    if len(pool) > 1:
        log(f"⚠️ 有多个候选设备，取第一个：{getattr(dev, 'name', '?')}")
    return dev


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("entry", nargs="?", help="pipeline 入口节点名")
    ap.add_argument("--device", default="MuMu", help="设备名匹配子串（默认 MuMu）")
    ap.add_argument("--list-only", action="store_true", help="只列出已注册的 Custom* 后退出")
    args = ap.parse_args()

    if not args.entry and not args.list_only:
        ap.error("需要指定入口节点名，或加 --list-only")

    if not PYTHON.exists():
        raise SystemExit(f"找不到 venv 里的 Python：{PYTHON}\n先执行：uv venv && uv pip install MaaFw")
    if not RESOURCE.is_dir():
        raise SystemExit(f"找不到资源目录：{RESOURCE}")

    from maa.agent_client import AgentClient
    from maa.controller import AdbController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit

    Toolkit.init_option(str(ROOT))

    dev = pick_device(args.device)
    log(f"连接设备：{getattr(dev, 'name', '?')} @ {getattr(dev, 'address', '?')}")
    ctrl = AdbController(str(dev.adb_path), str(dev.address))
    if not ctrl.post_connection().wait().succeeded:
        raise SystemExit("设备连接失败")
    log("设备已连接")

    res = Resource()
    if not res.post_bundle(str(RESOURCE)).wait().succeeded:
        raise SystemExit("资源加载失败")
    log("资源已加载")

    tasker = Tasker()
    tasker.bind(res, ctrl)
    if not tasker.inited:
        raise SystemExit("Tasker 初始化失败")

    # ── 拉起 Agent 子进程并建立会话 ─────────────────────────────
    # cwd 必须是 agent/ 目录：main.py 里用的是顶层模块名 `import my_action`，
    # 依赖脚本所在目录在 sys.path 上。
    agent = AgentClient()
    identifier = agent.identifier  # 注意：是 property，不是方法
    log(f"Agent identifier = {identifier}")

    proc = subprocess.Popen(
        [str(PYTHON), str(AGENT_MAIN), identifier],
        cwd=str(AGENT_DIR),
    )

    agent.bind(res)
    if not agent.register_sink(res, ctrl, tasker):
        log("⚠️ register_sink 返回 False")

    connected = False
    for _ in range(50):  # 最多等 10 秒
        if agent.connect() and agent.connected:  # connected 是 property
            connected = True
            break
        time.sleep(0.2)

    if not connected:
        proc.terminate()
        raise SystemExit("Agent 连接失败（子进程可能启动即退出，检查上面的 Python 报错）")
    log("Agent 会话已建立")

    recos = agent.custom_recognition_list  # property
    actions = agent.custom_action_list  # property
    log(f"已注册 CustomRecognition ({len(recos)}): {recos}")
    log(f"已注册 CustomAction    ({len(actions)}): {actions}")

    rc = 0
    if not args.list_only:
        log(f"执行入口：{args.entry}")
        detail = tasker.post_task(args.entry).wait().get()
        # Status.succeeded / .failed / .done 都是 property
        st = getattr(detail, "status", None)
        ok = bool(getattr(st, "succeeded", False))
        log(f"status = {'succeeded' if ok else 'failed'}")
        for node in getattr(detail, "nodes", []) or []:
            reco = getattr(node, "recognition", None)
            results = list(getattr(reco, "all_results", None) or []) if reco else []
            if not results:
                log(f"    {node.name}: (无识别结果)")
                continue
            for r in results:
                box = list(getattr(r, "box", []) or [])
                score = getattr(r, "score", None)
                text = getattr(r, "text", None)
                extra = f" score={score:.4f}" if isinstance(score, float) else ""
                extra += f" text={text!r}" if isinstance(text, str) else ""
                log(f"    {node.name}: box={box}{extra}")
        if not ok:
            rc = 1

    try:
        agent.disconnect()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return rc


if __name__ == "__main__":
    sys.exit(main())
