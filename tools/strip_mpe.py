"""把 MaaPipelineEditor 写进 pipeline JSON 的元数据删掉。

MPE 会在两处写东西：
  1. 每个节点里的 `$__mpe_code`（{"position": {...}}）—— 记节点在画布上的坐标
  2. 文件**顶层**的 `$__mpe_config_<文件名>` / `$__mpe_external_<节点名>_<文件名>`
     —— 记画布配置和跨文件连线的布局

都只给编辑器自己用，MaaFramework 不认也不需要。留着会让 maa-tools check
刷一堆 "检测到 MPE 配置" 的 warning、退出码变成 1，把真正的 error 淹掉。

⚠️ 以后再用插件的图形界面拖节点，这些会写回来。届时重跑本脚本即可。

用法：.venv/Scripts/python.exe tools/strip_mpe.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIPE = ROOT / "assets" / "resource" / "pipeline"

PREFIX = "$__mpe"


def strip(obj, path, removed):
    """递归删掉所有 $__mpe* 的 key。别的 $-key 只报不动。"""
    if not isinstance(obj, dict):
        return
    for k in list(obj.keys()):
        if k.startswith(PREFIX):
            del obj[k]
            removed.append("%s.%s" % (path, k))
        elif k.startswith("$"):
            print("  ⚠️ 未知的 $-key，没动它：%s.%s" % (path, k), file=sys.stderr)
    for k, v in obj.items():
        if isinstance(v, dict):
            strip(v, "%s.%s" % (path, k), removed)


total = 0
for f in sorted(PIPE.glob("*.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    removed = []
    strip(d, f.name, removed)  # 从顶层开始 —— 顶层那些 $__mpe_config / $__mpe_external 就是这么来的
    if removed:
        f.write_text(json.dumps(d, ensure_ascii=False, indent=4), encoding="utf-8")
        json.loads(f.read_text(encoding="utf-8"))  # 自检
        print("%-16s 删了 %d 处" % (f.name, len(removed)))
        total += len(removed)
    else:
        print("%-16s 没有" % f.name)

print("共删除 %d 处" % total)
