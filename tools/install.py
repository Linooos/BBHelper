from pathlib import Path

import shutil
import sys

try:
    import jsonc
except ModuleNotFoundError as e:
    raise ImportError(
        "Missing dependency 'json-with-comments' (imported as 'jsonc').\n"
        f"Install it with:\n  {sys.executable} -m pip install json-with-comments\n"
        "Or add it to your project's requirements."
    ) from e

from configure import configure_ocr_model
from setup_embed_python import child_exec as embedded_python_exec
from setup_embed_python import setup as setup_embedded_python


working_dir = Path(__file__).parent.parent.resolve()
install_path = working_dir / Path("install")
version = len(sys.argv) > 1 and sys.argv[1] or "v0.0.1"

# the first parameter is self name
if sys.argv.__len__() < 4:
    print("Usage: python install.py <version> <os> <arch>")
    print("Example: python install.py v1.0.0 win x86_64")
    sys.exit(1)

os_name = sys.argv[2]
arch = sys.argv[3]


def get_dotnet_platform_tag():
    """自动检测当前平台并返回对应的dotnet平台标签"""
    if os_name == "win" and arch == "x86_64":
        platform_tag = "win-x64"
    elif os_name == "win" and arch == "aarch64":
        platform_tag = "win-arm64"
    elif os_name == "macos" and arch == "x86_64":
        platform_tag = "osx-x64"
    elif os_name == "macos" and arch == "aarch64":
        platform_tag = "osx-arm64"
    elif os_name == "linux" and arch == "x86_64":
        platform_tag = "linux-x64"
    elif os_name == "linux" and arch == "aarch64":
        platform_tag = "linux-arm64"
    else:
        print("Unsupported OS or architecture.")
        print("available parameters:")
        print("version: e.g., v1.0.0")
        print("os: [win, macos, linux, android]")
        print("arch: [aarch64, x86_64]")
        sys.exit(1)

    return platform_tag


def install_deps():
    if not (working_dir / "deps" / "bin").exists():
        print('Please download the MaaFramework to "deps" first.')
        print('请先下载 MaaFramework 到 "deps"。')
        sys.exit(1)

    if os_name == "android":
        shutil.copytree(
            working_dir / "deps" / "bin",
            install_path,
            dirs_exist_ok=True,
        )
        shutil.copytree(
            working_dir / "deps" / "share" / "MaaAgentBinary",
            install_path / "MaaAgentBinary",
            dirs_exist_ok=True,
        )
    else:
        shutil.copytree(
            working_dir / "deps" / "bin",
            install_path / "runtimes" / get_dotnet_platform_tag() / "native",
            ignore=shutil.ignore_patterns(
                "*MaaDbgControlUnit*",
                "*MaaThriftControlUnit*",
                "*MaaRpc*",
                "*MaaHttp*",
                "plugins",
                "*.node",
                "*MaaPiCli*",
            ),
            dirs_exist_ok=True,
        )
        shutil.copytree(
            working_dir / "deps" / "share" / "MaaAgentBinary",
            install_path / "libs" / "MaaAgentBinary",
            dirs_exist_ok=True,
        )
        shutil.copytree(
            working_dir / "deps" / "bin" / "plugins",
            install_path / "plugins" / get_dotnet_platform_tag(),
            dirs_exist_ok=True,
        )



def install_resource():

    configure_ocr_model()

    shutil.copytree(
        working_dir / "assets" / "resource",
        install_path / "resource",
        dirs_exist_ok=True,
    )
    shutil.copy2(
        working_dir / "assets" / "interface.json",
        install_path,
    )

    with open(install_path / "interface.json", "r", encoding="utf-8") as f:
        interface = jsonc.load(f)

    interface["version"] = version

    # Agent 块：发布产物**必须**有，否则所有 CustomRecognition / CustomAction
    # 节点都会失效（实测报 Action is null）。
    #
    # child_exec 指向**包内自带**的 python（setup_embed_python 装出来的），
    # 不用 PATH 上那个 —— 理由见 tools/setup_embed_python.py 的模块注释。
    # 具体路径由目标平台决定（win 是 ./python/python.exe，mac/linux 是
    # ./python/bin/python3），所以这里**不能写死**。
    #
    # 路径用 `./`：实测 MFAAvalonia 以包根为 CWD 启动子进程，
    # `./agent/main.py` 解析得到（2026-09-20 的 Protocol version mismatch 日志
    # 就是 agent 已经跑起来的证据）。resource 那种 `{PROJECT_DIR}` 变量在
    # agent 路径上**没有**实测支持，别拿它换 `./`。
    #
    # `-u` = 不缓冲 stdout，否则 agent 的报错会卡在缓冲区里，
    # 拿不到「agent 到底为什么起不来」这条最关键的线索。
    if os_name == "android":
        # Android 发的是「资源 APK」，agent 运行时来自 APK 自带的
        # python-for-android，不走包内 python。
        # ⚠️ 这个组合本项目**没有实测过**，写出来只是为了不让它静默错位。
        print("⚠️  目标 android：不装内嵌 python，child_exec 保持裸 'python'。")
        print("    这条路没验证过 —— 出问题先怀疑这里。")
        interface["agent"] = {
            "child_exec": "python",
            "child_args": ["-u", "./agent/main.py"],
        }
    else:
        interface["agent"] = {
            "child_exec": embedded_python_exec(os_name, arch),
            "child_args": ["-u", "./agent/main.py"],
        }

    with open(install_path / "interface.json", "w", encoding="utf-8") as f:
        jsonc.dump(interface, f, ensure_ascii=False, indent=4)


def install_chores():
    shutil.copy2(
        working_dir / "README.md",
        install_path,
    )
    shutil.copy2(
        working_dir / "LICENSE",
        install_path,
    )


def install_agent():
    shutil.copytree(
        working_dir / "agent",
        install_path / "agent",
        dirs_exist_ok=True,
        # __pycache__ 是开发机上别的 python 版本编的，带过去只会误导；
        # debug/ 是 agent/my_action.py 的调试日志，属于本机运行痕迹，不该外发。
        ignore=shutil.ignore_patterns("__pycache__", "debug", "*.pyc"),
    )


def install_python():
    """把一份装了 maafw 的绿色 python 装到 install/python/。

    ⚠️ 认的是 **os_name/arch（目标平台）**，不是 sys.platform（宿主机）。
    CI 上两者不同 —— 全都跑在 ubuntu 上，却要给 win 出包。传错了会产出一个
    agent 起不来的包，详见 setup_embed_python 的模块注释。

    ⚠️ 放在 install_agent() **之后**：setup_embed_python 会读
    agent/requirements.txt，虽然读的是源目录，但先拷过去更不容易搞混。

    可重复执行 —— 版本对得上就跳过，所以第二次打包几乎是瞬时的。
    """
    if os_name == "android":
        return
    setup_embedded_python(os_name, arch)


if __name__ == "__main__":
    install_deps()
    install_resource()
    install_chores()
    install_agent()
    install_python()

    print(f"Install to {install_path} successfully.")
