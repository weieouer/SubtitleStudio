"""默认在线 EXE 的选档、暂存清理边界和统一入口路由。"""
import io
import json
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
from tools import package_release, prepare_runtime, release


class RuntimeRecipeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows DLL 搜索路径')
    def test_whisper_finds_cuda_installed_by_source_gpu_button(self):
        from backend.core import bundled_runtime as runtime
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            lib = root/'data/engines/qwen/Lib/site-packages/torch/lib'
            lib.mkdir(parents=True)
            (lib/'cublas64_12.dll').touch()
            with patch.object(runtime,'ROOT_DIR',root), patch.object(runtime,'_DLL_HANDLES',[]), \
                 patch.object(runtime,'_DLL_PATHS',set()), patch.dict(runtime.os.environ), \
                 patch.object(runtime.os,'add_dll_directory') as register:
                runtime.configure_cuda_libraries()
                runtime.configure_cuda_libraries()
                register.assert_called_once_with(str(lib))
                self.assertEqual(runtime.os.environ['PATH'].split(runtime.os.pathsep)[0],str(lib))

    def test_chinese_alignment_does_not_eagerly_initialize_japanese_native_model(self):
        with tempfile.TemporaryDirectory() as folder:
            python=Path(folder)/'python.exe'
            path=python.parent/'Lib/site-packages/qwen_asr/inference/qwen3_forced_aligner.py'
            path.parent.mkdir(parents=True)
            path.write_text('import os\nimport nagisa\nclass Processor:\n'
                            '    def tokenize_japanese(self, text: str) -> List[str]:\n'
                            '        return nagisa.tagging(text).words\n',encoding='utf-8')
            prepare_runtime.defer_japanese_tokenizer(python)
            first=path.read_text(encoding='utf-8')
            self.assertNotIn('\nimport nagisa\n',first)
            self.assertIn('        import nagisa\n',first)
            prepare_runtime.defer_japanese_tokenizer(python)
            self.assertEqual(path.read_text(encoding='utf-8'),first)
    def test_source_dependency_metadata_changes_runtime_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);metadata=root/'.venv/Lib/site-packages/example-1.dist-info/METADATA'
            metadata.parent.mkdir(parents=True);metadata.write_text('Name: example\nVersion: 1\n',encoding='utf-8')
            with patch.object(prepare_runtime,'ROOT',root):
                first=prepare_runtime.runtime_key();metadata.write_text('Name: example\nVersion: 2\n',encoding='utf-8')
                self.assertNotEqual(first,prepare_runtime.runtime_key())

    def test_prepared_runtime_rejects_non_mapping_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);(base/'manifest.json').write_text('[]',encoding='utf-8')
            self.assertFalse(prepare_runtime.prepared_runtime(base)['ready'])
    def test_bundled_runtime_format_two_resolves_from_custom_unicode_directory(self):
        sys.path.insert(0,str(ROOT/'backend'))
        from core import bundled_runtime
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'自定义目录 Subtitle Studio'
            python=root/'runtime/ml/python.exe'
            python.parent.mkdir(parents=True);python.write_bytes(b'fixture')
            (root/'runtime/manifest.json').write_text(json.dumps({'format':2,'ready':True,
                'engines':['qwen','firered'],'ml':'ml/python.exe','torch_flavor':'cpu'}),encoding='utf-8')
            with patch.object(bundled_runtime,'ROOT_DIR',root):
                self.assertEqual(bundled_runtime.python_for('qwen'),python)
                self.assertEqual(bundled_runtime.python_for('firered'),python)

    def test_recipe_config_and_torch_flavor_change_the_reuse_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('requirements-lock.txt', 'config/ui.json', 'config/engines.json'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name, encoding='utf-8')
            with patch.object(prepare_runtime, 'ROOT', root):
                first = prepare_runtime.runtime_key()
                (root / 'config/firered.json').write_text('{"revision":"' + 'a' * 40 + '"}', encoding='utf-8')
                self.assertNotEqual(first, prepare_runtime.runtime_key())
                self.assertNotEqual(prepare_runtime.runtime_key(), prepare_runtime.runtime_key(cuda=True))

    def test_prepared_runtime_requires_matching_key_and_layout_files(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            for name in ('requirements-lock.txt', 'config/ui.json', 'config/engines.json'):
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name, encoding='utf-8')
            manifest = {'format': 2, 'ready': True, 'torch_flavor': 'cpu',
                        'dependency_sha256': ''}
            with patch.object(prepare_runtime, 'ROOT', base):
                manifest['dependency_sha256'] = prepare_runtime.runtime_key()
                (base / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
                status = prepare_runtime.prepared_runtime(base)
                self.assertFalse(status['ready'])
                self.assertIn('关键文件', status['reason'])
                for name in prepare_runtime.LAYOUT:
                    path = base / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'x')
                self.assertTrue(prepare_runtime.prepared_runtime(base)['ready'])
                manifest['dependency_sha256'] = '0' * 64
                (base / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
                self.assertIn('配方', prepare_runtime.prepared_runtime(base)['reason'])


class InstallerTests(unittest.TestCase):
    def test_upgrade_preserves_user_configuration_and_model_directories(self):
        script=(ROOT/'installer/SubtitleStudio.iss').read_text(encoding='utf-8')
        files=script.split('[Files]',1)[1].split('[Icons]',1)[0]
        self.assertIn('Flags: onlyifdoesntexist',files)
        self.assertIn('Excludes: "config\\*"',files)
        self.assertIn('UsePreviousAppDir=yes',script)
        self.assertNotIn('[InstallDelete]',script)
        self.assertIn('AppId={{FFE1553D-606B-4F3E-8460-641574C7776D}',script)

    def make_source(self, root):
        for name in (*[name for name in package_release.REQUIRED if name != 'run.bat'],
                     'tools/portable/run.bat', 'installer/首次使用说明.txt', 'main.py',
                     'wheels/dependency.whl', 'docs/private.md', 'tests/private.py',
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
