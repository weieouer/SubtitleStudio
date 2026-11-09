"""复核问题与多剧本乱序的行为回归，不调用真实识别模型。"""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from core import alignment, jobs, segment
from core.audio_boundaries import refine_boundaries, global_noise_floor, speech_intervals
from core.reference_match import parse_document, parse_documents, match_reference


def cue(text,i=0,**extra):
    return {'text':text,'start':i*2.,'end':i*2.+1.5,'id':i+1,**extra}


class BoundaryReviewTests(unittest.TestCase):
    def test_complete_word_not_split_even_with_acoustic_silence(self):
        item=cue('中毒的人',words=[{'word':'中','start':0,'end':.2},
            {'word':'毒','start':.4,'end':.6},{'word':'的人','start':.6,'end':1.2}])
        with patch.object(segment,'jieba') as tokenizer:
            tokenizer.lcut.return_value=['中毒','的','人']
            self.assertEqual(segment.split_on_gaps([item],.1,10)[1],0)
            item['audio_silences']=[{'start':.2,'end':.4}]
            self.assertEqual(segment.split_on_gaps([item],.1,10)[1],0)
            # 即使整句超限，也优先让宽度分段选词边界。
            self.assertEqual(segment.split_on_gaps([item],.1,3)[1],0)
            # 只有词本身已超限才可在词内拆。
            self.assertEqual(segment.split_on_gaps([item],.1,1)[1],1)

    def test_intact_short_sentence_and_overflow_prefer_word_pause_boundaries(self):
        self.assertEqual(segment.split_text('中毒的人',10),['中毒的人'])
        text='我们今天一起回家吃饭'
        parts=segment.split_text(text,6,{6:2})
        self.assertEqual(''.join(parts),text)
        self.assertTrue(all(segment.text_width(p)<=6 for p in parts))
        boundaries=set();offset=0
        for token in segment.jieba.lcut(text):offset+=len(token);boundaries.add(offset)
        offset=0
        for part in parts:offset+=len(part);self.assertIn(offset,boundaries)

    def test_continuous_speech_word_gaps_do_not_isolate_one_or_two_chars(self):
        # 声波连续时词时间仍可能有空档；有静音证据列表但空档内没有静音，不能切。
        item=cue('我今天去公园散步',words=[
            {'word':'我','start':0,'end':.2},{'word':'今天','start':1.,'end':1.4},
            {'word':'去','start':1.4,'end':1.6},{'word':'公园','start':1.6,'end':2.},
            {'word':'散步','start':2.,'end':2.4}],audio_silences=[])
        parts,n=segment.split_on_gaps([item],.6)
        self.assertEqual((n,[p['text'] for p in parts]),(0,['我今天去公园散步']))
        item=cue('我们今天去公园散步吧',words=[
            {'word':'我们','start':0,'end':.3},{'word':'今天','start':.3,'end':.7},
            {'word':'去','start':.7,'end':.9},{'word':'公园','start':.9,'end':1.3},
            {'word':'散步','start':1.3,'end':1.7},{'word':'吧','start':2.5,'end':2.7}],audio_silences=[])
        self.assertEqual(segment.split_on_gaps([item],.6)[1],0)

    def test_single_character_is_never_left_alone_inside_a_sentence(self):
        item=cue('我们去公园散步',words=[
            {'word':'我们','start':0,'end':.3},{'word':'去','start':.9,'end':1.},
            {'word':'公园','start':1.6,'end':2.},{'word':'散步','start':2.,'end':2.4}],
            audio_silences=[{'start':.3,'end':.9},{'start':1.,'end':1.6}])
        parts,n=segment.split_on_gaps([item],.5)
        self.assertGreaterEqual(n,0)
        self.assertEqual(''.join(p['text'] for p in parts),item['text'])
        self.assertTrue(all(segment.significant_width(p['text'])>=2 for p in parts))

    def test_acoustic_pause_still_splits_but_never_off_a_single_char(self):
        item=cue('你好，走吧！',words=[{'word':'你好','start':0,'end':.2},
            {'word':'走吧','start':1.2,'end':1.6}],audio_silences=[{'start':.2,'end':1.2}])
        self.assertEqual(segment.split_on_gaps([item],.6)[1],1)
        # 真静音也不把句首的一个字单独切出去。
        item=cue('我今天去公园散步',words=[
            {'word':'我','start':0,'end':.2},{'word':'今天','start':1.2,'end':1.6},
            {'word':'去','start':1.6,'end':1.8},{'word':'公园','start':1.8,'end':2.2},
            {'word':'散步','start':2.2,'end':2.6}],audio_silences=[{'start':.2,'end':1.2}])
        self.assertEqual(segment.split_on_gaps([item],.6)[1],0)

    def test_width_split_never_leaves_a_single_character_line(self):
        for text,max_width in [('我们今天去公园散步',6),('你好，我们今天去公园',9),
                               ('这是一个测试句子用来检查分段',7)]:
            parts=segment.split_text(text,max_width)
            if len(parts)>1:
                self.assertTrue(all(segment.significant_width(part)>=2 for part in parts),
                                (text,parts))
            self.assertEqual(''.join(parts),text)

    def test_actual_short_fragment_shape_merges_text_and_full_time(self):
        parent={'text':'中毒的人','start':79.46,'end':82.03,'id':19}
        items=[cue('中',start=81.47,end=81.71,asr_original=parent,source='gap_split'),
               cue('毒的人',start=81.71,end=82.23,asr_original=parent,source='gap_split')]
        parts,n=segment.repair_fragments(items,10,.1)
        self.assertEqual(n,1);self.assertEqual(parts[0]['text'],'中毒的人')
        self.assertEqual((parts[0]['start'],parts[0]['end']),(81.47,82.23))
        self.assertEqual(parts[0]['asr_original']['text'],'中毒的人')
        items[1]['start']=81.95
        self.assertEqual(segment.repair_fragments(items,10,.1)[1],0)
        items[1]['start']=81.71
        self.assertEqual(segment.repair_fragments(items,3,.1)[1],0)

    def test_already_accurate_word_end_still_protects_following_silence(self):
        sr=16000;audio=np.zeros(sr*3,dtype=np.float32)
        audio[6400:16000]=.08*np.sin(2*np.pi*220*np.arange(9600)/sr)
        audio[24000:32000]=.08*np.sin(2*np.pi*220*np.arange(8000)/sr)
        item=cue('前一句',start=.35,end=1.,words=[{'word':'前一句','start':.4,'end':1.}])
        original=deepcopy(item)
        result=refine_boundaries([item],audio)[0]
        self.assertIn('pause_after',result)
        self.assertLess(result['pause_after'],1.1)
        self.assertLess(abs(result['end']-item['end']),.12)
        self.assertEqual(item,original)

    def test_global_noise_floor_resists_a_single_sixty_ms_dip(self):
        audio=np.ones(16000*5,dtype=np.float32)*.01
        audio[16000:16960]=0
        baseline=global_noise_floor(audio)
        self.assertAlmostEqual(baseline,.01,places=5)
        rms=np.full(300,.01);rms[100:106]=0
        _,threshold=speech_intervals(rms,np.zeros(len(rms)),baseline=baseline)
        if threshold:self.assertGreaterEqual(threshold['noise_floor'],baseline*.5)

    def test_typo_preserves_current_text_and_mapped_word_times(self):
        text='我们今天明天去学效学习';reference='我们今天明天去学校学习'
        words=[{'text':char,'start':.1+i*.2+(.5 if i>=6 else 0),
                'end':.2+i*.2+(.5 if i>=6 else 0)} for i,char in enumerate(reference)]
        items=[cue(text)]
        result=alignment.apply_word_times(items,[0],words)[0]
        self.assertEqual(result['text'],text);self.assertNotIn('alignment_error',result)
        self.assertTrue(result['words']);self.assertEqual(''.join(w['word'] for w in result['words']),text)
        self.assertTrue(result['estimated'])
        self.assertGreater(segment.split_on_gaps([result],.4)[1],0)

    def test_unmappable_insert_keeps_input_time_and_reports_error(self):
        text='我们今天明天要去学校学习';reference='我们今天明天去学校学习'
        item=cue(text,words=[{'word':text,'start':0,'end':1.5}])
        result=alignment.apply_word_times([item],[0],[{'text':reference,'start':3,'end':6}])[0]
        self.assertEqual((result['start'],result['end']),(0,1.5))
        self.assertTrue(result['alignment_error']);self.assertTrue(result['needs_review'])
        self.assertEqual(result['words'],[])

    def test_overlap_rollback_clears_all_derived_timing(self):
        items=[cue('前一句',start=3,end=6),cue('后一句',start=1,end=2)]
        for item in items:
            item.update(words=[{'word':item['text'],'start':item['start'],'end':item['end']}],
                        pause_after=item['end'],pause_before=item['start'],audio_silences=[{'start':1,'end':2}],
                        alignment={'engine':'test'})
        result=alignment._resolve_overlaps(items,[(0,1),(2,3)])
        for item in result:
            self.assertEqual(item['words'],[]);self.assertNotIn('pause_after',item)
            self.assertNotIn('pause_before',item);self.assertNotIn('audio_silences',item)
            self.assertTrue(item['alignment']['stale']);self.assertTrue(item['needs_review'])
        self.assertEqual(segment.split_on_gaps(result,.1)[1],0)

    def test_alignment_does_not_auto_exclude_normal_outro_dialogue(self):
        items=[cue('感谢观看点赞关注一键三连',start=0,end=30)]
        with tempfile.TemporaryDirectory() as folder,patch.object(jobs,'RESULTS_DIR',Path(folder)), \
             patch.object(alignment,'align_cues',return_value=(items,{})):
            manager=jobs.JobManager();job=manager.submit('clip.wav','aligner','cpu',False,0,task='align',input_cues=items,max_width=30)
            manager.queue.join()
            self.assertEqual(job.status,'done',job.error)
            self.assertFalse(job.cues[0].get('exclude_export'))
            self.assertNotIn('hallucination',job.cues[0])


class SourceMatchingTests(unittest.TestCase):
    A='我们明天去公园散步。\n那就带上雨伞一起出发。'
    B='今天晚上一起回家吃饭。\n饭后记得早点休息。'

    def match(self,order,texts):
        documents=[{'name':name+'.txt','text':getattr(self,name)} for name in order]
        return match_reference([cue(t,i) for i,t in enumerate(texts)],parse_documents(documents))

    def test_reversed_import_order_still_follows_video_dialogue(self):
        texts=['我们明天去公园散步','那就带上雨伞一起出发','今天晚上一起回家吃饭','饭后记得早点休息']
        for order in (['A','B'],['B','A']):
            result=self.match(order,texts)
            self.assertEqual(result['stats']['matched'],4)
            self.assertEqual([c['reference_match']['reference']['source_name'] for c in result['cues']],['A.txt','A.txt','B.txt','B.txt'])
            self.assertTrue(all(c['reference_match']['status']=='high' for c in result['cues']))
            for item in result['cues']:
                ref=item['reference_match']['reference']
                self.assertEqual(result['document']['text'][ref['start']:ref['end']],ref['text'])

    def test_can_switch_sources_then_return_without_reusing_old_position(self):
        result=self.match(['B','A'],['我们明天去公园散步','今天晚上一起回家吃饭','那就带上雨伞一起出发','饭后记得早点休息'])
        self.assertEqual(result['stats']['matched'],4)
        self.assertEqual([c['reference_match']['reference']['source_name'] for c in result['cues']],['A.txt','B.txt','A.txt','B.txt'])
        self.assertEqual(result['gaps'],[])

    def test_same_source_cannot_reverse_or_reuse_dialogue(self):
        result=self.match(['A','B'],['那就带上雨伞一起出发','我们明天去公园散步'])
        self.assertEqual(result['stats']['matched'],1)
        result=self.match(['A','B'],['我们明天去公园散步']*3)
        self.assertEqual(result['stats']['matched'],1)

    def test_candidates_do_not_cross_file_boundary(self):
        doc=parse_documents([{'name':'甲','text':'甲乙丙丁'},{'name':'乙','text':'戊己庚辛'}])
        result=match_reference([cue('甲乙丙丁戊己庚辛')],doc)
        for candidate in result['cues'][0]['reference_match']['alternatives']:
            self.assertNotIn('\n',candidate['text'])

    def test_unicode_source_offsets_and_invalid_sources(self):
        doc=parse_documents([{'name':'甲😀','text':'第一段😀\r\n你好。'},{'name':'乙','text':'第二段再见。'}])
        self.assertEqual(doc['text'][doc['sources'][1]['start']:],'第二段再见。')
        self.assertEqual(parse_document(doc['text'],doc['sources']),doc)
        with self.assertRaises(ValueError):parse_document('abcdef',[{'id':'a','start':0,'end':2}])
        with self.assertRaises(ValueError):parse_documents([{'id':'a','text':'甲'},{'id':'a','text':'乙'}])

    def test_split_inherits_distinct_reference_ranges_and_source(self):
        doc=parse_documents([{'name':'乙','text':self.B},{'name':'甲','text':'你好，走吧！'}])
        original=cue('你好，走吧！',words=[{'word':'你好','start':0,'end':.4},{'word':'走吧','start':1.,'end':1.4}])
        matched=match_reference([original],doc)['cues'][0]
        parts,n=segment.split_on_gaps([matched],.5)
        self.assertEqual(n,1)
        self.assertTrue(all(p['reference_match']['inherited'] for p in parts))
        refs=[p['reference_match']['reference'] for p in parts]
        self.assertEqual(''.join(p['script_text'] for p in parts),matched['script_text'])
        self.assertEqual(refs[0]['end'],refs[1]['start'])
        self.assertTrue(all(r['source_name']=='甲' for r in refs))
        for ref in refs:self.assertEqual(doc['text'][ref['start']:ref['end']],ref['text'])

    def test_legacy_flat_document_remains_compatible(self):
        result=match_reference([cue('我们明天去公园散步')],{'text':self.A})
        self.assertEqual(result['stats']['matched'],1)
        self.assertEqual(len(result['document']['sources']),1)


if __name__=='__main__':unittest.main()
