"""PCM缓存复用、局部切片、文件变更失效与容量限制。"""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from core import media

class AudioCacheTests(unittest.TestCase):
    def test_full_decode_reused_for_alignment_and_region_without_mutating_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.wav';source.write_bytes(b'source')
            samples=np.arange(16000,dtype=np.float32)
            with patch.object(media,'AUDIO_CACHE_DIR',root/'cache'),patch.object(media,'_decode_audio',return_value=samples) as decode:
                first=media.decode_audio(str(source))
                second=media.decode_audio(str(source))
                region=media.decode_audio(str(source),start=.25,end=.75)
                self.assertEqual(decode.call_count,1)
                np.testing.assert_array_equal(first,second)
                np.testing.assert_array_equal(region,samples[4000:12000])
                with self.assertRaises(ValueError):second[0]=99
                del second,region  # Windows不能删除仍被映射的测试缓存。

    def test_source_change_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.wav';source.write_bytes(b'a')
            with patch.object(media,'AUDIO_CACHE_DIR',root/'cache'),patch.object(media,'_decode_audio',return_value=np.zeros(100,dtype=np.float32)) as decode:
                media.decode_audio(str(source));source.write_bytes(b'changed')
                media.decode_audio(str(source));self.assertEqual(decode.call_count,2)

    def test_cache_is_bounded_and_cancellation_does_not_decode(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'one.wav';source.write_bytes(b'a');other=root/'two.wav';other.write_bytes(b'b')
            with patch.object(media,'AUDIO_CACHE_DIR',root/'cache'),patch.object(media,'AUDIO_CACHE_ENTRIES',1), \
                 patch.object(media,'_decode_audio',return_value=np.zeros(100,dtype=np.float32)) as decode:
                media.decode_audio(str(source));media.decode_audio(str(other))
                self.assertEqual(len(list((root/'cache').glob('*.npy'))),1)
                event=threading.Event();event.set()
                with self.assertRaises(media.DecodeCancelled):media.decode_audio(str(source),cancel_event=event)
                self.assertEqual(decode.call_count,2)

if __name__=='__main__':unittest.main()
