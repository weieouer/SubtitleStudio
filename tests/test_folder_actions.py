"""文件选择位置记忆与“打开文件夹”行为。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import app
from core import media


class DialogDirectoryMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'dialog_dirs.json'
        patcher = patch.object(app, 'DIALOG_DIRS_FILE', self.file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.a = self.root / 'A'
        self.b = self.root / 'B'
        self.a.mkdir()
        self.b.mkdir()

    def test_remembers_each_kind_and_falls_back_to_last(self):
        app._remember_dialog_dir('script', str(self.a))
        self.assertEqual(app._dialog_start_dir('script'), str(self.a))
        self.assertEqual(app._dialog_start_dir('media'), str(self.a))
        app._remember_dialog_dir('media', str(self.b))
        self.assertEqual(app._dialog_start_dir('script'), str(self.a))
        self.assertEqual(app._dialog_start_dir('media'), str(self.b))
        self.assertEqual(app._dialog_start_dir('dir'), str(self.b))
        data = json.loads(self.file.read_text(encoding='utf-8'))
        self.assertEqual(data['script'], str(self.a))
        self.assertEqual(data['last'], str(self.b))

    def test_stale_or_corrupt_locations_are_ignored(self):
        self.file.write_text(json.dumps({'script': str(self.root / 'missing'),
                                         'last': str(self.b)}), encoding='utf-8')
        self.assertEqual(app._dialog_start_dir('script'), str(self.b))
        self.file.write_text('{', encoding='utf-8')
        self.assertEqual(app._dialog_start_dir('script'), '')
        app._remember_dialog_dir('script', str(self.a))
        self.assertEqual(app._dialog_start_dir('script'), str(self.a))

    def test_nonexistent_folder_is_not_recorded(self):
        app._remember_dialog_dir('script', str(self.root / 'missing'))
        self.assertFalse(self.file.exists())

    def test_export_dialog_remembers_own_directory_and_cancel_preserves_it(self):
        app._remember_dialog_dir('dir',str(self.a))
        handler=object.__new__(app.Handler);handler.send_json=Mock();requests=[]
        paths=[str(self.b)]
        def selected(*args,**kwargs):
            requests.append(json.loads(kwargs['input']))
            return SimpleNamespace(returncode=0,stdout=json.dumps({'path':paths[0] if paths else '', 'paths':paths}),stderr='')
        with patch.object(app.subprocess,'run',side_effect=selected):
            handler._dialog({'kind':'dir','purpose':'export','title':'选择字幕导出文件夹'})
            self.assertEqual(requests[-1]['initial_dir'],str(self.a))
            app._remember_dialog_dir('media',str(self.a))
            paths=[]
            handler._dialog({'kind':'dir','purpose':'export','title':'选择 MKV 导出文件夹'})
            self.assertEqual(requests[-1]['initial_dir'],str(self.b))
        saved=json.loads(self.file.read_text(encoding='utf8'))
        self.assertEqual(saved['export'],str(self.b));self.assertEqual(saved['dir'],str(self.a))
        self.assertEqual(app._dialog_start_dir('export'),str(self.b))
        self.assertEqual(handler.send_json.call_args.args[0]['paths'],[])


@unittest.skipUnless(os.name == 'nt', 'Windows 资源管理器')
class OpenFolderTests(unittest.TestCase):
    def test_file_is_revealed_with_explorer_select(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / '素材 一.mp4'
            target.write_bytes(b'x')
            with patch('subprocess.Popen') as popen:
                media.open_path(str(target))
            popen.assert_called_once_with(['explorer', '/select,', str(target)])

    def test_directory_is_opened_with_startfile(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('os.startfile', create=True) as startfile:
                media.open_path(folder)
            startfile.assert_called_once_with(folder)

    def test_missing_path_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                media.open_path(str(Path(folder) / '不存在.mp4'))


if __name__ == '__main__':
    unittest.main()
