"""准备视频修复环境；--validation 使用独立 CPU 验证环境，不修改界面选择的环境。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    from core import removal_models
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    parser.add_argument('--validation', action='store_true')
    args = parser.parse_args()
    if args.validation:
        removal_models.ENGINE = removal_models.DATA_DIR / 'engines' / 'removal-validation'
    installer = removal_models.INSTALLER
    installer.start(args.device)
    while installer.busy():
        print(json.dumps(installer.get(), ensure_ascii=False), flush=True)
        time.sleep(2)
    print(json.dumps(installer.get(), ensure_ascii=False), flush=True)
    return 0 if installer.get()['status'] == 'done' else 1


if __name__ == '__main__':
    raise SystemExit(main())
