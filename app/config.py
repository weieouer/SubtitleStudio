# -*- coding: utf-8 -*-
"""路径配置。所有运行数据都留在项目目录里，方便以后打包。"""
from __future__ import annotations

import sys

from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
MODELS_DIR = DATA_DIR / "models"
RESULTS_DIR = DATA_DIR / "results"

for _dir in (UPLOAD_DIR, OUTPUT_DIR, MODELS_DIR, RESULTS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)
