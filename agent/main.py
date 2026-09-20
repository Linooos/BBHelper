import sys
from pathlib import Path

# ⚠️ 这一行**不能删**。
#
# 发布产物用的是 embeddable python，它带一个 `._pth` 文件。按 CPython 的设计，
# `._pth` 一旦存在就**接管 sys.path 的初始化**（不再读环境变量 / 注册表 / site），
# 顺带把「脚本所在目录自动进 sys.path[0]」这条也一起关掉了。
#
# 后果（2026-09-20 实测）：下面几个同目录的 import 全部
# ModuleNotFoundError: No module named 'my_action'。
#
# 自己插一下，从此不依赖「谁在什么 CWD 下怎么启动的」。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maa.agent.agent_server import AgentServer  # noqa: E402
from maa.toolkit import Toolkit  # noqa: E402

import my_action  # noqa: E402
import my_reco  # noqa: E402
import my_state  # noqa: E402,F401  只为触发 @AgentServer.custom_action 注册


def main():
    Toolkit.init_option("./")

    if len(sys.argv) < 2:
        print("Usage: python main.py <socket_id>")
        print("socket_id is provided by AgentIdentifier.")
        sys.exit(1)

    socket_id = sys.argv[-1]

    AgentServer.start_up(socket_id)
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
