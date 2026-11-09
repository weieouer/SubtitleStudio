"""任务栏尺寸随 DPI 变化，图标资源能由 Windows 直接加载。"""
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from core import window_icon


class IconResourceTests(unittest.TestCase):
    def test_source_relaunch_uses_python_and_portable_relaunch_keeps_bat(self):
        source = window_icon.relaunch_command(ROOT / 'main.py')
        self.assertIn('python', source.lower())
        self.assertIn('main.py', source)
        self.assertIn('--background', source)
        portable = window_icon.relaunch_command(ROOT / 'run.bat')
        self.assertIn('/c', portable)
        self.assertIn('run.bat', portable)

    def test_ico_contains_native_frames_for_common_display_scales(self):
        data=(ROOT/'frontend/assets/studio-film-taskbar.ico').read_bytes()
        self.assertEqual(struct.unpack_from('<HH',data),(0,1))
        sizes=set()
        for i in range(struct.unpack_from('<H',data,4)[0]):
            width,height,_,_,_,depth,length,offset=struct.unpack_from('<BBBBHHII',data,6+16*i)
            size=width or 256;sizes.add(size)
            self.assertEqual(height or 256,size);self.assertEqual(depth,32)
            png=data[offset:offset+length]
            self.assertEqual(png[:8],b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack_from('>II',png,16),(size,size))
        self.assertTrue({16,20,24,28,32,40,48,56,64,96,256} <= sizes)

    @unittest.skipUnless(os.name=='nt','Windows DPI API')
    def test_window_dpi_selects_matching_small_and_taskbar_sizes(self):
        user=MagicMock()
        for dpi,expected in [(96,(16,32)),(120,(20,40)),(144,(24,48)),(168,(28,56)),(192,(32,64))]:
            user.GetDpiForWindow.return_value=dpi
            user.GetSystemMetricsForDpi.side_effect=lambda index,dpi:round((16 if index==49 else 32)*dpi/96)
            with patch.object(c.windll,'user32',user):
                self.assertEqual(window_icon.window_icon_sizes(123),expected)

    @unittest.skipUnless(os.name=='nt','Windows HICON loader')
    def test_windows_loads_all_common_icon_sizes_without_a_gui(self):
        user=c.windll.user32
        user.LoadImageW.argtypes=[w.HINSTANCE,w.LPCWSTR,w.UINT,c.c_int,c.c_int,w.UINT]
        user.LoadImageW.restype=w.HANDLE
        user.DestroyIcon.argtypes=[w.HANDLE]
        path=str(ROOT/'frontend/assets/studio-film-taskbar.ico')
        for size in (16,20,24,28,32,40,48,56,64,96):
            handle=user.LoadImageW(None,path,1,size,size,0x10)
            self.assertTrue(handle,f'Windows 无法加载 {size}px 图标')
            user.DestroyIcon(handle)


if __name__=='__main__':
    unittest.main()
