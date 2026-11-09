"""模型输入窗口的声学切点；不运行模型、不改写用户工程。"""
from copy import deepcopy
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import wave
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from core.alignment import _alignment_windows
from core import alignment
from core.asr import Cancelled


class AlignmentWindowTests(unittest.TestCase):
    rate=1000

    def plan(self,cues,audio,**kwargs):
        return _alignment_windows(cues,audio,self.rate,**kwargs)

    def test_edges_use_quiet_and_keep_complete_cue_coverage(self):
        audio=np.zeros(6000,dtype=np.float32);audio[2000:4000]=.1
        cues=[{'start':2.,'end':4.,'text':'完整短句'}]
        before=audio.copy();original=deepcopy(cues)
        window=self.plan(cues,audio)[0]
        self.assertEqual((window['start_boundary'],window['end_boundary']),('quiet','quiet'))
        self.assertLessEqual(window['start'],2.)
        self.assertGreaterEqual(window['end'],4.)
        for t in (window['start'],window['end']):
            self.assertTrue(np.all(audio[round((t-.039)*1000):round((t+.039)*1000)]==0))
        np.testing.assert_array_equal(audio,before);self.assertEqual(cues,original)

    def test_constant_background_does_not_become_false_quiet(self):
        cues=[{'start':2.,'end':4.,'text':'对白'}]
        window=self.plan(cues,np.full(6000,.001,dtype=np.float32))[0]
        self.assertEqual((window['start'],window['end']),(0.,6.))
        self.assertEqual((window['start_boundary'],window['end_boundary']),('context','context'))

    def test_brief_energy_dip_is_not_a_cut(self):
        audio=np.full(6000,.1,dtype=np.float32);audio[1700:1740]=0
        window=self.plan([{'start':2.,'end':4.}],audio)[0]
        self.assertEqual(window['start_boundary'],'context')

    def test_near_capacity_prefers_a_complete_cue_gap(self):
        cues=[{'start':i,'end':i+.8,'text':str(i)} for i in range(10)]
        audio=np.full(12000,.1,dtype=np.float32);audio[3800:4000]=0
        windows=self.plan(cues,audio,max_span=5.)
        self.assertEqual(windows[0]['indices'],[0,1,2,3])
        self.assertEqual(windows[0]['end_boundary'],'quiet')
        self.assertEqual(windows[1]['start_boundary'],'quiet')
        self.assertEqual([i for w in windows for i in w['indices']],list(range(10)))
        for w in windows:
            self.assertLessEqual(max(cues[i]['end'] for i in w['indices'])-cues[w['indices'][0]]['start'],5.)

    def test_continuous_speech_keeps_context_overlap(self):
        cues=[{'start':i,'end':i+.8} for i in range(12)]
        windows=self.plan(cues,np.full(14000,.1,dtype=np.float32),max_span=5.)
        self.assertEqual([len(w['indices']) for w in windows],[5,5,2])
        self.assertGreater(windows[0]['end'],windows[1]['start'])
        self.assertTrue(all(w['start_boundary']==w['end_boundary']=='context' for w in windows))
        self.assertEqual([i for w in windows for i in w['indices']],list(range(12)))

    def test_nested_cue_end_does_not_allow_an_oversized_group(self):
        cues=[{'start':0,'end':4.8},{'start':1,'end':2},{'start':4,'end':5.8}]
        windows=self.plan(cues,np.zeros(8000,dtype=np.float32),max_span=5.)
        self.assertEqual([w['indices'] for w in windows],[[0,1],[2]])
        self.assertGreaterEqual(windows[0]['end'],4.8)

    def test_cancel_and_oversized_single_cue(self):
        event=threading.Event();event.set()
        with self.assertRaises(Cancelled):
            self.plan([{'start':0,'end':1}],np.zeros(2000),cancel_event=event)
        with self.assertRaisesRegex(ValueError,'单条字幕过长'):
            self.plan([{'start':0,'end':6}],np.zeros(8000),max_span=5.)

    def test_media_edges_empty_input_and_outside_media(self):
        audio=np.zeros(3000,dtype=np.float32)
        self.assertEqual(self.plan([],audio),[])
        w=self.plan([{'start':0,'end':3}],audio)[0]
        self.assertEqual((w['start'],w['end']),(0.,3.))
        with self.assertRaisesRegex(ValueError,'字幕时间超出音轨'):
            self.plan([{'start':8,'end':9}],audio)

    def test_runner_receives_quiet_windows_once_and_offsets_words_back(self):
        cues=[{'text':'前一句','start':2.,'end':122.},
              {'text':'后一句','start':124.,'end':244.}]
        audio=np.zeros(246*16000,dtype=np.float32)
        received=[]
        def run(argv,**kwargs):
            request=json.loads(Path(argv[2]).read_text(encoding='utf8'))
            output=[]
            for chunk in request['chunks']:
                received.append(chunk)
                with wave.open(chunk['path'],'rb') as wav:
                    self.assertEqual(wav.getframerate(),16000)
                    self.assertEqual(wav.getnchannels(),1)
                    self.assertGreater(wav.getnframes(),0)
                words=[]
                for i in chunk['indices']:
                    words.append({'text':cues[i]['text'],'start':cues[i]['start']-chunk['offset'],
                                  'end':cues[i]['end']-chunk['offset']})
                self.assertEqual(chunk['text'],'\n'.join(cues[i]['text'] for i in chunk['indices']))
                output.append(words)
            Path(argv[3]).write_text(json.dumps({'aligned_words':output}),encoding='utf8')
            return Mock(returncode=0,poll=Mock(return_value=0))
        with tempfile.TemporaryDirectory() as folder,patch.object(alignment,'RESULTS_DIR',Path(folder)), \
             patch.object(alignment,'weights_present',return_value=True), \
             patch.object(alignment,'ensure_runtime'),patch.object(alignment,'runtime_python',return_value=sys.executable), \
             patch.object(alignment,'model_directory',return_value=Path(folder)), \
             patch('core.media.decode_audio',return_value=audio), \
             patch('core.audio_boundaries.refine_boundaries',side_effect=lambda items,*args,**kwargs:items), \
             patch('core.audio_boundaries.cap_durations',return_value=[]), \
             patch.object(alignment.subprocess,'Popen',side_effect=run) as process:
            result,info=alignment.align_cues('fixture.wav',cues,'cpu')
        self.assertEqual(process.call_count,1)
        self.assertEqual(info['windowing']['count'],2)
        self.assertEqual(len(received),2)
        self.assertEqual([(c['start'],c['end']) for c in result],[(2.,122.),(124.,244.)])
        self.assertTrue(all(c['words'] for c in result))
        self.assertEqual(cues[0],{'text':'前一句','start':2.,'end':122.})


if __name__=='__main__':unittest.main()
