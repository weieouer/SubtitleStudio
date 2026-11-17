"""覆盖无系统 CRT 的发布边界和对齐错误分类。"""
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'backend'))
from core import windows_runtime
from tools import prepare_vc_runtime
from tools import prepare_runtime


def dll(path, machine=0x8664):
    content = bytearray(134)
    content[:2] = b'MZ'
    struct.pack_into('<I', content, 0x3c, 128)
    content[128:134] = b'PE\0\0' + struct.pack('<H', machine)
    path.write_bytes(content)


class WindowsRuntimeTests(unittest.TestCase):
    def test_complete_crt_is_copied_as_a_set_and_required_in_both_pythons(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'CRT'; source.mkdir()
            target = Path(folder) / 'python'; target.mkdir()
            for name in windows_runtime.VC_DLLS:
                dll(source / name)
            with patch.dict(os.environ, {'SUBTITLESTUDIO_VC_REDIST_DIR': str(source)}):
                prepare_vc_runtime.copy_crt(target)
            for name in windows_runtime.VC_DLLS:
                self.assertEqual((source / name).read_bytes(), (target / name).read_bytes())
                for environment in ('python', 'ml'):
                    self.assertIn(f'{environment}/{name}', prepare_runtime.LAYOUT)

    def test_partial_or_x86_crt_fails_even_when_system_crt_is_installed(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            with patch.dict(os.environ, {'SUBTITLESTUDIO_VC_REDIST_DIR': folder}):
                with self.assertRaises(RuntimeError):
                    prepare_vc_runtime.crt_source()
                for name in windows_runtime.VC_DLLS:
                    dll(source / name)
                dll(source / 'msvcp140.dll', machine=0x14c)
                with self.assertRaises(RuntimeError):
                    prepare_vc_runtime.crt_source()

    def test_known_crt_error_explains_repair_without_discarding_calibration(self):
        log = ('Microsoft Visual C++ Redistributable is not installed\n'
               + 'Traceback\n' * 300 + 'OSError: [WinError 126] c10.dll')
        message = windows_runtime.engine_failure(log)
        self.assertIn(windows_runtime.VC_DOWNLOAD, message)
        self.assertIn('无需重新校准', message)
        self.assertNotIn('Traceback', message)

    def test_unrelated_model_or_cuda_error_keeps_diagnostics(self):
        for log in ('RuntimeError: CUDA out of memory', 'OSError: [WinError 126] c10.dll',
                    'ValueError: invalid model'):
            self.assertEqual(windows_runtime.engine_failure(log), log)

    @unittest.skipUnless(os.name == 'nt', 'Windows DLL search path')
    def test_engine_keeps_dll_directory_handle_alive_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            dll(Path(folder) / 'msvcp140.dll')
            with patch.object(windows_runtime.sys, 'executable', str(Path(folder) / 'python.exe')), \
                    patch.object(windows_runtime, '_DLL_HANDLES', []), \
                    patch.object(windows_runtime, '_DLL_PATHS', set()), \
                    patch.object(os, 'add_dll_directory') as add:
                windows_runtime.configure_libraries()
                windows_runtime.configure_libraries()
                self.assertEqual(sum(c.args == (str(Path(folder).resolve()),) for c in add.call_args_list), 1)
                self.assertIn(add.return_value, windows_runtime._DLL_HANDLES)


if __name__ == '__main__':
    unittest.main()
