"""停顿断句和底噪边界：用确定的音频和词时间验证，不启动模型。"""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))
from core import segment, jobs, alignment
from core.audio_boundaries import speech_intervals, refine_boundaries
import app


class PauseTests(unittest.TestCase):
    def test_reference_cut_keeps_pause_when_background_masks_acoustic_silence(self):
        cue={'text':'守住！援军赶来','start':0.,'end':1.84,
             'words':[{'word':'守住','start':0.,'end':.4},{'word':'援军赶来','start':1.2,'end':1.84}],
             'audio_silences':[{'start':.38,'end':.54}],
             'reference_match':{'status':'high','query_text':'守住！援军赶来','reference':{'text':'守住！援军赶来'}}}
        before=deepcopy(cue)
        parts,count=segment.split_on_gaps([cue],.3,16)
        self.assertEqual(count,1)
        self.assertEqual([c['text'] for c in parts],['守住！','援军赶来'])
        self.assertEqual((parts[0]['end'],parts[1]['start']),(.4,1.2))
        self.assertEqual(cue,before)

    def test_large_word_gap_splits_when_silence_detection_misses_pause(self):
        # 背景声让 audio_silences 漏掉 0.5 秒停顿，词时间仍是明显证据。
        cue={'start':0.,'end':2.,'text':'我们回家明天再见','words':[
            {'word':'我们回家','start':0.,'end':.8},
            {'word':'明天再见','start':1.3,'end':2.}],
            'audio_silences':[{'start':2.2,'end':2.4}]}
        parts,n=segment.split_on_gaps([cue],.1,16)
        self.assertEqual(n,1)
        self.assertEqual([c['text'] for c in parts],['我们回家','明天再见'])
        self.assertEqual((parts[0]['end'],parts[1]['start']),(.8,1.3))

    def test_reference_break_overrides_single_tokenizer_token(self):
        # 参考里的逗号是明确句界；分词把它并成一个词也不能吞掉断句。
        class WholeTokenizer:
            @staticmethod
            def lcut(text):
                return [text]
        cue={'start':0.,'end':.85,'text':'这地方不大倒还藏着个','words':[
            {'word':'这','start':0.,'end':.08},{'word':'地','start':.08,'end':.16},
            {'word':'方','start':.16,'end':.24},{'word':'不','start':.24,'end':.32},
            {'word':'大','start':.32,'end':.40},{'word':'倒','start':.45,'end':.53},
            {'word':'还','start':.53,'end':.61},{'word':'藏','start':.61,'end':.69},
            {'word':'着','start':.69,'end':.77},{'word':'个','start':.77,'end':.85}],
            'reference_match':{'status':'high','query_text':'这地方不大倒还藏着个','stale':False,
                               'reference':{'text':'这地方不大，倒还藏着个'}}}
        with patch.object(segment,'jieba',WholeTokenizer()):
            parts,n=segment.split_on_gaps([cue],.1,16)
        self.assertEqual(n,1)
        self.assertEqual([c['text'] for c in parts],['这地方不大','倒还藏着个'])
        self.assertEqual((parts[0]['end'],parts[1]['start']),(.40,.45))

    def test_tenth_second_pause_survives_float_rounding(self):
        cue={'start':0,'end':.6,'text':'你好，走吧！','words':[
            {'word':'你好','start':0,'end':.2},{'word':'走吧','start':.3,'end':.6}]}
        parts,n=segment.split_on_gaps([cue],.1)
        self.assertEqual(n,1);self.assertEqual(parts[0]['end'],.2)
        self.assertEqual(parts[1]['start'],.3)
        self.assertEqual(''.join(p['text'] for p in parts),cue['text'])
        self.assertEqual(parts[0]['pause_after'],.2);self.assertEqual(parts[1]['pause_before'],.3)

    def test_width_split_retains_words_for_later_pause_change(self):
        text='甲乙丙丁戊己庚辛壬癸子丑寅卯'
        words=[{'word':c,'start':round(i*.1+(1 if i>=3 else 0),3),
                'end':round((i+1)*.1+(1 if i>=3 else 0),3)} for i,c in enumerate(text)]
        cue={'start':0,'end':2.4,'text':text,'words':words,'audio_silences':[{'start':.3,'end':1.3}]}
        # 前片留足 3 个字以上，避免新版“单字不成条”保护直接拦住这个用例。
        with patch.object(segment,'split_text',return_value=[text[:6],text[6:10],text[10:]]):
            width_parts,_,_=segment.segment_cues([cue],6)
        self.assertTrue(all(p['words'] for p in width_parts))
        with patch.object(segment,'jieba') as tokenizer:
            tokenizer.lcut.side_effect=list
            parts,n=segment.split_on_gaps(width_parts,.5)
        self.assertEqual(n,1)
        self.assertEqual(parts[0]['end'],.3);self.assertEqual(parts[1]['start'],1.3)
        self.assertEqual(''.join(p['text'] for p in parts),text)

    def test_width_boundary_does_not_fill_existing_word_gap(self):
        text='你好走吧'
        cue={'start':0,'end':2,'text':text,'words':[
            {'word':'你好','start':0,'end':.5},{'word':'走吧','start':1.5,'end':2}]}
        with patch.object(segment,'split_text',return_value=['你好','走吧']):
            parts,_=segment.segment_cue(cue,2)
        self.assertEqual(parts[0]['end'],.5);self.assertEqual(parts[1]['start'],1.5)

    def test_acoustic_pause_splits_a_word_end_stretched_into_noise(self):
        cue={'start':0,'end':2,'text':'你好，走吧！','words':[
            {'word':'你好','start':0,'end':1.4},{'word':'走吧','start':1.5,'end':2}],
            'audio_silences':[{'start':.6,'end':1.5}]}
        before=deepcopy(cue)
        parts,n=segment.split_on_gaps([cue],.6)
        self.assertEqual(n,1);self.assertLess(parts[0]['end'],.7)
        self.assertEqual(parts[1]['start'],1.5);self.assertEqual(cue,before)
        self.assertTrue(all(w['end']<=p['end'] for p in parts for w in p['words']))

    def test_acoustic_pause_does_not_guess_words_inside_one_token(self):
        cue={'start':0,'end':2,'text':'你好走吧','words':[{'word':'你好走吧','start':0,'end':2}],
             'audio_silences':[{'start':.6,'end':1.5}]}
        self.assertEqual(segment.split_on_gaps([cue],.3),([cue],0))

    def test_short_pause_is_found_even_when_under_twenty_percent_of_sentence(self):
        for level in (.1,.0002):
            rms=np.r_[np.full(100,level),np.full(10,level/50),np.full(100,level)]
            intervals,threshold=speech_intervals(rms,np.full(len(rms),100.))
            self.assertEqual(len(intervals),2)
            self.assertGreater(threshold['dead_zone'],level/50)
            self.assertGreater(intervals[1][0]-intervals[0][1],.06)

    def test_noise_dead_zone_preserves_audio_and_removes_long_silent_tail(self):
        sr=16000;t=np.arange(sr*3)/sr
        audio=(.0003*np.sin(2*np.pi*60*t)).astype(np.float32)
        for start,end in ((.4,.7),(1.3,1.6)):
            a,b=round(start*sr),round(end*sr)
            audio[a:b]+=.05*np.sin(2*np.pi*220*t[a:b])
        cue={'start':.35,'end':2.5,'text':'你好，走吧！','words':[
            {'word':'你好','start':.4,'end':1.},{'word':'走吧','start':1.3,'end':2.5}]}
        original=audio.copy();before=deepcopy(cue)
        refined=refine_boundaries([cue],audio)[0]
        self.assertLess(refined['end'],1.8)
        self.assertTrue(refined['audio_refinement']['long_silence_trim'])
        self.assertEqual(refined['original_end'],2.5)
        self.assertEqual(refined['pause_after'],refined['end'])
        parts,n=segment.split_on_gaps([refined],.4)
        self.assertEqual(n,1)
        self.assertGreater(parts[1]['start']-parts[0]['end'],.4)
        self.assertEqual(''.join(p['text'] for p in parts),cue['text'])
        np.testing.assert_array_equal(audio,original);self.assertEqual(cue,before)

    def test_constant_noise_does_not_fabricate_word_boundaries(self):
        cue={'start':.1,'end':.9,'text':'轻声对白'}
        refined=refine_boundaries([cue],np.ones(16000,dtype=np.float32)*.001)[0]
        self.assertEqual((refined['start'],refined['end']),(.1,.9))
        self.assertEqual(refined['audio_refinement']['status'],'uncertain')

    def test_quiet_last_word_is_not_removed_by_a_louder_previous_phrase(self):
        sr=16000;t=np.arange(sr*3)/sr
        audio=(.0003*np.sin(2*np.pi*60*t)).astype(np.float32)
        audio[3200:16000]+=.08*np.sin(2*np.pi*220*t[3200:16000])
        audio[24000:28800]+=.001*np.sin(2*np.pi*190*t[24000:28800])
        cue={'start':.1,'end':2.4,'text':'大声说话，轻声回答','words':[
            {'word':'大声说话','start':.2,'end':1.},{'word':'轻声回答','start':1.5,'end':1.8}]}
        refined=refine_boundaries([cue],audio)[0]
        self.assertGreaterEqual(refined['end'],1.8)
        self.assertEqual(refined['text'],cue['text'])

    def test_alignment_request_carries_global_pause_setting(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'clip.wav';source.write_bytes(b'fixture')
            handler=object.__new__(app.Handler);handler.send_json=MagicMock()
            with patch.object(app.DOWNLOADS,'get',return_value={'status':'idle'}), \
                 patch.object(app,'list_auxiliary',return_value=[{'name':'qwen3-forcedaligner-0.6b','available':True}]), \
                 patch.object(app.JOBS,'submit') as submit:
                handler._create_alignment_job({'path':str(source),'cues':[{'start':0,'end':1,'text':'你好'}],
                    'device':'cpu','request_id':'a'*12,'gap_seconds':.1})
                self.assertEqual(submit.call_args.args[4],.1)

    def test_alignment_job_splits_on_pause_and_width_without_fragment_merge(self):
        base=[{'start':0,'end':2,'text':'你好，走吧！','words':[
            {'word':'你好','start':0,'end':.5},{'word':'走吧','start':1.5,'end':2}]},
            {'start':2,'end':2.2,'text':'中'},{'start':2.2,'end':2.8,'text':'毒的人'}]
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(jobs,'RESULTS_DIR',Path(temp)), \
             patch.object(alignment,'align_cues',side_effect=lambda path,cues,device,on_progress=None,cancel_event=None:(deepcopy(cues),{})):
            def run(gap,width_split=True):
                manager=jobs.JobManager()
                job=manager.submit('clip.wav','qwen3-forcedaligner-0.6b','cpu',False,gap,
                    task='align',input_cues=deepcopy(base),max_width=3,width_split=width_split)
                manager.queue.join()
                self.assertEqual(job.status,'done',job.error)
                return job
            split=run(.5)
            self.assertEqual([c['text'] for c in split.cues],['你好，','走吧！','中','毒的人'])
            self.assertEqual(split.info['gap_splits'],1)
            self.assertEqual(split.info['width_splits'],0)
            self.assertLessEqual(split.cues[0]['end'],.5)
            self.assertEqual(split.cues[1]['start'],1.5)
            self.assertNotIn('fragment_merges',split.info)
            # 0 秒关闭停顿拆条，但过长字幕仍按每行字数分段。
            width_only=run(0)
            self.assertEqual([c['text'] for c in width_only.cues],['你好，','走吧！','中','毒的人'])
            self.assertNotIn('gap_splits',width_only.info)
            self.assertEqual(width_only.info['width_splits'],1)
            self.assertNotIn('fragment_merges',width_only.info)
            # width_split=False 时关闭宽度拆条，保持条数与输入一致（兼容旧调用）。
            stable=run(0,False)
            self.assertEqual([c['text'] for c in stable.cues],['你好，走吧！','中','毒的人'])
            self.assertNotIn('width_splits',stable.info)

    def test_reference_sentence_boundary_is_not_dropped_as_short_fragment(self):
        # 参考里一秒一句，无论声音停顿长短都要保留句界，不能当碎片并回去。
        for word_start in (.6, 1.6):
            with self.subTest(word_start=word_start):
                cue={'start':0,'end':word_start+.6,'text':'好走吧','words':[
                    {'word':'好','start':0,'end':.4},
                    {'word':'走吧','start':word_start,'end':word_start+.6}],
                    'reference_match':{'status':'high','query_text':'好走吧','stale':False,
                                       'reference':{'text':'好。走吧'}}}
                parts,n=segment.split_on_gaps([deepcopy(cue)],.5)
                self.assertEqual(n,1)
                self.assertEqual([p['text'] for p in parts],['好','走吧'])

    def test_reference_sentence_controls_pause_split(self):
        def run(reference_text, word_gap):
            cue={'start':0,'end':word_gap+1.5,'text':'你好走吧','words':[
                {'word':'你好','start':0,'end':.5},
                {'word':'走吧','start':.5+word_gap,'end':word_gap+1.5}],
                'reference_match':{'status':'high','query_text':'你好走吧','stale':False,
                                   'reference':{'text':reference_text}}}
            return segment.split_on_gaps([deepcopy(cue)],.5)
        # 参考是同一句，停顿未达阈值：不拆。
        same,held=run('你好走吧',.2)
        self.assertEqual(held,0)
        self.assertEqual([p['text'] for p in same],['你好走吧'])
        # 参考仍是同一句，停顿达到阈值：按声音停顿拆开。
        parts,n=run('你好走吧',1.0)
        self.assertEqual(n,1)
        self.assertEqual([p['text'] for p in parts],['你好','走吧'])
        # 参考里是两句话：停顿没到阈值也按句界拆开。
        two,n=run('你好。走吧',.2)
        self.assertEqual(n,1)
        self.assertEqual([p['text'] for p in two],['你好','走吧'])


if __name__=='__main__':unittest.main()
