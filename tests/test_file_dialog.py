"""原生文件选择协议：Unicode 多选、取消、错误和句柄释放（不弹出交互窗口）。"""
import ctypes
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core import file_dialog


@unittest.skipUnless(os.name == 'nt', 'Windows 原生选择框')
class FileDialogTests(unittest.TestCase):
    def run_dialog(self, raw='', error=0, initial_dir='', expect_initial=None):
        user, common = MagicMock(), MagicMock()
        user.CreateWindowExW.return_value = 123
        common.CommDlgExtendedError.return_value = error
        def select(pointer):
            options = ctypes.cast(pointer, ctypes.POINTER(file_dialog.OpenFileName)).contents
            self.assertTrue(options.Flags & 0x200)
            self.assertEqual(options.lStructSize, ctypes.sizeof(file_dialog.OpenFileName))
            self.assertEqual(options.lpstrInitialDir, expect_initial)
            if not raw:
                return False
            data = ctypes.create_unicode_buffer(raw)
            ctypes.memmove(options.lpstrFile, data, ctypes.sizeof(data))
            return True
        common.GetOpenFileNameW.side_effect = select
        with patch.object(ctypes, 'WinDLL', side_effect=[user, common]):
            try:
                return file_dialog.windows_dialog({'kind':'script','multiple':True,'initial_dir':initial_dir})
            finally:
                user.DestroyWindow.assert_called_once_with(123)

    def test_single_and_multiple_unicode_paths(self):
        self.assertEqual(self.run_dialog('C:\\测试 目录\\第一.txt\0\0'), ['C:\\测试 目录\\第一.txt'])
        self.assertEqual(self.run_dialog('C:\\测试 目录\0第一.txt\0第二.md\0\0'),
                         ['C:\\测试 目录\\第一.txt', 'C:\\测试 目录\\第二.md'])

    def test_initial_directory_is_used_and_missing_one_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(self.run_dialog('C:\\测试 目录\\第一.txt\0\0', initial_dir=folder, expect_initial=folder),
                             ['C:\\测试 目录\\第一.txt'])
        self.run_dialog('C:\\测试 目录\\第一.txt\0\0', initial_dir='C:\\不存在的目录-字幕工作室')

    def test_cancel_is_empty_and_system_failure_is_reported(self):
        self.assertEqual(self.run_dialog(), [])
        with self.assertRaisesRegex(RuntimeError, '0x3003'):
            self.run_dialog(error=0x3003)

    def test_windows_dispatch_does_not_import_tkinter(self):
        with patch.dict(sys.modules, {'tkinter':None}), patch.object(file_dialog, 'windows_dialog', return_value=[]) as native:
            self.assertEqual(file_dialog.open_dialog({'kind':'dir'}), [])
            native.assert_called_once()

    def test_directory_unicode_path_and_com_resources_are_released(self):
        shell, ole = MagicMock(), MagicMock()
        shell.SHBrowseForFolderW.return_value=456
        def selected(pidl, buffer, size, flags):
            buffer.value='C:\\测试 目录';return True
        shell.SHGetPathFromIDListEx.side_effect=selected
        with patch.object(ctypes,'WinDLL',return_value=shell),patch.object(ctypes,'OleDLL',return_value=ole):
            self.assertEqual(file_dialog.windows_directory(123,'选择文件夹'),['C:\\测试 目录'])
            ole.CoTaskMemFree.assert_called_once_with(456)
            ole.CoUninitialize.assert_called_once()

    def test_directory_dialog_selects_remembered_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            shell, ole = MagicMock(), MagicMock()
            seen = {}
            def browse(pointer):
                info = ctypes.cast(pointer, ctypes.POINTER(file_dialog.BrowseInfo)).contents
                seen['info'] = info
                callback = ctypes.cast(info.lpfn, file_dialog.BrowseCallback)
                callback(321, file_dialog.BFFM_INITIALIZED, 0, 0)
                return 456
            def selected(pidl, buffer, size, flags):
                buffer.value = folder
                return True
            shell.SHBrowseForFolderW.side_effect = browse
            shell.SHGetPathFromIDListEx.side_effect = selected
            with patch.object(ctypes, 'WinDLL', return_value=shell), patch.object(ctypes, 'OleDLL', return_value=ole):
                self.assertEqual(file_dialog.windows_directory(123, '选择文件夹', folder), [folder])
            self.assertTrue(seen['info'].lpfn)
            args = shell.SendMessageW.call_args.args
            self.assertEqual(args[0], 321)
            self.assertEqual(args[1], file_dialog.BFFM_SETSELECTIONW)
            self.assertEqual(args[2], 1)
            ole.CoTaskMemFree.assert_called_once_with(456)


if __name__ == '__main__':
    unittest.main()
