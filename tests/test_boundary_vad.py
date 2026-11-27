"""VAD 二次校验：用确定的假概率验证“话起字幕起、话落字幕落”的保守收紧。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))
from core import audio_boundaries as boundaries
from core.audio_boundaries import (_vad_runs, _vad_region_ambiguous, _vad_start,
                                   _vad_end, refine_boundaries)


def curve(length, runs):
    """按帧生成概率：runs 为 [(起, 止, 概率)]，其余帧接近静音。"""
    probabilities=np.zeros(length)
    for low,high,value in runs:
        probabilities[low:high]=value
    return probabilities


class VadPrimitiveTests(unittest.TestCase):
    def test_compact_edge_threshold_is_stable_at_later_absolute_times(self):
        words=[{'word':'死','start':99.6,'end':99.92},
               {'word':'多','start':100.,'end':100.16}]
        self.assertTrue(boundaries._compact_edge_word(words))
        shifted=[{**w,'start':w['start']-99.,'end':w['end']-99.} for w in words]
        self.assertTrue(boundaries._compact_edge_word(shifted))
        words[0]['start']=99.59
        self.assertFalse(boundaries._compact_edge_word(words))

    def test_one_noise_run_cannot_confirm_edge_against_multiple_interior_words(self):
        words=[{'word':'你','start':.064,'end':.19},{'word':'们','start':.7,'end':.82},
               {'word':'回','start':.85,'end':1.},{'word':'家','start':1.02,'end':1.18}]
        frames=curve(50,[(2,6,.9),(21,39,.9)])
        self.assertTrue(boundaries._boundary_context_conflict(frames,0.,words))
        self.assertFalse(boundaries._context_supports_edge(.064,.192,words))
        self.assertTrue(boundaries._context_supports_edge(.672,1.248,words))
        mirror=[{**w,'start':2.-w['end'],'end':2.-w['start']} for w in reversed(words)]
        mirrored_frames=curve(65,[(23,41,.9),(57,61,.9)])
        self.assertTrue(boundaries._boundary_context_conflict(mirrored_frames,0.,mirror,at_end=True))

    def test_both_edges_can_trim_when_interior_words_support_same_speech_run(self):
        audio=np.zeros(16000*3)
        words=[{'word':'你好','start':0.,'end':.8},{'word':'世界','start':.8,'end':2.}]
        frames=curve(80,[(10,44,.9)])
        self.assertAlmostEqual(_vad_start(audio,16000,0.,2.,words,frames,0.),.30)
        self.assertAlmostEqual(_vad_end(audio,16000,0.,2.,words,frames,0.),1.428)

    def test_tail_without_speech_uses_preceding_word_as_context(self):
        words=[{'word':'你好','start':.1,'end':.7},{'word':'吧','start':1.1,'end':1.5}]
        frames=curve(60,[(3,23,.9)])
        self.assertTrue(boundaries._trailing_word_without_speech(frames,0.,words))
        self.assertFalse(boundaries._trailing_word_without_speech(np.zeros(60),0.,words))
        frames[36:40]=.32
        self.assertFalse(boundaries._trailing_word_without_speech(frames,0.,words))

    def test_tail_short_word_continuity_is_symmetric_with_first_word(self):
        words=[{'word':'回家','start':.1,'end':.6},{'word':'吧','start':.68,'end':.84}]
        self.assertTrue(boundaries._compact_edge_word(words,at_end=True))
        words[-1]['start']=1.;words[-1]['end']=1.16
        self.assertFalse(boundaries._compact_edge_word(words,at_end=True))

    def test_isolated_start_run_cannot_trim_without_following_word_context(self):
        words=[{'word':'你好','start':0.,'end':1.},{'word':'世界','start':1.5,'end':2.}]
        frames=curve(100,[(10,20,.9)])
        result=_vad_start(np.zeros(16000*3),16000,0.,2.5,words,frames,0.)
        self.assertIsNone(result)

    def test_isolated_tail_run_cannot_trim_without_preceding_word_context(self):
        words=[{'word':'你好','start':.1,'end':.6},{'word':'世界','start':1.,'end':2.8}]
        frames=curve(100,[(55,65,.9)])
        result=_vad_end(np.zeros(16000*4),16000,0.,3.2,words,frames,0.)
        self.assertIsNone(result)

    def test_leading_word_on_sound_effect_is_rejected_only_with_later_speech(self):
        words=[{'start':0.,'end':.3},{'start':.8,'end':1.4}]
        probabilities=curve(60,[(0,1,.2),(25,45,.9)])
        self.assertTrue(boundaries._leading_word_without_speech(probabilities,0.,words))
        self.assertFalse(boundaries._leading_word_without_speech(np.zeros(60),0.,words))
        self.assertFalse(boundaries._leading_word_without_speech(None,None,words))
        self.assertFalse(boundaries._leading_word_without_speech(probabilities,0.,words[:1]))

    def test_isolated_loud_vad_peak_does_not_certify_first_word_as_speech(self):
        words=[{'start':0.,'end':.4},{'start':.9,'end':1.5}]
        probabilities=curve(65,[(3,4,.99),(28,48,.9)])
        self.assertTrue(boundaries._leading_word_without_speech(probabilities,0.,words))
        # 一个真实但短促的首字仍要保护，不能将其单帧证据直接删掉。
        self.assertFalse(boundaries._leading_word_without_speech(probabilities,0.,
                         [{'start':.064,'end':.16},words[1]]))

    def test_sustained_quiet_or_voiced_prefix_is_not_treated_as_effect(self):
        words=[{'start':0.,'end':.4},{'start':.9,'end':1.5}]
        for probability in (.32,.95):
            with self.subTest(probability=probability):
                frames=curve(65,[(2,6,probability),(28,48,.9)])
                self.assertFalse(boundaries._leading_word_without_speech(frames,0.,words))

    def test_quiet_first_word_is_protected_and_spanning_first_word_can_be_trimmed(self):
        words=[{'start':0.,'end':.3},{'start':.8,'end':1.4}]
        probabilities=curve(60,[(2,8,.32),(25,45,.9)])
        self.assertFalse(boundaries._leading_word_without_speech(probabilities,0.,words))
        self.assertFalse(boundaries._leading_word_without_speech(curve(60,[(25,45,.9)]),0.,
                         [{'start':0.,'end':1.1},words[1]]))

    def test_runs_ignore_short_blips_and_bridge_short_dips(self):
        bridged=curve(40,[(5,15,.9),(17,18,.9)])
        self.assertEqual(_vad_runs(bridged),[(5,18)])
        blip=curve(40,[(1,3,.9),(20,30,.9)])
        runs=_vad_runs(blip)
        self.assertEqual(len(runs),1)
        self.assertEqual(runs[0][0],20)

    def test_isolated_peak_is_not_quiet_speech(self):
        probabilities=curve(60,[(0,1,.32),(5,6,.32),(10,11,.32),(30,60,.9)])
        self.assertFalse(_vad_region_ambiguous(probabilities,0,30))

    def test_sustained_medium_probability_blocks_trim(self):
        probabilities=curve(60,[(5,12,.32),(30,60,.9)])
        self.assertTrue(_vad_region_ambiguous(probabilities,0,30))

    def test_start_uses_speech_onset_after_music(self):
        sr=16000;audio=np.zeros(sr*3,dtype=np.float32)
        probabilities=curve(47,[(22,47,.9)])
        cue_words=[{'word':'你好','start':0.,'end':1.5}]
        with patch.object(boundaries,'_vad_probabilities',return_value=probabilities):
            result=_vad_start(audio,sr,0.,2.,cue_words)
        self.assertAlmostEqual(result,22*0.032-.02,places=3)

    def test_start_refuses_to_erase_first_word(self):
        sr=16000;audio=np.zeros(sr*3,dtype=np.float32)
        probabilities=curve(47,[(22,47,.9)])
        cue_words=[{'word':'你好','start':0.,'end':0.7}]
        with patch.object(boundaries,'_vad_probabilities',return_value=probabilities):
            self.assertIsNone(_vad_start(audio,sr,0.,2.,cue_words))

    def test_start_keeps_ambiguous_quiet_prefix(self):
        sr=16000;audio=np.zeros(sr*3,dtype=np.float32)
        probabilities=curve(47,[(5,13,.32),(25,47,.9)])
        cue_words=[{'word':'你好','start':0.,'end':1.5}]
        with patch.object(boundaries,'_vad_probabilities',return_value=probabilities):
            self.assertIsNone(_vad_start(audio,sr,0.,2.,cue_words))

    def test_later_ambiguous_run_does_not_cancel_valid_onset(self):
        sr=16000;audio=np.zeros(sr*4,dtype=np.float32)
        probabilities=curve(100,[(25,40,.9),(50,60,.32),(70,100,.9)])
        cue_words=[{'word':'你好世界','start':0.,'end':2.}]
        with patch.object(boundaries,'_vad_probabilities',return_value=probabilities):
            result=_vad_start(audio,sr,0.,3.2,cue_words)
        self.assertAlmostEqual(result,25*0.032-.02,places=3)

    def test_end_uses_speech_offset_before_noise(self):
        sr=16000;audio=np.zeros(sr*3,dtype=np.float32)
        probabilities=curve(72,[(0,20,.9)])
        cue_words=[{'word':'你好','start':0.,'end':1.}]
        with patch.object(boundaries,'_vad_probabilities',return_value=probabilities):
            result=_vad_end(audio,sr,0.,2.,cue_words)
        self.assertAlmostEqual(result,20*0.032+.02,places=3)

    def test_end_refuses_when_speech_reaches_boundary(self):
        sr=16000;audio=np.zeros(sr*3,dtype=np.float32)
        probabilities=np.full(72,.9)
        cue_words=[{'word':'你好','start':0.,'end':2.}]
        with patch.object(boundaries,'_vad_probabilities',return_value=probabilities):
            self.assertIsNone(_vad_end(audio,sr,0.,2.,cue_words))

    def test_end_caps_vad_overrun_after_last_word(self):
        sr=16000;audio=np.zeros(sr*4,dtype=np.float32)
        probabilities=curve(100,[(0,70,.9)])
        cue_words=[{'word':'你好','start':1.,'end':1.8}]
        with patch.object(boundaries,'_vad_probabilities',return_value=probabilities):
            result=_vad_end(audio,sr,0.,3.2,cue_words)
        self.assertAlmostEqual(result,1.95,places=3)

    def test_earlier_ambiguous_run_does_not_cancel_valid_end(self):
        sr=16000;audio=np.zeros(sr*4,dtype=np.float32)
        probabilities=curve(100,[(10,20,.9),(25,45,.9)])
        cue_words=[{'word':'你好','start':.5,'end':1.6}]
        with patch.object(boundaries,'_vad_probabilities',return_value=probabilities):
            result=_vad_end(audio,sr,0.,3.,cue_words)
        self.assertAlmostEqual(result,45*0.032+.02,places=3)


class VadIntegrationTests(unittest.TestCase):
    def test_real_pause_after_short_opening_keeps_words_for_pause_split(self):
        from core.segment import split_on_gaps
        cue={'text':'守住！援军赶来','start':0.,'end':1.84,'alignment':{'engine':'test'},
             'words':[{'word':t,'start':a,'end':b} for t,a,b in
                      [('守',0.,.16),('住',.16,.4),('援',1.2,1.36),('军',1.36,1.52),('赶',1.52,1.68),('来',1.68,1.84)]],
             'reference_match':{'status':'high','query_text':'守住！援军赶来','reference':{'text':'守住！援军赶来'}}}
        with patch.object(boundaries,'_vad_region',return_value=(0.,curve(100,[(6,20,.9),(38,59,.9)]))):
            result=self._refine(cue)[0]
        self.assertNotIn('alignment_error',result)
        self.assertIn('alignment_warning',result)
        self.assertEqual(result['words'],cue['words'])
        pieces,count=split_on_gaps([result],.3,16)
        self.assertEqual(count,1)
        self.assertEqual([c['text'] for c in pieces],['守住！','援军赶来'])
        self.assertEqual((pieces[0]['end'],pieces[1]['start']),(.4,1.2))

    def test_failed_neighbour_does_not_cut_successful_first_or_last_word(self):
        from core.alignment import _resolve_overlaps
        for at_end in [False,True]:
            with self.subTest(at_end=at_end):
                good={'text':'正常台词','start':1.,'end':2.,'alignment':{'engine':'test'},
                      'words':[{'word':'正常台词','start':1.,'end':2.}]}
                bad={'text':'定位失败','start':1.88 if at_end else 0.,'end':3. if at_end else 1.12,
                     'alignment_error':'无法定位','alignment':{'stale':True},'words':[]}
                cues=[good,bad] if at_end else [bad,good]
                threshold={'on':.1,'off':.05,'noise_floor':.001,'dead_zone':.001,'voice':.2}
                with patch.object(boundaries,'speech_intervals',return_value=([(0.,3.)],threshold)), \
                     patch.object(boundaries,'_vad_region',return_value=(None,None)):
                    result=refine_boundaries(deepcopy(cues),np.zeros(16000*4,dtype=np.float32))
                result=_resolve_overlaps(result,[(c['start'],c['end']) for c in cues])
                actual=result[0 if at_end else 1]
                self.assertEqual((actual['start'],actual['end']),(1.,2.))
                self.assertEqual(actual['words'],good['words'])
                self.assertNotIn('alignment_error',actual)

    def test_short_tail_word_missed_by_vad_keeps_words_and_marks_existing_warning(self):
        cue={'text':'回家吧','start':.1,'end':1.,'alignment':{'engine':'test'},
             'words':[{'word':'回家','start':.1,'end':.7},
                      {'word':'吧','start':.78,'end':1.}]}
        with patch.object(boundaries,'_vad_region',return_value=(0.,curve(60,[(3,21,.9)]))):
            result=self._refine(cue)[0]
        self.assertNotIn('alignment_error',result)
        self.assertIn('尾字',result['alignment_warning'])
        self.assertEqual(result['end'],1.)
        self.assertEqual(result['words'],cue['words'])

    def test_disconnected_tail_on_effect_is_marked_for_manual_review(self):
        cue={'text':'回家吧','start':.1,'end':1.5,'alignment':{'engine':'test'},
             'words':[{'word':'回家','start':.1,'end':.7},
                      {'word':'吧','start':1.1,'end':1.5}]}
        with patch.object(boundaries,'_vad_region',return_value=(0.,curve(60,[(3,23,.9)]))):
            result=self._refine(cue)[0]
        self.assertIn('尾字',result['alignment_error'])
        self.assertEqual(result['text'],cue['text'])
        self.assertTrue(result['needs_review'])

    def test_short_first_word_with_coherent_following_words_keeps_model_time(self):
        # 实测“不自量力”：VAD 漏掉 160ms 的“不”，不能退回更早的 51.14s。
        cue={'text':'不自量力','start':51.42,'end':52.22,'alignment':{'engine':'test'},
             'words':[{'word':'不','start':51.42,'end':51.58},
                      {'word':'自','start':51.66,'end':51.82},
                      {'word':'量','start':51.9,'end':51.98},
                      {'word':'力','start':52.14,'end':52.22}]}
        audio=np.zeros(16000*54,dtype=np.float32)
        probabilities=curve(50,[(20,45,.9)])
        with patch.object(boundaries,'_vad_region',return_value=(51.12,probabilities)):
            result=self._refine(cue,audio=audio)[0]
        self.assertNotIn('alignment_error',result)
        self.assertIn('alignment_warning',result)
        self.assertEqual((result['start'],result['end']),(51.42,52.22))
        self.assertEqual(result['words'],cue['words'])
        self.assertTrue(result['needs_review'])

    def test_first_word_on_non_speech_is_marked_without_guessing_or_losing_text(self):
        cue={'text':'你好世界','start':0.,'end':1.5,'alignment':{'engine':'test'},
             'words':[{'word':'你','start':0.,'end':.3},
                      {'word':'好世界','start':.8,'end':1.4}]}
        before=deepcopy(cue)
        with patch.object(boundaries,'_vad_region',return_value=(0.,curve(60,[(25,45,.9)]))):
            result=self._refine(cue)[0]
        self.assertIn('首字',result['alignment_error'])
        self.assertTrue(result['needs_review'])
        self.assertEqual(result['text'],before['text'])
        self.assertEqual(result['start'],before['start'])
        self.assertEqual(cue,before)

    def _cue(self):
        return {'start':0.,'end':2.,'text':'你好，走吧','words':[
            {'word':'你好','start':0.,'end':.9},{'word':'走吧','start':1.,'end':2.}]}

    def _refine(self,cue,audio=None,sr=16000):
        audio=np.zeros(sr*3,dtype=np.float32) if audio is None else audio
        threshold={'on':.1,'off':.05,'noise_floor':.001,'dead_zone':.001,'voice':.2}
        with patch.object(boundaries,'speech_intervals',return_value=([(0.,2.)],threshold)):
            return refine_boundaries([deepcopy(cue)],audio,sr)

    def test_vad_can_trim_beyond_rms_limit_and_clips_words(self):
        cue=self._cue()
        with patch.object(boundaries,'_vad_region',return_value=(0.,np.zeros(100))), \
             patch.object(boundaries,'_vad_start',return_value=.5), \
             patch.object(boundaries,'_vad_end',return_value=1.6):
            result=self._refine(cue)[0]
        self.assertEqual(result['start'],.5)
        self.assertEqual(result['end'],1.6)
        self.assertEqual(result['words'][0]['start'],.5)
        self.assertEqual(result['words'][-1]['end'],1.6)
        self.assertEqual(result['pause_after'],1.6)
        self.assertTrue(result['audio_refinement']['vad_trim']['applied_start'])
        self.assertTrue(result['audio_refinement']['vad_trim']['applied_end'])

    def test_missing_vad_keeps_original_boundaries(self):
        cue=self._cue()
        with patch.object(boundaries,'_vad_probabilities',return_value=None):
            result=self._refine(cue)[0]
        self.assertEqual((result['start'],result['end']),(0.,2.))
        self.assertNotIn('vad_trim',result['audio_refinement'])

    def test_vad_end_before_last_word_start_is_rejected(self):
        cue=self._cue()
        with patch.object(boundaries,'_vad_region',return_value=(0.,np.zeros(100))), \
             patch.object(boundaries,'_vad_start',return_value=None), \
             patch.object(boundaries,'_vad_end',return_value=.5):
            result=self._refine(cue)[0]
        self.assertEqual(result['end'],2.)


if __name__=='__main__':
    unittest.main()
