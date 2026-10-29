# -*- coding: utf-8 -*-
"""把本机已经下载好的 whisper 模型拷进发布目录，让对方开箱即用。

为什么不在 spec 里打包：
    模型在运行时才被读取，且体积大（small 约 470MB）；
    塞进 _internal 里既拖慢启动，又会让"升级只换程序、不动模型"变得不可能。
    放在 exe 旁边的 data\\models 里，升级时可跳过、丢失可单补。

用法：
    python packaging\\bundle_models.py --dist dist\\SubtitleStudio            # 拷所有完整模型
    python packaging\\bundle_models.py --dist dist\\SubtitleStudio --only small  # 只拷 small
    python packaging\\bundle_models.py --list                                 # 只看本机有哪些
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_MODELS = ROOT / "data" / "models"


def size_mb(path: Path) -> float:
    total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return total / (1024 * 1024)


def discover() -> list[tuple[str, Path]]:
    """返回 [(模型名, 模型缓存目录)]，只认有 model.bin 的完整模型。"""
    found: list[tuple[str, Path]] = []
    if not SRC_MODELS.is_dir():
        return found
    for folder in sorted(SRC_MODELS.glob("models--*")):
        snapshots = folder / "snapshots"
        if not snapshots.is_dir():
            continue
        complete = False
        for snap in snapshots.iterdir():
            if (snap / "model.bin").is_file() and (snap / "config.json").is_file():
                complete = True
                break
        if not complete:
            continue
        name = folder.name
        if "faster-whisper-" in name:
            name = name.split("faster-whisper-")[-1]
        found.append((name, folder))
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default="dist/SubtitleStudio", help="发布目录（exe 所在目录）")
    ap.add_argument("--only", default="", help="只拷某个模型，例如 small")
    ap.add_argument("--list", action="store_true", help="只列出本机已有模型")
    args = ap.parse_args()

    models = discover()
    if not models:
        print("[!] data\\models 里没有完整模型，跳过内置（用户首次启动会自己下载）")
        return 0

    print("本机可用模型：")
    for name, folder in models:
        print(f"  - {name:<10} {size_mb(folder):7.0f} MB   {folder.name}")

    if args.list:
        return 0

    target_root = Path(args.dist).resolve() / "data" / "models"
    target_root.mkdir(parents=True, exist_ok=True)

    picked = [(n, f) for n, f in models if not args.only or n == args.only]
    if not picked:
        print(f"[!] 没有找到名为 {args.only} 的完整模型，跳过")
        return 0

    total = 0.0
    for name, folder in picked:
        dest = target_root / folder.name
        if dest.exists():
            shutil.rmtree(dest)
        print(f"[+] 内置 {name} -> {dest}")
        shutil.copytree(folder, dest)
        total += size_mb(dest)

    print(f"[完成] 共内置 {len(picked)} 个模型，{total:.0f} MB，位于 {target_root}")
    print("      用户首次启动即可离线用这几个模型，其余模型在界面里按需下载。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
