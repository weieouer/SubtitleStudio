# -*- coding: utf-8 -*-
"""把项目改成「打包后也能正确找路径」，可重复执行，不会改乱代码。

为什么需要：
    PyInstaller 打包后，__file__ 指向的是 _internal/ 里的副本，
    Path(__file__).parent.parent 会变成 <exe目录>/_internal，
    于是 data/（模型、上传、导出）会被建到 _internal 里面 ——
    升级时一覆盖就丢数据，肉眼也找不到。

做的事：
    app/config.py 和 app/main.py 里的 BASE_DIR 改成：
        frozen（打包后）→ exe 所在目录
        源码运行        → 项目根目录（行为完全不变）

用法：
    .venv\\Scripts\\python.exe packaging\\make_frozen_safe.py          # 打补丁
    .venv\\Scripts\\python.exe packaging\\make_frozen_safe.py --check  # 只看状态
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FROZEN_BLOCK = '''if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent'''

OLD_LINE = "BASE_DIR = Path(__file__).resolve().parent.parent"

TARGETS = [ROOT / "app" / "config.py", ROOT / "app" / "main.py"]


def ensure_import_sys(text: str) -> str:
    if "\nimport sys\n" in text or text.startswith("import sys\n"):
        return text
    marker = "from __future__ import annotations\n"
    if marker in text:
        return text.replace(marker, marker + "\nimport sys\n", 1)
    return "import sys\n" + text


def patch(path: Path, dry: bool) -> str:
    if not path.exists():
        return f"[跳过] 找不到 {path}"
    text = path.read_text(encoding="utf-8")

    if 'getattr(sys, "frozen", False)' in text:
        return f"[已处理] {path.name} 已经支持打包模式"

    if OLD_LINE not in text:
        return f"[手工看] {path.name} 里没找到预期的 BASE_DIR 写法，跳过"

    new_text = text.replace(OLD_LINE, FROZEN_BLOCK, 1)
    new_text = ensure_import_sys(new_text)

    if dry:
        return f"[待处理] {path.name} 需要打补丁"

    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(new_text, encoding="utf-8", newline="")
    return f"[已修改] {path.name}（原文件备份为 {backup.name}）"


def main() -> int:
    dry = "--check" in sys.argv
    print("=" * 52)
    print("  SubtitleStudio · 打包路径适配")
    print("=" * 52)
    for target in TARGETS:
        print(patch(target, dry))
    print()
    print("提示：这一步只影响打包后的路径，源码运行行为不变。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
