"""全流程复查：数字句界、人工参考生命周期和外部引擎音频分块。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core.reference_match import normalized, match_reference, parse_document
from core.segment import segment_midpoint, split_on_gaps, merge_cues
from core.external_asr import speech_chunks
from core.validation import cues as validate_cues
from unittest.mock import patch
from tempfile import TemporaryDirectory
from core import model_manager


class ReferenceLifecycleTests(unittest.TestCase):
    def test_numbers_do_not_merge_across_punctuation_whitespace_or_lines(self):
        for separator in ('，', '。', '\n', ' '):
            text = '十'+separator+'二'
            clean, positions = normalized(text)
            self.assertEqual(clean, '102')
            self.assertEqual(positions, [(0,1),(0,1),(2,3)])
        self.assertEqual(normalized('十二')[0], '12')
        self.assertEqual(normalized('三点五')[0], '35')
        self.assertEqual(normalized('三，点五')[0], '3点5')

    def test_numeric_sentences_can_be_matched_separately_without_reusing_raw_clusters(self):
        source = parse_document('十，二。')
        cues = [{'text':'十','start':0.,'end':1.}, {'text':'二','start':1.,'end':2.}]
        result = match_reference(cues, source)['cues']
        self.assertEqual([c['script_text'] for c in result], ['十，','二。'])

    def test_manual_reference_survives_midpoint_and_pause_split_without_duplication(self):
        cue = {'text':'我们今天回家明天一起出发','start':0.,'end':4.,
               'manual_reference':'我们今天回家，明天一起出发！','words':[
                   {'word':'我们今天回家','start':0.,'end':1.},
                   {'word':'明天一起出发','start':2.,'end':4.}]}
        before = deepcopy(cue)
        for parts, count in (segment_midpoint(cue,10), split_on_gaps([cue],.6)):
            self.assertGreater(count,0)
            self.assertEqual(''.join(p['manual_reference'] for p in parts),cue['manual_reference'])
            self.assertTrue(all(p['manual_reference']!=cue['manual_reference'] for p in parts))
        self.assertEqual(cue,before)

    def test_split_inside_normalized_number_never_raises_or_duplicates_manual_text(self):
        from core.reference_match import inherit_manual_reference, inherit_reference
        parent={'text':'十二个人回家','manual_reference':'十二个人回家。'}
        pieces=[{'text':'十'},{'text':'二个人回家'}]
        self.assertTrue(inherit_manual_reference(parent,pieces))
        self.assertEqual(''.join(p['manual_reference'] for p in pieces),parent['manual_reference'])
        parent['reference_match']={'status':'high','reference':{'text':'十二个人回家。','start':0,'end':7}}
        self.assertFalse(inherit_reference(parent,pieces))

    def test_manual_reference_type_is_checked_before_save(self):
        with self.assertRaisesRegex(ValueError,'manual_reference'):
            validate_cues([{'text':'字幕','start':0,'end':1,'manual_reference':{}}])

    def test_backend_merge_keeps_both_manual_reference_pieces(self):
        items=[{'text':'我们回家','manual_reference':'我们回家，','start':0,'end':1},
               {'text':'明天再见','manual_reference':'明天再见！','start':1,'end':2}]
        before=deepcopy(items);merged,count=merge_cues(items)
        self.assertEqual(count,1)
        self.assertEqual(merged[0]['manual_reference'],'我们回家，明天再见！')
        self.assertEqual(items,before)

    def test_adopted_manual_reference_punctuation_splits_even_with_pause_disabled(self):
        cue={'text':'我们回家明天再见','manual_reference':'我们回家，明天再见！',
             'start':0.,'end':2.,'words':[
                 {'word':'我们回家','start':0.,'end':1.}, {'word':'明天再见','start':1.,'end':2.}]}
        parts,count=split_on_gaps([cue],0)
        self.assertEqual(count,1)
        self.assertEqual([p['text'] for p in parts],['我们回家','明天再见'])
        self.assertEqual([p['manual_reference'] for p in parts],['我们回家，','明天再见！'])
        cue['manual_reference']='完全不相关，不能自动拆。'
        self.assertEqual(split_on_gaps([cue],0)[1],0)


class SpeechChunkTests(unittest.TestCase):
    def test_cut_prefers_nearby_quiet_and_covers_every_sample(self):
        samples=np.ones(61000,dtype=np.float32)*.2
        samples[23800:24200]=0
        chunks=speech_chunks(samples,[{'start':0,'end':len(samples)}],1000)
        self.assertTrue(23800<=chunks[0][1]<=24200)
        self.assertEqual(chunks[0][0],0)
        self.assertEqual(chunks[-1][1],len(samples))
        self.assertTrue(all(a[1]==b[0] for a,b in zip(chunks,chunks[1:])))
        self.assertTrue(all(0<b-a<=25000 for a,b in chunks))

    def test_constant_noise_keeps_hard_limit_and_vad_gaps_remain_excluded(self):
        samples=np.ones(65000,dtype=np.float32)*.1
        spans=[{'start':1000,'end':28000},{'start':40000,'end':65000}]
        self.assertEqual(speech_chunks(samples,spans,1000),[(1000,26000),(26000,28000),(40000,65000)])
        self.assertEqual(speech_chunks(samples,[],1000),[])

    def test_cancel_is_checked_while_planning(self):
        def cancel(): raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError,'cancelled'):
            speech_chunks(np.zeros(26000),[{'start':0,'end':26000}],1000,check=cancel)


class RuntimeReadinessTests(unittest.TestCase):
    def test_valid_json_with_wrong_configuration_types_falls_back_safely(self):
        with TemporaryDirectory() as folder:
            recipe=Path(folder)/'engines.json'
            with patch.object(model_manager,'ENGINE_CONFIG_FILE',recipe):
                for text in ('[]','{"engines": ["qwen"]}','{"engines": "bad"}'):
                    recipe.write_text(text,encoding='utf-8')
                    self.assertIn('qwen',model_manager.engine_config()['engines'])

    def test_online_firered_recipe_tracks_pinned_revision_and_cache_updates(self):
        import json
        with TemporaryDirectory() as folder:
            recipe=Path(folder)/'engines.json';pin=Path(folder)/'firered.json'
            recipe.write_text('{}',encoding='utf-8')
            pin.write_text(json.dumps({'revision':'a'*40}),encoding='utf-8')
            with patch.object(model_manager,'ENGINE_CONFIG_FILE',recipe), \
                 patch.object(model_manager,'FIRERED_CONFIG_FILE',pin):
                packages=model_manager.engine_spec('firered')['packages']
                self.assertIn('/'+('a'*40)+'.zip',packages[0])
                pin.write_text(json.dumps({'revision':'b'*40})+' ',encoding='utf-8')
                packages=model_manager.engine_spec('firered')['packages']
                self.assertIn('/'+('b'*40)+'.zip',packages[0])

    def test_damaged_or_wrong_engine_marker_is_not_reported_ready(self):
        from core import bundled_runtime
        with TemporaryDirectory() as folder:
            location=Path(folder)/'qwen';location.mkdir()
            interpreter=location/'python.exe';interpreter.touch()
            marker=location/'ready.json'
            with patch.object(model_manager,'ENGINE_DIR',Path(folder)), \
                 patch.object(model_manager,'runtime_python',return_value=interpreter), \
                 patch.object(bundled_runtime,'manifest',return_value={}), \
                 patch.object(bundled_runtime,'python_for',return_value=None), \
                 patch.object(bundled_runtime,'stale_venv_home',return_value=False):
                for text in ('bad json','{}','{"engine":"firered"}'):
                    marker.write_text(text,encoding='utf-8')
                    self.assertFalse(model_manager.runtime_ready('qwen'))


if __name__ == '__main__':
    unittest.main()
