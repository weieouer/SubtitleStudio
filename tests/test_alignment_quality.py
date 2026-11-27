"""对齐异常判定与局部重试：只采用通过完整文字和时间检查的局部结果。"""
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core.alignment_quality import cue_word_slices, plain, word_timing_issue, cue_timing_issue
from engine_runner import align_chunk
from core.alignment import apply_word_times, _alignment_windows, _resolve_overlaps
from core.segment import split_on_gaps, _reference_break_at
import numpy as np


class Word:
    def __init__(self, text, start, end):
        self.text, self.start_time, self.end_time = text, start, end


class AlignmentQualityTests(unittest.TestCase):
    def test_detached_first_word_needs_agreement_between_context_windows(self):
        cues=[{'text':'前面','start':0.,'end':.09},
              {'text':'研究项目','start':.8,'end':1.6},
              {'text':'后面','start':1.8,'end':2.2}]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'audio.wav'
            with wave.open(str(path),'wb') as wav:
                wav.setparams((1,2,1000,0,'NONE',''))
                wav.writeframes(np.arange(3000,dtype='<i2').tobytes())
            class Model:
                calls=0
                def align(self,audio,text,language):
                    self.calls+=1
                    with wave.open(audio,'rb') as wav:
                        origin=struct.unpack('<h',wav.readframes(1))[0]/1000
                    timings={'前面':(0.,.09),'后面':(1.8,2.2),
                             '研':(.1,.18) if self.calls<=2 else (1.,1.08),
                             '究':(1.08,1.16),'项':(1.16,1.24),'目':(1.24,1.32)}
                    tokens=[t for line in text.splitlines() for t in (list(line) if line=='研究项目' else [line])]
                    return [[Word(t,timings[t][0]-origin,timings[t][1]-origin) for t in tokens]]
            model=Model()
            words,summary=align_chunk(model,{'path':str(path),'text':'前面\n研究项目\n后面','cues':cues})
            out=apply_word_times([dict(c) for c in cues],[0,1,2],words)
        self.assertEqual(model.calls,4)
        self.assertEqual(summary,{'attempted':1,'accepted':1,'unresolved':0})
        self.assertNotIn('alignment_error',out[1])
        self.assertEqual((out[1]['start'],out[1]['end']),(1.,1.32))

    def test_context_retry_confirms_observed_words_and_leaves_neighbors_untouched(self):
        cues=[{'text':'先来','start':.4,'end':.7},{'text':'马上','start':1.,'end':1.32},
              {'text':'回家','start':1.8,'end':2.2}]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'audio.wav'
            with wave.open(str(path),'wb') as wav:
                wav.setparams((1,2,1000,0,'NONE',''))
                wav.writeframes(np.arange(3000,dtype='<i2').tobytes())
            for corroborated in (True,False):
                class Model:
                    calls=0
                    def align(self,audio,text,language):
                        self.calls+=1
                        with wave.open(audio,'rb') as wav:
                            origin=struct.unpack('<h',wav.readframes(1))[0]/1000
                        measured={'先来':(.4,.7),'马':(1.,1.),'上':(1.16,1.32),'回家':(1.8,2.2)}
                        if self.calls==3:
                            start=1. if corroborated else 1.45
                            measured.update(马=(start,start+.16),上=(start+.16,start+.32))
                        tokens=[t for line in text.splitlines() for t in (list(line) if line=='马上' else [line])]
                        return [[Word(t,measured[t][0]-origin,measured[t][1]-origin) for t in tokens]]
                model=Model()
                words,summary=align_chunk(model,{'path':str(path),'text':'先来\n马上\n回家','cues':cues})
                delivered=apply_word_times([dict(c) for c in cues],[0,1,2],words)
                self.assertEqual(summary['accepted'],int(corroborated))
                self.assertEqual(bool(delivered[1].get('alignment_error')),not corroborated)
                self.assertEqual([(delivered[i]['start'],delivered[i]['end']) for i in (0,2)],[(.4,.7),(1.8,2.2)])
                if corroborated:
                    self.assertEqual(model.calls,3)
                    self.assertEqual([(w['start'],w['end']) for w in delivered[1]['words']],[(1.,1.16),(1.16,1.32)])

    def test_short_cue_late_inside_coarse_input_keeps_valid_word_times(self):
        for text,end,starts,ends in [('四师姐',3.52,[3.16,3.32,3.40],[3.32,3.40,3.56]),
                                    ('拿到了',2.32,[2.02,2.18,2.34],[2.18,2.34,2.42])]:
            with self.subTest(text=text),tempfile.TemporaryDirectory() as folder:
                class Model:
                    calls=0
                    def align(self,**kwargs):
                        self.calls+=1
                        return [[Word(t,a,b) for t,a,b in zip(text,starts,ends)]]
                path=Path(folder)/'audio.wav'
                with wave.open(str(path),'wb') as output:
                    output.setnchannels(1);output.setsampwidth(2);output.setframerate(1000)
                    output.writeframes(b'\0\0'*6000)
                cue={'text':text,'start':0.,'end':end};model=Model()
                words,summary=align_chunk(model,{'path':str(path),'text':text,'cues':[cue]})
                result=apply_word_times([cue],[0],words)[0]
            self.assertEqual(model.calls,1)
            self.assertEqual(summary,{'attempted':0,'accepted':0,'unresolved':0})
            self.assertNotIn('alignment_error',result)
            self.assertEqual((result['start'],result['end']),(starts[0],ends[-1]))

    def test_single_edge_outlier_is_checked_against_several_interior_words(self):
        words=[{'text':'你','start':0.,'end':1.1},{'text':'们','start':1.1,'end':1.25},
               {'text':'回','start':1.25,'end':1.4},{'text':'家','start':1.4,'end':1.55}]
        self.assertIn('句首',cue_timing_issue('你们回家',words,1.6))
        tail=[{'text':'你','start':0.,'end':.15},{'text':'们','start':.15,'end':.3},
              {'text':'回','start':.3,'end':.45},{'text':'家','start':.45,'end':1.55}]
        self.assertIn('句尾',cue_timing_issue('你们回家',tail,1.6))
        slow=[{'text':'你','start':0.,'end':.4},{'text':'们','start':.4,'end':.8},
              {'text':'回','start':.8,'end':1.2},{'text':'家','start':1.2,'end':1.6}]
        self.assertIsNone(cue_timing_issue('你们回家',slow,1.6))

    def test_short_sentence_cannot_hide_stretch_across_individually_valid_words(self):
        words=[{'text':'你','start':0.,'end':1.5},{'text':'好','start':1.5,'end':3.}]
        self.assertIsNone(word_timing_issue(words))
        self.assertIn('短句',cue_timing_issue('你好',words,1.))

    def test_short_sentence_ratio_flags_stretch_without_cutting_slow_speech(self):
        words=[{'text':'我们','start':0.,'end':1.},{'text':'回家','start':1.,'end':2.4}]
        self.assertIn('拉长',cue_timing_issue('我们回家',words,.6))
        self.assertIsNone(cue_timing_issue('我们回家',words,2.))

    def test_late_tail_retries_whole_sentence_while_normal_start_is_preserved(self):
        class Model:
            calls=0
            def align(self,audio,text,language):
                self.calls+=1
                if self.calls==1:
                    return [[Word('大',.1,.4),Word('哥',.4,2.9)]]
                return [[Word('大',.1,.4),Word('哥',.4,.8)]]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'audio.wav'
            with wave.open(str(path),'wb') as output:
                output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000)
                output.writeframes(b'\0\0'*16000*4)
            model=Model()
            words,summary=align_chunk(model,{'path':str(path),'text':'大哥',
                'cues':[{'text':'大哥','start':0.,'end':.8}]})
        self.assertEqual(model.calls,2)
        self.assertEqual(summary,{'attempted':1,'accepted':1,'unresolved':0})
        self.assertEqual((words[0]['start'],words[-1]['end']),(.1,.8))

    def test_large_word_overlap_is_rejected_but_rounding_overlap_is_allowed(self):
        self.assertIn('重叠',word_timing_issue([{'text':'你','start':0.,'end':.5},
                                              {'text':'好','start':.2,'end':.8}]))
        self.assertIsNone(word_timing_issue([{'text':'你','start':0.,'end':.5},
                                            {'text':'好','start':.48,'end':.8}]))

    def test_invalid_word_only_marks_its_cue_and_preserves_other_results(self):
        for start,end in [(2.,1.),(float('nan'),1.),('bad',1.)]:
            with self.subTest(start=start,end=end):
                cues=[{'text':'你好','start':0.,'end':1.},{'text':'世界','start':1.,'end':2.}]
                words=[{'text':'你好','start':start,'end':end},{'text':'世界','start':1.1,'end':1.9}]
                apply_word_times(cues,[0,1],words)
                self.assertTrue(cues[0]['alignment_error'])
                self.assertEqual((cues[0]['start'],cues[0]['end']),(0.,1.))
                self.assertNotIn('alignment_error',cues[1])
                self.assertEqual((cues[1]['start'],cues[1]['end']),(1.1,1.9))

    def test_numeric_decimal_and_time_punctuation_do_not_force_splits(self):
        for text, cut in [('价格3.5元',4),('明天12:30见',5)]:
            cue={'text':text,'reference_match':{'status':'high','reference':{'text':text}}}
            self.assertIsNone(_reference_break_at(cue,cut))

    def test_failed_long_input_does_not_erase_successful_neighbor_alignment(self):
        cues = [{'text':'异常句','start':1.,'end':130.,'alignment_error':'无法定位'},
                {'text':'正常句','start':14.,'end':15.,'words':[{'word':'正常句','start':14.,'end':15.}],
                 'alignment':{'engine':'test'}}]
        result = _resolve_overlaps(cues, [(1.,130.),(131.,132.)])
        self.assertEqual((result[1]['start'],result[1]['end']),(14.,15.))
        self.assertNotIn('alignment_error',result[1])
        self.assertTrue(result[1]['words'])
        self.assertEqual(result[0]['end'],14.)

    def test_reference_commas_colons_and_ellipsis_force_sentence_split(self):
        for punctuation in ('，', ',', '：', ':', '…', '；', '、'):
            with self.subTest(punctuation=punctuation):
                cue = {'text':'我们回家明天再见','start':0.,'end':2.,'words':[
                    {'word':'我们回家','start':0.,'end':1.}, {'word':'明天再见','start':1.,'end':2.}],
                    'reference_match':{'status':'high','reference':{'text':'我们回家'+punctuation+'明天再见'}}}
                parts, count = split_on_gaps([cue], 0)
                self.assertEqual(count,1)
                self.assertEqual([p['text'] for p in parts],['我们回家','明天再见'])

    def test_plain_strips_punctuation_and_width_variants(self):
        self.assertEqual(plain('你好，World！'), '你好world')

    def test_valid_word_times_pass(self):
        words = [{'text': '大哥', 'start': 1.0, 'end': 1.4},
                 {'text': '别怕', 'start': 1.5, 'end': 2.0}]
        self.assertIsNone(word_timing_issue(words))

    def test_empty_invalid_order_and_overlong_words_are_reported(self):
        self.assertEqual(word_timing_issue([]), '未得到可靠词时间')
        self.assertEqual(word_timing_issue([{'text': '哥', 'start': 2.0, 'end': 2.0}]),
                         '模型词时间无效')
        self.assertEqual(word_timing_issue([{'text': '哥', 'start': 1.0, 'end': 2.0},
                                            {'text': '别', 'start': .8, 'end': .9}]),
                         '模型词时间顺序异常')
        self.assertEqual(word_timing_issue([{'text': '哥', 'start': 0.0, 'end': 4.0}]),
                         '单个词时间异常拉长')
        self.assertEqual(word_timing_issue([{'text': '前', 'start': 0.0, 'end': .5},
                                            {'text': '后', 'start': 4.0, 'end': 4.5}]),
                         '同句词时间跨越过长空档')

    def test_cue_slices_require_exact_text_and_boundaries(self):
        cues = [{'text': '你好，'}, {'text': '世界'}]
        words = [{'text': '你好'}, {'text': '世界'}]
        self.assertEqual(cue_word_slices(cues, words), [(0, 1), (1, 2)])
        self.assertIsNone(cue_word_slices(cues, [{'text': '你好世界'}]))
        self.assertIsNone(cue_word_slices([{'text': '你好'}, {'text': '世界'}],
                                          [{'text': '你好'}, {'text': '远方'}]))

    def test_shifted_global_result_is_replaced_by_checked_local_retry(self):
        class Model:
            def __init__(self):
                self.calls = []

            def align(self, audio, text, language):
                self.calls.append(text)
                if len(self.calls) == 1:
                    return [[Word('大哥', 4.0, 4.4)]]
                return [[Word('大哥', .1, .9)]]

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audio.wav'
            with wave.open(str(path), 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(struct.pack('<h', 0) * 16000 * 6)
            model = Model()
            words, summary = align_chunk(model, {
                'path': str(path), 'text': '大哥', 'offset': 0,
                'cues': [{'text': '大哥', 'start': 0., 'end': 1.}]})
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(summary, {'attempted': 1, 'accepted': 1, 'unresolved': 0})
        self.assertEqual([(w['text'], w['start'], w['end']) for w in words],
                         [('大哥', .1, .9)])

    def test_retry_window_excludes_neighbour_line_for_repeated_short_cue(self):
        class Model:
            def __init__(self):
                self.calls = []

            def align(self, audio, text, language):
                with wave.open(audio, 'rb') as source:
                    duration = source.getnframes()/source.getframerate()
                self.calls.append((text, duration))
                if len(self.calls) == 1:
                    return [[Word('四',1.6,1.76), Word('师',1.76,1.84), Word('姐',1.84,2.04),
                             Word('东',2.56,2.64), Word('西',2.64,2.72), Word('拿',2.72,2.88),
                             Word('到',2.88,3.04), Word('了',4.64,4.72),
                             Word('拿',5.16,5.24), Word('到',5.24,5.4), Word('了',5.4,5.48)]]
                if duration > 2:
                    return [[Word('东',.32,1.28), Word('西',1.28,1.28),
                             Word('拿',1.36,1.52), Word('到',1.52,1.68), Word('了',1.68,1.76)]]
                return [[Word('东',.44,.52), Word('西',.52,.6), Word('拿',.6,.76),
                         Word('到',.76,.92), Word('了',.92,1.)]]

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'audio.wav'
            with wave.open(str(path), 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(1000)
                output.writeframes(struct.pack('<h', 0)*6500)
            cues = [{'text':'四师姐','start':0.,'end':2.04},
                    {'text':'东西拿到了？','start':2.04,'end':3.1},
                    {'text':'拿到了','start':5.16,'end':5.53}]
            model = Model()
            words, summary = align_chunk(model, {
                'path':str(path), 'text':'四师姐\n东西拿到了？\n拿到了', 'cues':cues})
            applied = apply_word_times([dict(cue) for cue in cues], [0, 1, 2], words)
        # 第一次主对齐把问句的“了”甩到下一句；重试不能把上一句尾音留在窗口里。
        self.assertEqual(len(model.calls), 2)
        self.assertGreater(model.calls[1][1], 1.)
        self.assertLess(model.calls[1][1], 2.)
        self.assertEqual(summary, {'attempted':1, 'accepted':1, 'unresolved':0})
        # 交付标准是整段首尾，不是中间逐字；这里确认问句的起止都落到音频上。
        self.assertNotIn('alignment_error', applied[1])
        self.assertEqual((applied[1]['start'], applied[1]['end']), (2.56, 3.12))

    def test_bad_internal_gap_keeps_sentence_whole_and_preserves_other_cue(self):
        cues = [{'text':'大哥别怕','start':1.,'end':3.},
                {'text':'下一句','start':24.,'end':25.}]
        words = [{'text':'大哥','start':1.,'end':1.5},
                 {'text':'别怕','start':22.,'end':22.5},
                 {'text':'下一句','start':24.,'end':25.}]
        result=apply_word_times(cues,[0,1],words)
        self.assertEqual((result[0]['start'],result[0]['end']),(1.,3.))
        self.assertIn('过长空档',result[0]['alignment_error'])
        self.assertEqual(result[0]['words'],[])
        self.assertNotIn('alignment_error',result[1])
        split,count=split_on_gaps(result,.5)
        self.assertEqual(count,0)
        self.assertEqual([cue['text'] for cue in split],['大哥别怕','下一句'])

    def test_failed_retry_marks_plausible_but_shifted_words_and_rolls_back(self):
        class Model:
            calls=0
            def align(self, **kwargs):
                self.calls+=1
                return [[Word('大哥',4.,4.5)]]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'audio.wav'
            with wave.open(str(path),'wb') as output:
                output.setnchannels(1);output.setsampwidth(2);output.setframerate(1000)
                output.writeframes(b'\0\0'*6000)
            cues=[{'text':'大哥','start':0.,'end':1.}]
            words,summary=align_chunk(Model(),{'path':str(path),'text':'大哥','cues':cues})
            result=apply_word_times(cues,[0],words)[0]
        self.assertEqual(summary,{'attempted':1,'accepted':0,'unresolved':1})
        self.assertEqual((result['start'],result['end']),(0.,1.))
        self.assertTrue(result['alignment_error']);self.assertEqual(result['words'],[])

    def test_good_result_does_not_retry_and_does_not_mutate_cues(self):
        class Model:
            calls=0
            def align(self, **kwargs):
                self.calls+=1
                return [[Word('你好',.1,.8)]]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'audio.wav'
            with wave.open(str(path),'wb') as output:
                output.setnchannels(1);output.setsampwidth(2);output.setframerate(1000)
                output.writeframes(b'\0\0'*2000)
            model=Model();cues=[{'text':'你好','start':.1,'end':.8}]
            _,summary=align_chunk(model,{'path':str(path),'text':'你好','cues':cues})
        self.assertEqual(model.calls,1)
        self.assertEqual(summary,{'attempted':0,'accepted':0,'unresolved':0})
        self.assertEqual(cues,[{'text':'你好','start':.1,'end':.8}])

    def test_default_windows_keep_context_local_without_losing_or_splitting_cues(self):
        cues=[{'text':'完整一句','start':i*2.,'end':i*2.+1.5} for i in range(40)]
        windows=_alignment_windows(cues,np.ones(82000,dtype=np.float32)*.1,1000)
        self.assertGreater(len(windows),1)
        self.assertEqual([i for window in windows for i in window['indices']],list(range(40)))
        self.assertTrue(all(window['end']-window['start']<=34 for window in windows))


if __name__ == '__main__':
    unittest.main()
