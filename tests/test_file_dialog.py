"""Windows 文件选择框的配置、窗口尺寸和资源清理。"""
import ctypes
from ctypes import wintypes as w
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
    def test_explorer_folder_opens_itself_and_always_releases_shell_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            ole,shell=MagicMock(),MagicMock()
            ole.CoInitializeEx.return_value=0
            def parse(path,bind,pidl,flags,attributes):
                ctypes.cast(pidl,ctypes.POINTER(ctypes.c_void_p))[0]=123
                return 0
            shell.SHParseDisplayName.side_effect=parse
            shell.SHOpenFolderAndSelectItems.return_value=-2147467259
            with patch.object(ctypes,'WinDLL',side_effect=[ole,shell]):
                with self.assertRaises(OSError):
                    file_dialog.open_in_explorer(Path(folder))
            args=shell.SHOpenFolderAndSelectItems.call_args.args
            self.assertEqual(args[1],1)
            ole.CoTaskMemFree.assert_called_once()
            ole.CoUninitialize.assert_called_once()

    def test_file_dialog_expands_and_centers_on_its_monitor(self):
        old = w.RECT(-1300, 55, -700, 505)
        work = w.RECT(-1920, 15, 0, 1055)
        self.assertEqual(file_dialog.dialog_bounds(old, work, 120), (-1585, 122, 1250, 825))

    def test_bounds_fit_small_work_area_and_keep_larger_saved_size(self):
        window = w.RECT(10, 0, 620, 475)
        small = w.RECT(0, 0, 800, 560)
        self.assertEqual(file_dialog.dialog_bounds(window, small, 144), (40, 28, 720, 504))
        larger = w.RECT(120, 42, 1320, 842)
        work = w.RECT(0, 0, 1920, 1040)
        self.assertEqual(file_dialog.dialog_bounds(larger, work, 96), (120, 42, 1200, 800))

    def test_only_first_owned_dialog_is_brought_to_front(self):
        user, kernel = MagicMock(), MagicMock()
        kernel.GetCurrentThreadId.return_value = 50
        user.GetWindow.side_effect = [None, 123]

        def show(dialog, owner):
            callback = user.SetWindowsHookExW.call_args.args[1]
            callback(3, 456, 0)
            callback(5, 789, 0)
            callback(5, 456, 0)
            callback(5, 456, 0)
            return -2147023673  # HRESULT_FROM_WIN32(ERROR_CANCELLED)

        with patch.object(ctypes, 'WinDLL', return_value=kernel), \
                patch.object(file_dialog, 'com_method', return_value=show), \
                patch.object(file_dialog, 'foreground_dialog') as foreground:
            self.assertEqual(file_dialog.show_dialog(1, user, 123), -2147023673)
        foreground.assert_called_once_with(user, 456)
        self.assertEqual(user.CallNextHookEx.call_count, 4)
        user.UnhookWindowsHookEx.assert_called_once_with(user.SetWindowsHookExW.return_value)

    def test_configures_filters_multiselect_and_remembered_folder(self):
        class Options:
            def get_options(self, dialog, output):
                ctypes.cast(output, ctypes.POINTER(w.DWORD)).contents.value = 0x1808
                return 0

            def get_filters(self, dialog, count, filters):
                self.filters = [(filters[index].name, filters[index].pattern) for index in range(count)]
                return 0

            def create_item(self, path, context, interface, output):
                ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 1234
                return 0

        options = Options()
        shell = MagicMock()
        shell.SHCreateItemFromParsingName.side_effect = options.create_item
        set_index, set_options, set_title, set_folder, set_guid = (MagicMock(return_value=0) for _ in range(5))
        methods = {4: options.get_filters, 5: set_index, 9: set_options, 10: options.get_options, 12: set_folder,
                   17: set_title, 24: set_guid}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(file_dialog, 'com_method', side_effect=lambda _, slot, *args: methods[slot]), \
                patch.object(file_dialog, 'release') as release:
            file_dialog.configure_dialog(1, {'kind': 'script', 'initial_dir': folder, 'multiple': True}, shell)
        self.assertEqual(options.filters, [('剧本', '*.txt;*.md;*.docx'), ('所有文件', '*.*')])
        set_index.assert_called_once_with(1, 1)
        set_options.assert_called_once_with(1, 0x1808 | file_dialog.FOS_FORCEFILESYSTEM | file_dialog.FOS_ALLOWMULTISELECT)
        set_title.assert_called_once_with(1, '选择文件')
        self.assertEqual(shell.SHCreateItemFromParsingName.call_args.args[0], folder)
        self.assertEqual(set_folder.call_args.args[1].value, 1234)
        self.assertEqual(release.call_args.args[0].value, 1234)

    def test_folder_only_mode_uses_folder_title(self):
        shell = MagicMock()
        set_options, set_title = MagicMock(return_value=0), MagicMock(return_value=0)
        methods = {9: set_options, 10: MagicMock(return_value=0), 17: set_title, 24: MagicMock(return_value=0)}
        with patch.object(file_dialog, 'com_method', side_effect=lambda _, slot, *args: methods[slot]):
            file_dialog.configure_dialog(1, {'kind': 'dir', 'multiple': True}, shell)
        set_options.assert_called_once_with(1, file_dialog.FOS_FORCEFILESYSTEM | file_dialog.FOS_PICKFOLDERS)
        set_title.assert_called_once_with(1, '选择文件夹')
        shell.SHCreateItemFromParsingName.assert_not_called()

    def run_dialog(self, hresult):
        user, ole, shell = MagicMock(), MagicMock(), MagicMock()
        user.CreateWindowExW.return_value = 123
        ole.CoInitializeEx.return_value = 0

        def create(clsid, outer, context, iid, output):
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 456
            return 0

        ole.CoCreateInstance.side_effect = create
        with patch.object(ctypes, 'WinDLL', side_effect=[user, ole, shell]), \
                patch.object(file_dialog, 'configure_dialog'), \
                patch.object(file_dialog, 'show_dialog', return_value=hresult), \
                patch.object(file_dialog, 'release') as release, \
                patch.object(file_dialog, 'dialog_paths', return_value=['C:\\测试 目录\\第一.txt']) as paths:
            try:
                return file_dialog.windows_dialog({'kind': 'media'})
            finally:
                user.DestroyWindow.assert_called_once_with(123)
                ole.CoUninitialize.assert_called_once()
                self.assertEqual(release.call_args.args[0].value, 456)
                self.assertEqual(paths.call_count, 1 if hresult == 0 else 0)

    def test_cancel_is_empty_and_system_failure_releases_resources(self):
        self.assertEqual(self.run_dialog(-2147023673), [])
        self.assertEqual(self.run_dialog(0), ['C:\\测试 目录\\第一.txt'])
        with self.assertRaisesRegex(OSError, '0x80004005'):
            self.run_dialog(-2147467259)

    def test_windows_dispatch_does_not_import_tkinter(self):
        with patch.dict(sys.modules, {'tkinter': None}), \
                patch.object(file_dialog, 'windows_dialog', return_value=[]) as native:
            self.assertEqual(file_dialog.open_dialog({'kind': 'dir'}), [])
            native.assert_called_once()


if __name__ == '__main__':
    unittest.main()
