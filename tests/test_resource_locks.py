"""长下载不阻塞其他工程保存；同文件等待仍可取消。"""
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from core.storage import locked_file,atomic_json


class ResourceLockTests(unittest.TestCase):
    def test_unrelated_save_proceeds_while_download_holds_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);held=threading.Event();release=threading.Event();saved=threading.Event();errors=[]
            def download():
                with locked_file(root/'model.bin'):
                    held.set();release.wait(3)
            def save():
                try:
                    with locked_file(root/'project.json'):atomic_json(root/'project.json',{'revision':1})
                    saved.set()
                except Exception as error:errors.append(error)
            first=threading.Thread(target=download);first.start();self.assertTrue(held.wait(2))
            second=threading.Thread(target=save);second.start()
            try:self.assertTrue(saved.wait(1),'无关工程保存不能等待模型下载锁')
            finally:release.set();first.join(3);second.join(3)
            self.assertFalse(errors)

    def test_same_resource_wait_can_cancel_inside_process(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'model.bin';held=threading.Event();release=threading.Event();stopped=threading.Event()
            def hold():
                with locked_file(path):held.set();release.wait(3)
            worker=threading.Thread(target=hold);worker.start();self.assertTrue(held.wait(2))
            timer=threading.Timer(.1,stopped.set);timer.start()
            def check():
                if stopped.is_set():raise RuntimeError('cancelled')
            try:
                with self.assertRaisesRegex(RuntimeError,'cancelled'):
                    with locked_file(path,timeout=2,cancelled=check):self.fail('不能提前得到同一文件的锁')
            finally:release.set();timer.join(2);worker.join(3)


if __name__=='__main__':unittest.main()
