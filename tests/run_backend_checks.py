"""运行后端回归并保存本轮结果；业务测试数据仍只使用临时目录。"""
import hashlib
import io
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    directory = ROOT / "docs" / "修复验证"
    directory.mkdir(exist_ok=True)
    (directory / "后端回归日志.txt").write_text(stream.getvalue(), encoding="utf-8")
    files = sorted([ROOT / "main.py", *ROOT.glob("tools/**/*.py"),
                    *ROOT.glob("backend/**/*.py"), *ROOT.glob("frontend/**/*.js"),
                    *ROOT.glob("frontend/**/*.html"), *ROOT.glob("frontend/**/*.css"),
                    *ROOT.glob("frontend/assets/*")])
    snapshot = {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    version = re.search(r'^VERSION\s*=\s*["\']([^"\']+)', (ROOT / 'backend/app.py').read_text(encoding='utf-8'), re.MULTILINE)
    record = {"application_version": version.group(1) if version else 'unknown', "python": sys.version.split()[0], "tests_run": result.testsRun,
              "all_passed": result.wasSuccessful(), "failures": len(result.failures),
              "errors": len(result.errors), "skipped": len(result.skipped), "source_sha256": snapshot}
    (directory / "后端回归.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: record[key] for key in ("tests_run", "all_passed", "failures", "errors", "skipped")}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
