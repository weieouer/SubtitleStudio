# -*- coding: utf-8 -*-
"""导出位置同步的回归测试：完成后再改导出位置，点「导出 SRT」两处保持一致。

运行：.venv\\Scripts\\python.exe tests\\test_export.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.main as main  # noqa: E402
from app import jobs  # noqa: E402
from app.jobs import Job  # noqa: E402


def check_export_sync() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="ss-export-"))
    original_results = jobs.RESULTS_DIR
    original_outputs = main.OUTPUT_DIR
    job_id = "export-sync-test"
    try:
        default_out = scratch / "outputs"
        default_out.mkdir()
        results = scratch / "results"
        results.mkdir()
        jobs.RESULTS_DIR = results
        main.OUTPUT_DIR = default_out

        source = default_out / "wb.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n测试\n", encoding="utf-8"
        )
        jobs.register(
            Job(
                id=job_id,
                media_path=scratch / "wb.mp4",
                script_path=scratch / "script.txt",
                output_dir=default_out,
                stem="wb",
                options={"mode": "align"},
                status="done",
                srt_path=source,
            )
        )

        from fastapi.testclient import TestClient

        target = scratch / "Desktop"
        with TestClient(main.app) as client:
            # 留空 = 默认目录，文件已经在里面，不应再搬动。
            same = client.post(
                f"/api/jobs/{job_id}/export", data={"export_dir": ""}
            )
            assert same.status_code == 200, same.text
            assert same.json()["moved"] is False, same.text
            assert source.is_file()

            # 改成自定义目录：文件移过去，返回新路径。
            moved = client.post(
                f"/api/jobs/{job_id}/export",
                data={"export_dir": str(target)},
            )
            assert moved.status_code == 200, moved.text
            data = moved.json()
            assert data["moved"] is True, data
            assert Path(data["output_path"]) == target / "wb.srt", data
            assert (target / "wb.srt").is_file()
            assert not source.exists()

            # 再点一次应该幂等，不会生成「wb (2).srt」。
            again = client.post(
                f"/api/jobs/{job_id}/export",
                data={"export_dir": str(target)},
            )
            assert again.status_code == 200, again.text
            assert again.json()["moved"] is False, again.text
            assert not (target / "wb (2).srt").exists()

        record = json.loads((results / f"{job_id}.json").read_text(encoding="utf-8"))
        assert Path(record["srt_path"]) == target / "wb.srt", record
        assert record["stem"] == "wb", record
    finally:
        jobs.RESULTS_DIR = original_results
        main.OUTPUT_DIR = original_outputs
        jobs._JOBS.pop(job_id, None)
        shutil.rmtree(scratch, ignore_errors=True)

    print("OK  导出 SRT：文件移到当前导出位置，卡片路径同步")


if __name__ == "__main__":
    check_export_sync()
