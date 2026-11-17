"""SRT文本/毫秒精度及旧工程初始词表的边界回归，全部使用临时文件。"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import app
from core import srt_io


class SrtRoundtripEdgeTests(unittest.TestCase):
    def test_blank_lines_inside_cue_do_not_drop_following_text_on_reimport(self):
        cue={'text':'第一行\r\n  \r\n第二行\n\n第三行','start':.1,'end':1.}
        before=deepcopy(cue)
        parsed,warnings=srt_io.parse_srt_text(srt_io.format_srt([cue]))
        self.assertEqual([c['text'] for c in parsed],['第一行\n第二行\n第三行'])
        self.assertEqual(warnings,[])
        self.assertEqual(cue,before)

    def test_half_millisecond_rounding_never_formats_zero_duration(self):
        parsed,_=srt_io.parse_srt_text(srt_io.format_srt([{'text':'字幕','start':.0015,'end':.0025}]))
        self.assertGreater(parsed[0]['end'],parsed[0]['start'])

    def test_one_millisecond_overlap_is_rejected(self):
        cues=[{'text':'前句','start':0.,'end':1.001},{'text':'后句','start':1.,'end':2.}]
        self.assertTrue(srt_io.validate_cues(cues))

    def test_interval_collapsing_at_srt_precision_is_reported_before_export(self):
        self.assertTrue(srt_io.validate_cues([{'text':'字幕','start':.0015,'end':.0025}]))


class InitialCueRecoveryTests(unittest.TestCase):
    def initial(self):
        return {'text':'初始字幕','start':1.,'end':2.,
                'asr_original':{'text':'最初识别','start':1.,'end':2.},
                'words':[{'word':'初始字幕','start':1.8,'end':1.7}]}

    def test_bad_optional_initial_word_times_do_not_block_saving_correct_current_cues(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source.srt';source.write_text('source',encoding='utf-8')
            handler=object.__new__(app.Handler);handler.send_json=Mock()
            with patch.object(app,'PROJECT_DIR',root/'projects'):
                handler._save_project({'source_path':str(source),'cues':[{'text':'已校对','start':1.,'end':2.}],
                                       'initial_cues':[self.initial()],'expected_revision':0})
                result=handler.send_json.call_args.args[0]
                saved=json.loads(Path(result['path']).read_text(encoding='utf-8'))
                self.assertEqual(saved['cues'][0]['text'],'已校对')
                initial=saved['initial_cues'][0]
                self.assertEqual(initial['text'],'初始字幕')
                self.assertEqual(initial['asr_original']['text'],'最初识别')
                self.assertEqual(initial['words'],[])
                self.assertTrue(initial['word_timing_error'])

    def test_malformed_initial_word_times_are_still_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source.srt';source.write_text('source',encoding='utf-8')
            handler=object.__new__(app.Handler);handler.send_json=Mock()
            initial=self.initial();initial['words'][0]['start']=float('nan')
            with patch.object(app,'PROJECT_DIR',root/'projects'),self.assertRaises(ValueError):
                handler._save_project({'source_path':str(source),'cues':[],'initial_cues':[initial]})


if __name__=='__main__':
    unittest.main()
