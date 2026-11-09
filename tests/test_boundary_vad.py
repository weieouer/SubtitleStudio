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
