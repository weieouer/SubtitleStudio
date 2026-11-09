"""验证源码运行与便携发布隔离，所有安装命令固定到项目虚拟环境。"""
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main
from tools import bootstrap, development, package_release, release


class DevelopmentWorkflowTests(unittest.TestCase):
    def test_default_environment_and_explicit_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(development.environment_path(), (ROOT / '.venv').resolve())
        with patch.dict(os.environ, {'SUBTITLESTUDIO_DEV_VENV': 'custom'}):
            self.assertEqual(development.environment_path(), Path('custom').resolve())
            self.assertEqual(development.environment_path('explicit'), Path('explicit').resolve())

    def test_global_python_and_source_root_are_rejected(self):
        for directory in (Path(sys.base_prefix).resolve(), ROOT):
            with self.assertRaises(ValueError):
                development.validate_target(directory)

    def test_non_environment_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            sentinel = Path(folder) / 'keep.txt'
            sentinel.write_text('keep', encoding='utf-8')
            with patch.object(development.subprocess, 'run') as create:
                with self.assertRaises(ValueError):
                    development.setup_environment(Path(folder))
                create.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')

    def test_inheriting_global_packages_is_rejected_before_running_python(self):
        with tempfile.TemporaryDirectory() as folder:
            environment = Path(folder)
            (environment / 'pyvenv.cfg').write_text('include-system-site-packages = true\n', encoding='utf-8')
            with patch.object(development.subprocess, 'run') as probe:
                with self.assertRaises(ValueError):
                    development.validate_environment(environment)
                probe.assert_not_called()

    def test_installation_uses_explicit_python_and_ignores_global_pip_configuration(self):
        python = Path('C:/environments/project/Scripts/python.exe')
        command = development.install_command(python, wheels=ROOT / 'wheels')
        self.assertEqual(command[:5], [str(python), '-I', '-m', 'pip', '--isolated'])
        self.assertIn('--no-index', command)
        self.assertIn('--only-binary=:all:', command)
        self.assertIn(str(ROOT / 'requirements.txt'), command)

    def test_process_environment_removes_package_redirection(self):
        with patch.dict(os.environ, {'PYTHONPATH': 'global', 'PIP_TARGET': 'global', 'PIP_USER': 'yes'}):
            environment = development.process_environment()
            self.assertNotIn('PYTHONPATH', environment)
            self.assertNotIn('PIP_TARGET', environment)
            self.assertNotIn('PIP_USER', environment)
            self.assertEqual(environment['PYTHONNOUSERSITE'], '1')
            self.assertEqual(environment['PIP_REQUIRE_VIRTUALENV'], 'true')

    def test_setup_passes_isolated_child_environment_without_changing_parent(self):
        environment = development.environment_path()
        python = development.python_path(environment)
        with patch.dict(os.environ, {'PIP_TARGET': 'unrelated', 'PYTHONPATH': 'unrelated'}), \
                patch.object(development, 'validate_environment', return_value=python), \
                patch.object(development.subprocess, 'run'), \
                patch.object(development, 'run_pip', return_value=0) as install:
            development.setup_environment(environment)
            self.assertNotIn('PIP_TARGET', install.call_args.kwargs['env'])
            self.assertNotIn('PYTHONPATH', install.call_args.kwargs['env'])
            self.assertEqual(os.environ['PIP_TARGET'], 'unrelated')
            self.assertEqual(os.environ['PYTHONPATH'], 'unrelated')

    def test_setup_only_does_not_start_application(self):
        with patch.object(main, 'setup_environment') as setup, patch.object(main.subprocess, 'call') as launch:
            self.assertEqual(main.main(['setup']), 0)
            setup.assert_called_once()
            launch.assert_not_called()

    def test_main_from_global_python_relaunches_in_project_environment(self):
        python = Path('C:/environments/project/Scripts/python.exe')
        with patch.object(sys, 'prefix', sys.base_prefix), patch.object(main, 'validate_environment', return_value=python), \
                patch.object(main.subprocess, 'call', return_value=0) as launch:
            self.assertEqual(main.main(['--port', '9988', '--no-browser']), 0)
            self.assertEqual(launch.call_args.args[0], [str(python), str(ROOT / 'main.py'), 'run', '--port', '9988', '--no-browser'])

    def test_main_in_virtual_environment_runs_source_directly(self):
        environment = development.environment_path()
        with patch.object(sys, 'prefix', str(environment)), \
                patch.object(main, 'validate_environment', return_value=development.python_path(environment)), \
                patch.object(main, 'run_application', return_value=0) as run, patch.object(main.subprocess, 'call') as launch:
            self.assertEqual(main.main(['--no-browser']), 0)
            run.assert_called_once_with(['--port', '8765', '--no-browser'])
            launch.assert_not_called()

    def test_build_rejects_existing_or_in_source_output_before_preparing_wheels(self):
        with patch.object(release, 'validate_environment') as validate, redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError):
                release.build_portable(ROOT / 'bad.zip', development.environment_path())
            validate.assert_not_called()

    def test_help_is_available_without_environment_or_backend_import(self):
        with patch.object(main, 'validate_environment') as validate:
            with self.assertRaises(SystemExit) as result:
                main.main(['--help'])
            self.assertEqual(result.exception.code, 0)
            validate.assert_not_called()

    def test_cli_rejects_invalid_port_before_starting_application(self):
        with patch.object(main, 'validate_environment') as validate, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main.main(['--port', '-1'])
            with self.assertRaises(SystemExit):
                main.main(['--port', '65536'])
            validate.assert_not_called()

    def test_an_unrelated_active_venv_does_not_bypass_project_environment(self):
        python = development.python_path(development.environment_path())
        with patch.object(sys, 'prefix', str(ROOT / 'unrelated')), \
                patch.object(main, 'validate_environment', return_value=python), \
                patch.object(main.subprocess, 'call', return_value=0) as launch:
            self.assertEqual(main.main([]), 0)
            self.assertEqual(launch.call_args.args[0], [str(python), str(ROOT / 'main.py'), 'run'])

    def test_portable_launcher_is_selected_from_template_without_root_bat(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in (*[name for name in package_release.REQUIRED if name != 'run.bat'],
                         'tools/portable/run.bat', 'main.py', 'wheels/test.whl',
                         'tools/portable/首次使用说明.txt',
                         '.venv/secret.txt', 'data/models/secret.txt'):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'test')
            files = package_release.selected_files(root, include_work=False)
            self.assertEqual(files['run.bat'], root / 'tools/portable/run.bat')
            self.assertEqual(files['首次使用说明.txt'], root / 'tools/portable/首次使用说明.txt')
            self.assertIn('main.py', files)
            self.assertIn('runtime/python/python.exe', files)
            self.assertIn('wheels/test.whl', files)
            self.assertFalse(any(name.startswith(('.venv/', 'data/')) for name in files))

    def test_bat_template_is_ascii_crlf_and_source_has_no_bat_entry(self):
        template = (ROOT / 'tools/portable/run.bat').read_bytes()
        template.decode('ascii')
        self.assertNotIn(b'\n', template.replace(b'\r\n', b''))
        self.assertFalse((ROOT / 'run.bat').exists())

    def test_portable_bootstrap_never_accepts_global_interpreter_as_venv(self):
        python = Path(getattr(sys, '_base_executable', None) or sys.executable)
        with patch.object(bootstrap, 'VENV', Path(sys.base_prefix)), patch.object(bootstrap, 'VENV_PY', python):
            self.assertFalse(bootstrap.venv_ready())


if __name__ == '__main__':
    unittest.main()
