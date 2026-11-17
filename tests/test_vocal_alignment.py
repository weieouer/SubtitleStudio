"""音效复查的保守采纳、失败回退和可选权重契约；不联网或加载大模型。"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))
from core import alignment, model_manager as models
from core.asr import Cancelled
from core.vocal_alignment import confirm_vocal_candidate, retry_windows, raw_cue_words
from core.vocal_separation import MODEL_NAME, MODEL_FILE, MODEL_SIZE, MODEL_URL, write_wave


class VocalFixture:
    def setUp(self):
        self.original = [{'text':t, 'start':s, 'end':e} for t,s,e in
                         [('不',.2,1.),('自',1.,1.2),('量',1.2,1.4),('力',1.4,1.6)]]
        self.candidate = deepcopy(self.original);self.candidate[0]['start']=.8
        self.mixture = np.zeros(32000,dtype=np.float32)
        self.mixture[3200:12800]=.4;self.mixture[12800:25600]=.1
        self.vocals = np.zeros_like(self.mixture);self.vocals[12800:25600]=.08

    def confirm(self,original=None,candidate=None,vocals=None):
        return confirm_vocal_candidate('不自量力',original or self.original,candidate or self.candidate,
                                       self.mixture,self.vocals if vocals is None else vocals,0,1.4)


class VocalCandidateTests(VocalFixture, unittest.TestCase):
    def test_removed_effect_and_three_stable_words_allow_start_correction(self):
        before=deepcopy(self.original)
        self.assertTrue(self.confirm());self.assertEqual(self.original,before)

    def test_tail_effect_uses_previous_words_as_anchors(self):
        original=deepcopy(self.candidate);original[-1]['end']=1.95
        self.mixture[25600:31200]=.4
        self.assertTrue(self.confirm(original=original))

    def test_lost_quiet_speech_or_no_voice_does_not_authorize_cut(self):
        self.assertFalse(self.confirm(vocals=self.mixture*.8))
        self.assertFalse(self.confirm(vocals=np.zeros_like(self.vocals)))
        vocals=self.vocals.copy();vocals[12800:16000]=0
        self.assertFalse(self.confirm(vocals=vocals))

    def test_whole_sentence_shift_text_mismatch_and_stretched_words_rejected(self):
        shifted=[{**w,'start':w['start']+.3,'end':w['end']+.3} for w in self.candidate]
        self.assertFalse(self.confirm(candidate=shifted))
        mismatch=deepcopy(self.candidate);mismatch[0]['text']='还'
        self.assertFalse(self.confirm(candidate=mismatch))
        invalid=deepcopy(self.candidate);invalid[0]['end']=float('nan')
        self.assertFalse(self.confirm(candidate=invalid))

    def test_short_command_or_one_stable_anchor_is_insufficient(self):
        candidate=deepcopy(self.candidate)
        candidate[1]['start']+=.13;candidate[2]['start']+=.13
        self.assertFalse(self.confirm(candidate=candidate))
        self.assertEqual(retry_windows([{'text':'陛下','alignment_warning':'需复核'}],[(.2,1.6)],
                                      [self.original[:2]],2),[])

    def test_only_existing_uncertainty_is_retried_and_clip_is_bounded(self):
        cue={'text':'不自量力','start':.2,'end':1.6}
        self.assertEqual(retry_windows([cue],[(.2,1.6)],[self.original],2),[])
        cue['alignment_warning']='首词语音不明确'
        window=retry_windows([cue],[(.2,1.6)],[self.original],2)[0]
        self.assertEqual((window['offset'],window['end']),(0.,2.))
        self.assertEqual(retry_windows([cue],[(.2,40.)],[self.original],50),[])

    def test_raw_words_preserve_offset_and_do_not_invent_mapping(self):
        chunks=[{'indices':[1],'cues':[{'text':'不自量力'}],'offset':10}]
        words=raw_cue_words(chunks,[self.original],2)
        self.assertEqual(words[0],[]);self.assertAlmostEqual(words[1][0]['start'],10.2)
        chunks[0]['cues'][0]['text']='另一句话'
        self.assertEqual(raw_cue_words(chunks,[self.original],2),[[],[]])


class VocalPipelineTests(VocalFixture, unittest.TestCase):
    def run_pipeline(self,folder,mode='accept',cancel=None,helper=True):
        cues=[{'id':1,'text':'不自量力','start':.2,'end':1.6}]
        calls=[]
        def worker(argv,**kwargs):
            request=json.loads(Path(argv[2]).read_text(encoding='utf8'));calls.append(request)
            if request['engine']=='aligner':
                chunk=request['chunks'][0]
                data={'aligned_words':[[{**w,'start':w['start']-chunk['offset'],'end':w['end']-chunk['offset']} for w in self.original]]}
            else:
                if mode=='failure':raise RuntimeError('辅助环境异常')
                if cancel is not None:cancel.set()
                entries=[]
                for chunk in request['chunks']:
                    path=Path(folder)/'vocal.wav';write_wave(path,self.vocals)
                    candidate=self.candidate if mode!='conflict' else self.original
                    entries.append({'path':str(path),'words':candidate})
                data={'vocal_results':entries}
            Path(argv[3]).write_text(json.dumps(data),encoding='utf8')
            return Mock(returncode=0,poll=Mock(return_value=0))
        def refine(items,*args,**kwargs):
            for item in items:
                if not (item.get('alignment') or {}).get('vocal_assisted'):
                    item['alignment_warning']='句首音效待复核'
            return items
        with patch.object(alignment,'RESULTS_DIR',Path(folder)), \
             patch.object(alignment,'weights_present',side_effect=lambda name:name!=MODEL_NAME or helper), \
             patch.object(alignment,'ensure_runtime'),patch.object(alignment,'runtime_python',return_value=sys.executable), \
             patch.object(alignment,'model_directory',return_value=Path(folder)), \
             patch('core.media.decode_audio',return_value=self.mixture), \
             patch('core.audio_boundaries.refine_boundaries',side_effect=refine), \
             patch('core.audio_boundaries.cap_durations',return_value=[]), \
             patch.object(alignment.subprocess,'Popen',side_effect=worker):
            result,info=alignment.align_cues('fixture.wav',cues,'cpu',cancel_event=cancel)
        self.assertEqual(cues,[{'id':1,'text':'不自量力','start':.2,'end':1.6}])
        return result,info,calls

    def test_complete_optional_pass_adopts_confirmed_words(self):
        with tempfile.TemporaryDirectory() as folder:
            result,info,calls=self.run_pipeline(folder)
        self.assertEqual(len(calls),2)
        self.assertEqual(result[0]['text'],'不自量力');self.assertEqual(result[0]['start'],.8)
        self.assertTrue(result[0]['alignment']['vocal_assisted']);self.assertNotIn('alignment_warning',result[0])
        self.assertEqual(info['vocal_assistance']['accepted'],1)

    def test_missing_helper_conflict_and_failure_keep_original_and_existing_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            for mode,helper in [('accept',False),('conflict',True),('failure',True)]:
                with self.subTest(mode=mode,helper=helper):
                    result,info,calls=self.run_pipeline(folder,mode,helper=helper)
                    self.assertEqual(result[0]['start'],.2)
                    self.assertEqual(result[0]['alignment_warning'],'句首音效待复核')
                    self.assertEqual(info['vocal_assistance']['accepted'],0)
                    self.assertEqual(len(calls),2 if helper else 1)

    def test_cancel_is_not_swallowed_by_optional_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(Cancelled):self.run_pipeline(folder,cancel=threading.Event())


class VocalModelTests(unittest.TestCase):
    def test_fixed_official_source_and_reused_local_weights(self):
        download=models.ModelDownloads();revision,items=download._files(MODEL_NAME)
        self.assertEqual(len(items),1);self.assertEqual(items[0]['urls'][0]['url'],MODEL_URL)
        with tempfile.TemporaryDirectory() as folder,patch.object(models,'MODELS_DIR',Path(folder)):
            target=Path(folder)/MODEL_NAME/MODEL_FILE;target.parent.mkdir()
            target.write_bytes(b'partial');self.assertFalse(models.weights_present(MODEL_NAME))
            # 稀疏文件验证固定字节数识别，不写出数百 MB 假权重。
            with target.open('r+b') as stream:stream.truncate(MODEL_SIZE)
            self.assertTrue(models.weights_present(MODEL_NAME))
            self.assertEqual(models.set_model_location(MODEL_NAME,str(target.parent)),target.parent)
        self.assertEqual(models.AUXILIARY[MODEL_NAME]['purpose'],'alignment_support')


if __name__=='__main__':unittest.main()
