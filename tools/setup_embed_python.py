"""把一份绿色 Python 打进发布产物 —— 终端用户从此不用自己装 Python。

    python tools/setup_embed_python.py            # 装到 install/python/
    python tools/setup_embed_python.py --force    # 推倒重来

`tools/install.py` 会自动调用它，一般不用手动跑。

═══ 为什么要有这个 ═══
发布产物的 interface.json 里 `agent.child_exec` 原先写的是裸的 "python"，
意思是「去 PATH 上找一个」。这条假设**在真实用户机器上不成立**：

  - 2026-09-20 实测，本机 PATH 第一位是 uv 装的 python shim，
    它**没有 maafw** ⟹ agent 启动即 ModuleNotFoundError，任务链上所有
    Custom* 节点全废。
  - 就算 PATH 上那个 python 有 maafw，**版本还未必对**（见下）。

所以把一份确定版本、确定装了依赖的 python 直接放进包里，
`child_exec` 指向它 —— 「用哪个 python」这个变量就彻底消失了。

═══ 版本是硬耦合的，别乱改 ═══
`agent/requirements.txt` 里钉的 maafw 版本**必须与调用方的 MaaFramework 一致**，
否则 AgentServer 会拒连且不弹任何提示（细节见 docs/zh_cn/develop/dev_loop.md §4.5）。

  发布产物（MFAAvalonia）自带的框架是 5.13.1 ⟹ 包里这份 python 装 5.13.1
  开发 venv（VS Code 插件的框架是 5.12.2）      ⟹ venv 装 5.12.2

两边**故意不同**、各有各的 requirements 文件，不要合并。

═══ 为什么用官方 embeddable zip ═══
体积最小（约 11MB），官方源，够用：
maafw 的 wheel 是 `py3-none-win_amd64`（纯 ABI 无关），装进 embeddable 没问题。
唯一的坑是它默认**不加载 site-packages**，得手动改 `python3XX._pth`（见 _patch_pth）。

参考实现：MAA1999 / MaaStellaSora 的 `tools/ci/setup_embed_python.py`，
形态一模一样，是 Maa 生态里跑了很久的做法。
"""

from pathlib import Path

import argparse
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile


sys.stdout.reconfigure(encoding="utf-8")

working_dir = Path(__file__).parent.parent.resolve()
install_path = working_dir / "install"
dest_dir = install_path / "python"
requirements = working_dir / "agent" / "requirements.txt"

# ⚠️ 别随手升。3.12.10 是参考实现用的版本，numpy / maafw 在这上面是验证过的；
# 换版本要连带确认 numpy 有对应的 cp3XX wheel。
PYTHON_VERSION = "3.12.10"


def _pth_name() -> str:
    """3.12.10 -> python312._pth"""
    return "python%s._pth" % "".join(PYTHON_VERSION.split(".")[:2])


def _maafw_pin() -> str:
    """从 agent/requirements.txt 里抠出 maafw 的钉版本，如 '5.13.1'。"""
    text = requirements.read_text(encoding="utf-8")
    m = re.search(r"^\s*maafw\s*==\s*([0-9][^\s#]*)", text, re.MULTILINE)
    if m is None:
        raise SystemExit(
            "agent/requirements.txt 里没有 `maafw==<版本>` 这一行。\n"
            "这份 python 必须装确定的版本，不能放开。"
        )
    return m.group(1)


def _run(cmd, **kw) -> subprocess.CompletedProcess:
    print(">>> %s" % " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], **kw)


def _installed_maafw() -> str | None:
    """包里这份 python 现在装着哪个 maafw？没装 / 跑不起来都返回 None。"""
    exe = dest_dir / "python.exe"
    if not exe.exists():
        return None
    try:
        out = subprocess.run(
            [str(exe), "-c", "import importlib.metadata as m; print(m.version('maafw'))"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _download(url: str, dst: Path) -> None:
    print("下载 %s" % url)
    dst.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "BBHelper-setup"})
    with urllib.request.urlopen(req) as resp, dst.open("wb") as f:
        shutil.copyfileobj(resp, f)
    print("  -> %s (%.1f MB)" % (dst.name, dst.stat().st_size / 1e6))


def _patch_pth() -> None:
    """放开 site-packages —— embeddable 版默认把自己锁在 zip 里。

    默认的 `._pth` 只有一行 `#import site`（注释掉的）加一个 `python312.zip`，
    意思是「只认标准库，不认第三方包」。要做三件事：

      1. 取消注释 `import site` —— 否则 site-packages 里的 `.pth` 文件不生效
         （numpy / maaagentbinary 依赖这个）
      2. 补 `Lib\\site-packages` —— pip 装的东西落在这里
      3. 补 `.` 和 `DLLs` —— 让 python.exe 找得到自己那堆 dll

    不做这一步，`pip install` 看起来成功，`import maa` 照样 ModuleNotFoundError。
    """
    pth = dest_dir / _pth_name()
    if not pth.exists():
        raise SystemExit("没找到 %s —— embeddable 包的结构可能变了。" % pth.name)

    lines = pth.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        if line.strip().lstrip("#").strip() == "import site":
            out.append("import site")  # 取消注释
        else:
            out.append(line.strip())
    for need in (".", "Lib", r"Lib\site-packages", "DLLs"):
        if need not in out:
            out.append(need)
    pth.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("已改 %s:" % pth.name)
    for line in out:
        print("    %s" % line)


def _bootstrap_pip() -> None:
    """embeddable 包不带 pip，用官方 get-pip.py 装一个。"""
    python = dest_dir / "python.exe"
    check = _run([python, "-m", "pip", "--version"], capture_output=True, text=True)
    if check.returncode == 0:
        print("pip 已就位: %s" % check.stdout.strip())
        return

    get_pip = dest_dir / "get-pip.py"
    try:
        _download("https://bootstrap.pypa.io/get-pip.py", get_pip)
        _run([python, get_pip], check=True)
    finally:
        get_pip.unlink(missing_ok=True)


def _install_requirements() -> None:
    python = dest_dir / "python.exe"
    # 国内网络慢的话设 PIP_INDEX_URL 环境变量即可（pip 自己会读）：
    #   set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
    _run(
        [python, "-m", "pip", "install", "--no-warn-script-location",
         "-r", requirements],
        check=True,
    )


def setup(force: bool = False) -> None:
    """真正的入口。install.py 直接调这个，不走 argparse。"""
    if not sys.platform.startswith("win"):
        # 非 Windows 平台要改用 python-build-standalone 的 install_only 包
        # （embeddable zip 只有 Windows 有）。本项目只发 Windows，不实现。
        print("本项目只发 Windows 产物，%s 上不装内嵌 python。" % sys.platform)
        return

    pin = _maafw_pin()

    if not force:
        have = _installed_maafw()
        if have == pin:
            print("install/python 已就绪（maafw %s），跳过。" % have)
            return
        if have is not None:
            print("install/python 里的 maafw 是 %s，要求 %s —— 重装。" % (have, pin))
            shutil.rmtree(dest_dir)
        elif (dest_dir / "python.exe").exists():
            print("install/python 存在但 maafw 跑不起来 —— 推倒重来。")
            shutil.rmtree(dest_dir)

    dest_dir.mkdir(parents=True, exist_ok=True)

    zip_path = dest_dir / ("python-%s-embed-amd64.zip" % PYTHON_VERSION)
    try:
        _download(
            "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip"
            % (PYTHON_VERSION, PYTHON_VERSION),
            zip_path,
        )
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(dest_dir)
    finally:
        zip_path.unlink(missing_ok=True)

    _patch_pth()
    _bootstrap_pip()
    _install_requirements()

    have = _installed_maafw()
    if have != pin:
        raise SystemExit("装完了但 maafw 版本读到的是 %r，期望 %r —— 不对劲。" % (have, pin))

    print()
    print("内嵌 Python 就绪：%s" % (dest_dir / "python.exe"))
    print("  Python %s / maafw %s" % (PYTHON_VERSION, have))
    print("  tools/install.py 会把 interface.json 的 child_exec 指向它。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="删掉重装，不做版本检查")
    args = parser.parse_args()
    setup(force=args.force)


if __name__ == "__main__":
    main()
