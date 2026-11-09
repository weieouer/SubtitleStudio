"""实际反馈的尾字倒置与对齐拉长：原文保留，坏词表不能阻断校验/保存。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import app
from core import alignment, validation
from core.audio_boundaries import refine_boundaries, synchronize_word_times


class WordTimeConsistencyTests(unittest.TestCase):
    def damaged(self):
        return {'text':'录音在我这里','start':182.26,'end':182.88,
                'words':[{'word':'里','start':182.9,'end':182.88}],
                'alignment':{'engine':'test'},'pause_after':182.88}

    def test_exact_reported_reversed_tail_does_not_erase_text(self):
        cue=self.damaged();synchronize_word_times(cue)
        self.assertEqual(cue['text'],'录音在我这里');self.assertEqual(cue['words'],[])
        self.assertEqual((cue['start'],cue['end']),(182.26,182.88))
        self.assertTrue(cue['alignment']['stale']);self.assertTrue(cue['needs_review'])
        self.assertTrue(cue['word_timing_error']);self.assertNotIn('pause_after',cue)
        validation.cues([cue])

    def test_refinement_neighbor_clamp_cannot_return_negative_word_duration(self):
        sr=16000;audio=np.zeros(sr*5,dtype=np.float32)
        audio[sr*2:sr*4]=.1*np.sin(2*np.pi*220*np.arange(sr*2)/sr)
        cues=[{'text':'最后','start':2.,'end':3.,'words':[
            {'word':'最','start':2.,'end':2.96},{'word':'后','start':2.96,'end':3.}]},
            {'text':'下句','start':2.88,'end':4.,'words':[]}]
        before=deepcopy(cues)
        refined=refine_boundaries(cues,audio)
        validation.cues(refined)
        self.assertTrue(refined[0]['word_timing_error']);self.assertEqual(cues,before)

    def test_partial_tail_clip_keeps_all_words_with_positive_lengths(self):
        cue={'text':'你好','start':1.1,'end':1.9,'words':[
            {'word':'你','start':1.,'end':1.5},{'word':'好','start':1.5,'end':2.}]}
        synchronize_word_times(cue)
        self.assertEqual(''.join(w['word'] for w in cue['words']),'你好')
        self.assertEqual((cue['words'][0]['start'],cue['words'][-1]['end']),(1.1,1.9))
        self.assertTrue(all(w['end']>w['start'] for w in cue['words']))

    def test_repair_is_explicit_and_malformed_values_still_rejected(self):
        with self.assertRaisesRegex(ValueError,'第1条字幕第1词'):
            validation.cues([self.damaged()])
        cue=self.damaged();validation.cues([cue],repair_word_times=True)
        self.assertEqual(cue['words'],[]);self.assertTrue(cue['needs_review'])
        for bad in (float('nan'),-1,True,'bad'):
            cue=self.damaged();cue['words'][0]['start']=bad
            with self.assertRaises(ValueError):validation.cues([cue],repair_word_times=True)
        cue=self.damaged();cue['words'][0]['word']=None
        with self.assertRaises(ValueError):validation.cues([cue],repair_word_times=True)

    def test_matching_still_returns_reference_for_damaged_optional_word_times(self):
        handler=object.__new__(app.Handler);handler.send_json=Mock()
        handler._match_script({'cues':[self.damaged()],'document':{'text':'录音在我这里'},'lines':[]})
        result=handler.send_json.call_args.args[0]
        self.assertEqual(result['cues'][0]['script_text'],'录音在我这里')
        self.assertEqual(result['cues'][0]['reference_match']['status'],'high')
        self.assertTrue(result['cues'][0]['word_timing_error'])

    def test_exact_long_alignment_report_keeps_input_time_and_text(self):
        cue={'text':'先留着','start':61.35,'end':62.11,'id':28}
        words=[{'text':'先','start':50.18,'end':58.42},
               {'text':'留','start':61.78,'end':61.94},{'text':'着','start':61.94,'end':62.02}]
        result=alignment.apply_word_times([cue],[0],words)[0]
        self.assertEqual((result['start'],result['end']),(61.35,62.11))
        self.assertEqual(result['text'],'先留着');self.assertEqual(result['words'],[])
        self.assertIn('异常拉长',result['alignment_error'])

    def test_successful_realignment_clears_old_word_timing_error(self):
        cue=self.damaged();synchronize_word_times(cue)
        words=[{'text':'录音在我这里','start':182.26,'end':183.06}]
        result=alignment.apply_word_times([cue],[0],words)[0]
        self.assertNotIn('word_timing_error',result);self.assertNotIn('alignment_error',result)
        self.assertTrue(result['words']);validation.cues([result])


if __name__=='__main__':unittest.main()
