"""上下文插入与独立平衡分段：保留原文、时间和相邻字幕。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import app
from core import segment
from core.calibration import normalize_cues
from core.reference_match import parse_documents,match_reference


def cues(texts):return [{'text':text,'start':i*2.,'end':i*2.+1.5,'id':i+1} for i,text in enumerate(texts)]

class ContextTests(unittest.TestCase):
    left='我们明天一起去公园散步'
    right='请你现在把门口快递拿回来'
    extra='感谢各位观众朋友一直以来的支持和陪伴'

    def test_bounded_reference_uses_original_words_and_offsets(self):
        for said, written, expected in [('驱除的太慢', '祛除得太慢', '祛除得太慢'),
                                        ('他离开千水去了天宇与另外', '帝侯君王去了天宇与另外', '帝侯君王去了天宇与另外'),
                                        ('一封', '一方', '一封'), ('地方大大杀了人','地方大大n','地方大大杀了人')]:
            with self.subTest(said=said):
                raw = '。'.join([self.left, written, self.right, said])
                items = cues([self.left, said, self.right])
                matched = match_reference(items, {'text': raw})['cues']
                out = normalize_cues(matched)['cues'][1]
                ref = out['reference_match']['reference']
                self.assertEqual(raw[ref['start']:ref['end']], written+'。')
                self.assertEqual(ref['text'], out['script_text'])
                self.assertEqual(out['text'], expected)
                self.assertEqual(out['reference_match']['status'], 'weak' if said==expected else 'high')
                self.assertEqual(out['asr_original']['text'], said)

    def test_exact_elsewhere_in_other_script_is_red_and_not_adoptable(self):
        doc=parse_documents([{'name':'当前场景','text':self.left+'。\n'+self.right+'。'},
                             {'name':'其他场景','text':self.extra+'。'}])
        original=cues([self.left,self.extra,self.right]);before=deepcopy(original)
        result=match_reference(original,doc)
        middle=result['cues'][1];match=middle['reference_match']
        self.assertEqual(match['status'],'context_mismatch');self.assertIsNone(match['reference'])
        self.assertEqual(middle['script_text'],'');self.assertTrue(middle['match_needs_review'])
        self.assertTrue(match['alternatives']);self.assertTrue(all(c['reference_only'] for c in match['alternatives']))
        self.assertEqual([result['cues'][i]['reference_match']['status'] for i in (0,2)],['high','high'])
        self.assertEqual(result['stats']['matched'],2);self.assertEqual(original,before)

    def test_later_same_script_exact_match_cannot_steal_following_anchor(self):
        text=self.left+'。'+self.right+'。'+'这是后面另一个完全不同的场景。'*10+self.extra+'。'
        result=match_reference(cues([self.left,self.extra,self.right]),{'text':text})
        self.assertEqual([c['reference_match']['status'] for c in result['cues']],['high','context_mismatch','high'])

    def test_absent_from_script_is_context_problem_even_without_global_hit(self):
        result=match_reference(cues([self.left,'完全不存在的多余识别台词',self.right]),{'text':self.left+'。'+self.right+'。'})
        self.assertEqual(result['cues'][1]['reference_match']['status'],'context_mismatch')

    def test_local_script_dialogue_and_typo_are_not_insertions(self):
        middle='我们今天去学校学习'
        for said,status in [(middle,'high'),('我们今天去学效学习','homophone')]:
            result=match_reference(cues([self.left,said,self.right]),{'text':'。'.join([self.left,middle,self.right])})
            self.assertEqual(result['cues'][1]['reference_match']['status'],status)

    def test_two_inserted_lines_do_not_get_swallowed_by_group_candidate(self):
        doc=parse_documents([{'name':'当前','text':self.left+'。'+self.right+'。'},
            {'name':'另处','text':self.extra+'。'}])
        result=match_reference(cues([self.left,'无关的一句广告台词','又多出的一段无关内容',self.right]),doc)
        self.assertTrue(all(c['reference_match']['status']=='context_mismatch' for c in result['cues'][1:3]))
        self.assertEqual(result['stats']['matched'],2)

    def test_real_multisource_alternation_keeps_source_sequence(self):
        a=['我们明天去公园散步','那就带上雨伞一起出发']
        b=['今天晚上一起回家吃饭','饭后记得早点休息']
        doc=parse_documents([{'name':'乙','text':'。'.join(b)},{'name':'甲','text':'。'.join(a)}])
        result=match_reference(cues([a[0],b[0],a[1],b[1]]),doc)
        self.assertEqual(result['stats']['matched'],4)
        self.assertFalse(any(c['reference_match']['status']=='context_mismatch' for c in result['cues']))

    def test_sparse_selection_cannot_fake_immediate_context(self):
        doc=parse_documents([{'name':'当前','text':self.left+'。'+self.right+'。'},
            {'name':'另处','text':self.extra+'。'}])
        result=match_reference(cues([self.left,self.extra,self.right]),doc,sequence_indices=[0,9,15])
        self.assertNotEqual(result['cues'][1]['reference_match']['status'],'context_mismatch')

    def test_http_rejects_invalid_context_sequence(self):
        handler=object.__new__(app.Handler);handler.send_json=Mock()
        for sequence in ([0,0],[0],['0',1]):
            with self.assertRaisesRegex(ValueError,'上下文序号'):
                handler._match_script({'cues':cues([self.left,self.right]),'document':{'text':self.left+self.right},'sequence_indices':sequence})


class MidpointTests(unittest.TestCase):
    def test_midpoint_splits_text_instead_of_duplicating_or_borrowing_next(self):
        text='谁能把黎红拉进自己的战队奖金五十万'
        original=cues([text,'原来的下一句保持完整']);before=deepcopy(original)
        pieces,count=segment.segment_midpoint(original[0],10)
        self.assertEqual(count,1);self.assertEqual(''.join(p['text'] for p in pieces),text)
        self.assertLessEqual(abs(len(pieces[0]['text'])-len(pieces[1]['text'])),4)
        self.assertTrue(all(p['manual_split'] for p in pieces));self.assertEqual(original,before)
        self.assertEqual(pieces[0]['start'],original[0]['start']);self.assertEqual(pieces[-1]['end'],original[0]['end'])

    def test_complete_words_and_real_word_gap_survive_midpoint(self):
        item={'text':'我们今天一起回家吃饭','start':0.,'end':3.,'words':[
            {'word':'我们','start':0,'end':.5},{'word':'今天','start':.5,'end':1.},
            {'word':'一起','start':1.5,'end':2.},{'word':'回家','start':2.,'end':2.5},{'word':'吃饭','start':2.5,'end':3.}]}
        parts,n=segment.segment_midpoint(item,10)
        self.assertEqual(n,1);self.assertEqual(''.join(p['text'] for p in parts),item['text'])
        self.assertTrue(all(p['words'] for p in parts))
        self.assertTrue(all(w['start']>=p['start'] and w['end']<=p['end'] for p in parts for w in p['words']))

    def test_short_complete_word_is_not_forcibly_split(self):
        item=cues(['中毒'])[0]
        self.assertEqual(segment.segment_midpoint(item,10),([item],0))

    def test_manual_split_and_different_split_parents_are_not_automerged(self):
        parent={'id':1,'text':'中毒的人','start':0,'end':1}
        items=[{'text':'中','start':0,'end':.2,'asr_original':parent,'manual_split':True},
               {'text':'毒的人','start':.2,'end':1,'asr_original':parent}]
        self.assertEqual(segment.repair_fragments(items,10)[1],0)
        items[0].pop('manual_split');items[0]['split_parent']='first';items[1]['split_parent']='next'
        self.assertEqual(segment.repair_fragments(items,10)[1],0)

    def test_width_limit_prefers_balanced_independent_parts(self):
        text='我们今天一起回家吃饭然后明天带上雨伞出发'
        parts=segment.split_text(text,12)
        self.assertEqual(''.join(parts),text);self.assertEqual(len(parts),2)
        self.assertLessEqual(max(map(len,parts))-min(map(len,parts)),4)

    def test_very_long_manual_split_still_respects_global_width(self):
        item=cues(['我们今天一起回家吃饭然后明天带上雨伞出发去公园散步讨论后面的计划'])[0]
        parts,count=segment.segment_midpoint(item,10)
        self.assertGreater(count,1);self.assertEqual(''.join(p['text'] for p in parts),item['text'])
        self.assertTrue(all(segment.text_width(p['text'])<=10 for p in parts))

    def test_width_provenance_is_not_added_to_short_pause_or_manual_splits(self):
        item={'text':'我们回家','start':0,'end':2,'words':[
            {'word':'我们','start':0,'end':.5},{'word':'回家','start':1.5,'end':2}]}
        paused,count=segment.split_on_gaps([item],.5,20)
        self.assertEqual(count,1);self.assertFalse(any(c.get('width_split') for c in paused))
        manual,count=segment.segment_midpoint(item,20)
        self.assertEqual(count,1);self.assertFalse(any(c.get('width_split') for c in manual))
        long,count=segment.segment_midpoint(cues(['我们今天一起回家吃饭然后明天去公园散步'])[0],10)
        self.assertGreater(count,0);self.assertTrue(all(c['width_split'] for c in long))


if __name__=='__main__':unittest.main()
