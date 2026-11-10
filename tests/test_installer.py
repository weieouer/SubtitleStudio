"""默认在线 EXE 的选档、暂存清理边界和统一入口路由。"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main
from installer import build_installer as installer
from tools import package_release, release


class InstallerTests(unittest.TestCase):
    def make_source(self, root):
        for name in (*[name for name in package_release.REQUIRED if name != 'run.bat'],
                     'tools/portable/run.bat', 'installer/首次使用说明.txt', 'main.py',
                     'wheels/dependency.whl', 'docs/private.md', 'tests/private.py', 'config/removal.json', 'config/removal-scan.json',
                     '.venv/secret.txt', 'data/models/secret.txt', 'data/projects/secret.json'):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')

    def test_online_stage_keeps_runtime_but_excludes_dependencies_and_developer_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_source(root)
            stage = root / 'installer/build/stage-online'
            with patch.object(installer, 'ROOT', root), patch.object(installer, 'INSTALLER', root / 'installer'), \
                    redirect_stdout(io.StringIO()):
                installer.stage_release(stage, offline=False)
            self.assertTrue((stage / 'runtime/python/python.exe').is_file())
            self.assertTrue((stage / 'run.bat').is_file())
            self.assertTrue((stage / 'tools/bootstrap.py').is_file())
            self.assertTrue((stage / 'config/removal.json').is_file())
            self.assertTrue((stage / 'config/removal-scan.json').is_file())
            self.assertTrue((stage / '首次使用说明.txt').is_file())
            self.assertFalse(any((stage / name).exists() for name in
                                 ('wheels', '.venv', 'data', 'docs', 'tests', 'main.py')))

    def test_optional_legacy_offline_stage_can_still_include_wheels(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_source(root)
            stage = root / 'installer/build/stage-offline'
            with patch.object(installer, 'ROOT', root), patch.object(installer, 'INSTALLER', root / 'installer'), \
                    redirect_stdout(io.StringIO()):
                installer.stage_release(stage, offline=True)
            self.assertTrue((stage / 'wheels/dependency.whl').is_file())

    def test_stage_refuses_to_delete_outside_build_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sentinel = root / 'keep.txt'
            sentinel.write_text('keep', encoding='utf-8')
            with patch.object(installer, 'INSTALLER', root / 'installer'), \
                    patch.object(installer.shutil, 'rmtree') as delete:
                with self.assertRaises(ValueError):
                    installer.stage_release(root, offline=False)
                delete.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')

    def test_rebuilding_online_stage_removes_stale_bundled_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_source(root)
            stage = root / 'installer/build/stage-online'
            stale = stage / 'wheels/old.whl'
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b'old')
            with patch.object(installer, 'ROOT', root), patch.object(installer, 'INSTALLER', root / 'installer'), \
                    redirect_stdout(io.StringIO()):
                installer.stage_release(stage, offline=False)
            self.assertFalse((stage / 'wheels').exists())

    def test_main_build_defaults_to_full_installer(self):
        with patch.object(release, 'build_installer', return_value={'edition': 'full'}) as build, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(['build', '--stage-only']), 0)
            self.assertEqual(build.call_args.args[0], ROOT / 'dist/full')
            self.assertTrue(build.call_args.kwargs['stage_only'])

    def test_installer_build_preserves_existing_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            artifact = output / 'SubtitleStudio-Setup-test-x64-full.exe'
            artifact.write_bytes(b'existing')
            with patch.object(installer, 'app_version', return_value='test'), \
                    patch.object(release, 'validate_environment', return_value=Path(sys.executable)), \
                    patch.object(release.subprocess, 'run') as compiler:
                with self.assertRaises(FileExistsError):
                    release.build_installer(output, ROOT / '.venv')
                compiler.assert_not_called()
            self.assertEqual(artifact.read_bytes(), b'existing')


if __name__ == '__main__':
    unittest.main()
