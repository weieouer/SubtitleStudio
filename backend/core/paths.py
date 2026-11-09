"""项目路径常量：所有模块共用，避免相对路径混乱。"""
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
CONFIG_DIR = ROOT_DIR / "config"
FRONTEND_DIR = ROOT_DIR / "frontend"
MODELS_DIR = DATA_DIR / "models"
UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUTS_DIR = DATA_DIR / "outputs"
RESULTS_DIR = DATA_DIR / "results"
PROJECTS_DIR = DATA_DIR / "projects"

for _dir in (CONFIG_DIR, UPLOADS_DIR, OUTPUTS_DIR, RESULTS_DIR, PROJECTS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)
