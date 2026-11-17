"""运行后端回归并保存本轮结果；业务测试数据仍只使用临时目录。"""
import hashlib
import io
import json
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _files():
    patterns = [ROOT / "main.py", ROOT / "requirements-lock.txt",
                *ROOT.glob("tools/**/*.py"), *ROOT.glob("backend/**/*.py"),
                *ROOT.glob("frontend/**/*.js"), *ROOT.glob("frontend/**/*.html"),
                *ROOT.glob("frontend/**/*.css"), *ROOT.glob("frontend/assets/*"),
                *ROOT.glob("tests/**/*.py"), *ROOT.glob("tests/**/*.mjs"),
                *ROOT.glob("installer/**/*.py"), *ROOT.glob("installer/**/*.iss"),
                *ROOT.glob("config/*.json")]
    # installer/build 下是暂存产物，不属于测试输入。
    return sorted({p for p in patterns if p.is_file() and "build" not in p.relative_to(ROOT).parts})


def snapshot():
    return {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in _files()}


def _git(*arguments):
    try:
        return subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def main():
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    started = time.monotonic()
    before = snapshot()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    after = snapshot()
    directory = ROOT / "docs" / "修复验证"
    directory.mkdir(exist_ok=True)
    (directory / "后端回归日志.txt").write_text(stream.getvalue(), encoding="utf-8")
    version = re.search(r'^VERSION\s*=\s*["\']([^"\']+)', (ROOT / 'backend/app.py').read_text(encoding='utf-8'), re.MULTILINE)
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    record = {"application_version": version.group(1) if version else 'unknown',
              "python": sys.version.split()[0], "tests_run": result.testsRun,
              "all_passed": result.wasSuccessful(), "failures": len(result.failures),
              "errors": len(result.errors), "skipped": len(result.skipped),
              "git_commit": _git("rev-parse", "HEAD"),
              "git_dirty_before": bool(_git("status", "--porcelain")),
              "started_at": started_at,
              "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "duration_seconds": round(time.monotonic() - started, 3),
              "source_sha256": before,
              "source_sha256_after": after,
              "source_changed_during_tests": changed,
              "usable_as_baseline": result.wasSuccessful() and not changed}
    (directory / "后端回归.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: record[key] for key in ("tests_run", "all_passed", "failures", "errors", "skipped",
                                                    "source_changed_during_tests", "usable_as_baseline")}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
