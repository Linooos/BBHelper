"""把一份绿色 Python 打进发布产物 —— 终端用户从此不用自己装 Python。

    python tools/setup_embed_python.py win x86_64       # 指定目标平台
    python tools/setup_embed_python.py win x86_64 --force

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

═══ ⚠️ 判定看的是**目标平台**，不是宿主机 ═══
这两个东西经常不是一回事：

  - CI（.github/workflows/install.yml）全部跑在 `ubuntu-latest` 上，
    但矩阵里有 `win` 这一格 ⟹ 宿主机是 Linux、目标是 Windows。
  - 本地 Windows 上也能 `python install.py v1.0 linux x86_64`。

早先的版本写的是 `sys.platform`，于是 CI 上「宿主机是 linux ⟹ 跳过装 python」，
而 `install.py` 照样把 child_exec 写成 `./python/python.exe` ——
**产出一个看起来正常、但 agent 永远起不来的包**。现在按目标平台来。

═══ 两条安装路径 ═══
宿主机和目标平台一致时（本地 Windows 打包、或原生 runner）**直接跑包里那个
解释器**装依赖 —— 装完能立刻验一遍，是最可靠的那条路。

不一致时（CI 上的 ubuntu 给 win/macos/linux 打包）走**交叉安装**：
用宿主机的 pip，`--platform/--python-version/--only-binary=:all: --target`
把目标平台的轮子直接铺进 site-packages。2026-09-20 实测可行，
落地的确实是目标平台的东西（`libMaaLinuxControlUnit.so`、
`cpython-312-x86_64-linux-gnu.so`）。

⚠️ pip 的 `--platform` **不展开平台兼容性**，只做精确匹配，所以每个目标平台的
tag 要在 _PIP_TAGS 里列全（numpy 和 maafw 挑的 tag 就不是同一个）。

═══ 取 python 的方式按目标平台分两种 ═══
  win       官方 embeddable zip（约 11MB）。默认**不加载 site-packages**，
            要改 `python3XX._pth`（见 _patch_pth），且不带 pip。
  macos/linux  python-build-standalone 的 `install_only` 包（自带 pip、结构正常）。

═══ 版本是硬耦合的，别乱改 ═══
`agent/requirements.txt` 里钉的 maafw 版本**必须与调用方的 MaaFramework 一致**，
否则 AgentServer 会拒连且不弹任何提示（细节见 docs/zh_cn/develop/dev_loop.md §4.5）。

  发布产物（MFAAvalonia）自带的框架是 5.13.1 ⟹ 包里这份 python 装 5.13.1
  开发 venv（VS Code 插件的框架是 5.12.2）      ⟹ venv 装 5.12.2

两边**故意不同**、各有各的 requirements 文件，不要合并。

参考实现：MAA1999 / MaaStellaSora 的 `tools/ci/setup_embed_python.py`
（它们用原生 runner，所以没有交叉那条路；我们照搬了取包的部分）。
"""

from pathlib import Path

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile


# 中文报错要走 stderr，两个都设 —— 只设 stdout 的话出错信息是乱码，
# 而恰恰是出错的时候最需要看清它。
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

working_dir = Path(__file__).parent.parent.resolve()
install_path = working_dir / "install"
dest_dir = install_path / "python"
requirements = working_dir / "agent" / "requirements.txt"

# ⚠️ 别随手升。3.12.10 是参考实现用的版本，numpy / maafw 在这上面是验证过的；
# 换版本要连带确认 numpy 有对应的 cp3XX wheel。
PYTHON_VERSION = "3.12.10"
# python-build-standalone 的 release tag，与 PYTHON_VERSION 要兼容
PBS_RELEASE_TAG = "20250409"

_EMBED_BASE = "https://www.python.org/ftp/python/{v}/python-{v}-embed-{suffix}.zip"
_PBS_BASE = (
    "https://github.com/indygreg/python-build-standalone/releases/download/"
    "{tag}/cpython-{v}+{tag}-{arch}-{os}-install_only.tar.gz"
)

# pip 的 --platform 是**精确匹配**、不展开兼容性，所以每个平台要把
# numpy / maafw 实际用的 tag 都列上，少一个就会 backtrack 到旧版本或直接失败。
_PIP_TAGS = {
    ("win", "x86_64"): ["win_amd64"],
    ("win", "aarch64"): ["win_arm64"],
    ("macos", "x86_64"): [
        "macosx_10_9_x86_64", "macosx_10_13_x86_64", "macosx_11_0_x86_64",
        "macosx_12_0_x86_64", "macosx_13_0_x86_64", "macosx_14_0_x86_64",
        "macosx_15_0_x86_64",
    ],
    ("macos", "aarch64"): [
        "macosx_11_0_arm64", "macosx_12_0_arm64", "macosx_13_0_arm64",
        "macosx_14_0_arm64", "macosx_15_0_arm64",
    ],
    ("linux", "x86_64"): [
        "manylinux_2_17_x86_64", "manylinux_2_24_x86_64", "manylinux_2_27_x86_64",
        "manylinux_2_28_x86_64", "manylinux_2_34_x86_64", "manylinux2014_x86_64",
        "linux_x86_64",
    ],
    ("linux", "aarch64"): [
        "manylinux_2_17_aarch64", "manylinux_2_24_aarch64", "manylinux_2_27_aarch64",
        "manylinux_2_28_aarch64", "manylinux_2_34_aarch64", "manylinux2014_aarch64",
        "linux_aarch64",
    ],
}

# 目标平台 -> 怎么取 python、怎么装包、child_exec 写什么
_SPECS = {
    ("win", "x86_64"): {
        "kind": "embed",
        "url": _EMBED_BASE.format(v=PYTHON_VERSION, suffix="amd64"),
        "exe": ("python.exe",),
        "site_packages": ("Lib", "site-packages"),
        "child_exec": "./python/python.exe",
    },
    ("win", "aarch64"): {
        "kind": "embed",
        "url": _EMBED_BASE.format(v=PYTHON_VERSION, suffix="arm64"),
        "exe": ("python.exe",),
        "site_packages": ("Lib", "site-packages"),
        "child_exec": "./python/python.exe",
    },
    ("macos", "x86_64"): {
        "kind": "pbs",
        "url": _PBS_BASE.format(
            tag=PBS_RELEASE_TAG, v=PYTHON_VERSION,
            arch="x86_64", os="apple-darwin",
        ),
        "exe": ("bin", "python3"),
        "site_packages": ("lib", "python3.12", "site-packages"),
        "child_exec": "./python/bin/python3",
    },
    ("macos", "aarch64"): {
        "kind": "pbs",
        "url": _PBS_BASE.format(
            tag=PBS_RELEASE_TAG, v=PYTHON_VERSION,
            arch="aarch64", os="apple-darwin",
        ),
        "exe": ("bin", "python3"),
        "site_packages": ("lib", "python3.12", "site-packages"),
        "child_exec": "./python/bin/python3",
    },
    ("linux", "x86_64"): {
        "kind": "pbs",
        "url": _PBS_BASE.format(
            tag=PBS_RELEASE_TAG, v=PYTHON_VERSION,
            arch="x86_64", os="unknown-linux-gnu",
        ),
        "exe": ("bin", "python3"),
        "site_packages": ("lib", "python3.12", "site-packages"),
        "child_exec": "./python/bin/python3",
    },
    ("linux", "aarch64"): {
        "kind": "pbs",
        "url": _PBS_BASE.format(
            tag=PBS_RELEASE_TAG, v=PYTHON_VERSION,
            arch="aarch64", os="unknown-linux-gnu",
        ),
        "exe": ("bin", "python3"),
        "site_packages": ("lib", "python3.12", "site-packages"),
        "child_exec": "./python/bin/python3",
    },
}

# 装完之后写进去的标记，用来判断这个目录还要不要重装（交叉安装时跑不起来，
# 没法像原生那条路一样直接问解释器「你装了啥」）
_MARKER = dest_dir / ".bbhelper-python"


def child_exec(os_name: str, arch: str) -> str:
    """给 install.py 用：这个目标平台的 child_exec 该写什么。"""
    return _SPECS[(os_name, arch)]["child_exec"]


def _exe() -> Path:
    """包里那个解释器的路径（按当前 _SPECS 解析）。"""
    return dest_dir.joinpath(*_SPECS_ACTIVE["exe"])


_SPECS_ACTIVE: dict = {}


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


def _marker_text(os_name: str, arch: str) -> str:
    return "%s %s py%s maafw==%s" % (os_name, arch, PYTHON_VERSION, _maafw_pin())


def _is_native(os_name: str, arch: str) -> bool:
    """宿主机跑得动目标平台的解释器吗？跑得动就走原生那条路（能验证）。"""
    host_os = {"win32": "win", "darwin": "macos", "linux": "linux"}.get(sys.platform)
    if host_os != os_name:
        return False

    try:
        machine = os.uname().machine  # POSIX
    except AttributeError:
        machine = os.environ.get("PROCESSOR_ARCHITECTURE", "")  # Windows

    # Windows 上 PROCESSOR_ARCHITECTURE 是 AMD64 / ARM64；
    # 万一读不到，按 x86_64 处理（绝大多数情况）。
    host_arch = {
        "AMD64": "x86_64", "amd64": "x86_64", "x86_64": "x86_64",
        "ARM64": "aarch64", "arm64": "aarch64", "aarch64": "aarch64",
    }.get(machine, "x86_64")
    return host_arch == arch


def _run(cmd, **kw) -> subprocess.CompletedProcess:
    print(">>> %s" % " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], **kw)


def _download(url: str, dst: Path, attempts: int = 3) -> None:
    """带重试的下载。

    ⚠️ macos/linux 用的 python-build-standalone 放在 **GitHub Releases** 上，
    国内网络经常下一半就 RemoteDisconnected（2026-09-20 实测踩过）。
    重试能救掉大部分抖动；CI 跑在 GitHub 自己的 runner 上，本来就不会遇到。

    国内慢的话设 PIP_INDEX_URL 只管 pip，管不到这里 —— 只能挂代理。
    """
    print("下载 %s" % url)
    dst.parent.mkdir(parents=True, exist_ok=True)
    for i in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BBHelper-setup"})
            with urllib.request.urlopen(req) as resp, dst.open("wb") as f:
                shutil.copyfileobj(resp, f)
            print("  -> %s (%.1f MB)" % (dst.name, dst.stat().st_size / 1e6))
            return
        except Exception as e:
            dst.unlink(missing_ok=True)
            if i == attempts:
                raise SystemExit(
                    "下载失败（试了 %d 次）：%s\n  %r\n"
                    "  win 目标走 python.org，一般没事；\n"
                    "  macos/linux 走 GitHub Releases，国内网络容易断，挂代理重试。"
                    % (attempts, url, e)
                )
            wait = 5 * i
            print("  出错（%r），%d 秒后重试 %d/%d" % (e, wait, i + 1, attempts))
            time.sleep(wait)


def _extract_zip(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest_dir)


def _extract_pbs_tar(tar_path: Path) -> None:
    """python-build-standalone 的包解压后顶层是个 `python/`，要把内容提上来。"""
    tmp = dest_dir / "_tmp_extract"
    tmp.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:*") as tar:
        # 挡一下路径穿越（下载来的归档，不该无条件信任）
        root = os.path.realpath(tmp)
        for m in tar.getmembers():
            if os.path.isabs(m.name) or ".." in Path(m.name).parts:
                raise SystemExit("压缩包里有不安全的路径: %s" % m.name)
            if os.path.commonpath([root, os.path.realpath(tmp / m.name)]) != root:
                raise SystemExit("压缩包条目越界: %s" % m.name)
        tar.extractall(tmp)

    inner = tmp / "python"
    if not inner.is_dir():
        raise SystemExit("解压后没找到 python/ 子目录，包结构可能变了")
    for item in inner.iterdir():
        shutil.move(str(item), str(dest_dir / item.name))
    shutil.rmtree(tmp)


def _fix_exec_bits() -> None:
    """pbs 的 bin/* 在 POSIX 上要有可执行位。Windows 主机解压时会丢，补一下。"""
    if os.name == "nt":
        return
    for p in (dest_dir / "bin").glob("*") if (dest_dir / "bin").is_dir() else []:
        if p.is_file():
            p.chmod(p.stat().st_mode | 0o755)


def _patch_pth() -> None:
    """放开 site-packages —— embeddable 版默认把自己锁在 zip 里。

    默认的 `._pth` 只有一行 `#import site`（注释掉的）加一个 `python312.zip`，
    意思是「只认标准库，不认第三方包」。要做三件事：

      1. 取消注释 `import site` —— 否则 site-packages 里的 `.pth` 文件不生效
      2. 补 `Lib\\site-packages` —— pip 装的东西落在这里
      3. 补 `.` 和 `DLLs` —— 让 python.exe 找得到自己那堆 dll

    不做这一步，`pip install` 看起来成功，`import maa` 照样 ModuleNotFoundError。
    只有 Windows 的 embeddable 包有 `._pth`，pbs 不需要。
    """
    pth = dest_dir / _pth_name()
    if not pth.exists():
        raise SystemExit("没找到 %s —— embeddable 包的结构可能变了。" % pth.name)

    out = []
    for line in pth.read_text(encoding="utf-8").splitlines():
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
    """embeddable 包不带 pip，用官方 get-pip.py 装一个。pbs 自带，跳过。"""
    exe = _exe()
    check = _run([exe, "-m", "pip", "--version"], capture_output=True, text=True)
    if check.returncode == 0:
        print("pip 已就位: %s" % check.stdout.strip())
        return

    get_pip = dest_dir / "get-pip.py"
    try:
        _download("https://bootstrap.pypa.io/get-pip.py", get_pip)
        _run([exe, get_pip], check=True)
    finally:
        get_pip.unlink(missing_ok=True)


def _install_requirements(os_name: str, arch: str, native: bool) -> None:
    if native:
        # 国内网络慢的话设 PIP_INDEX_URL 环境变量即可（pip 自己会读）：
        #   set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
        _run([_exe(), "-m", "pip", "install", "--no-warn-script-location",
              "-r", requirements], check=True)
        return

    # 交叉安装：用宿主机的 pip 把**目标平台**的轮子铺进去。
    # --only-binary=:all: 是必需的 —— 没有它 pip 会去下 sdist 然后试图编译。
    sp = dest_dir.joinpath(*_SPECS_ACTIVE["site_packages"])
    sp.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--no-warn-script-location",
           "--only-binary=:all:", "--python-version", ".".join(PYTHON_VERSION.split(".")[:2]),
           "--target", sp, "-r", requirements, "pip"]
    for tag in _PIP_TAGS[(os_name, arch)]:
        cmd += ["--platform", tag]
    _run(cmd, check=True)


def setup(os_name: str, arch: str, force: bool = False) -> None:
    """真正的入口。install.py 直接调这个，不走 argparse。"""
    if (os_name, arch) not in _SPECS:
        raise SystemExit(
            "不支持的发布目标: %s/%s\n可选: %s"
            % (os_name, arch, ", ".join("%s/%s" % k for k in sorted(_SPECS)))
        )

    global _SPECS_ACTIVE
    _SPECS_ACTIVE = _SPECS[(os_name, arch)]
    want = _marker_text(os_name, arch)
    native = _is_native(os_name, arch)

    print("目标平台 %s/%s，%s安装"
          % (os_name, arch, "原生" if native else "交叉"))

    if not force and _MARKER.exists() and _MARKER.read_text(encoding="utf-8").strip() == want:
        print("install/python 已就绪（%s），跳过。" % want)
        return

    if dest_dir.exists():
        print("清掉旧的 install/python …")
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    archive = dest_dir / ("download" + Path(_SPECS_ACTIVE["url"]).suffix)
    try:
        _download(_SPECS_ACTIVE["url"], archive)
        if _SPECS_ACTIVE["kind"] == "embed":
            _extract_zip(archive)
        else:
            _extract_pbs_tar(archive)
    finally:
        archive.unlink(missing_ok=True)

    if _SPECS_ACTIVE["kind"] == "embed":
        _patch_pth()
    _fix_exec_bits()

    # 原生：跑解释器把 pip 备好（pbs 自带，embeddable 要现装）。
    # 交叉：跑不动解释器，pip 由下面那条命令连依赖一起铺进去。
    if native:
        _bootstrap_pip()
    _install_requirements(os_name, arch, native)

    _MARKER.write_text(want + "\n", encoding="utf-8")

    if native:
        have = _run([_exe(), "-c",
                     "import importlib.metadata as m; print(m.version('maafw'))"],
                    capture_output=True, text=True).stdout.strip()
        print("复核：包里这个解释器读到 maafw %s" % have)

    print()
    print("内嵌 Python 就绪：%s" % _exe())
    print("  目标 %s/%s，Python %s，maafw==%s"
          % (os_name, arch, PYTHON_VERSION, _maafw_pin()))
    print("  install.py 会把 interface.json 的 child_exec 写成 %s"
          % _SPECS_ACTIVE["child_exec"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="给发布产物装一份绿色 python",
        epilog="目标平台取值：%s" % ", ".join("%s %s" % k for k in sorted(_SPECS)),
    )
    parser.add_argument("os", choices=sorted({k[0] for k in _SPECS}))
    parser.add_argument("arch", choices=["x86_64", "aarch64"])
    parser.add_argument("--force", action="store_true", help="删掉重装，不做缓存检查")
    args = parser.parse_args()
    setup(args.os, args.arch, force=args.force)


if __name__ == "__main__":
    main()
