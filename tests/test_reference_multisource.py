"""来源保底、歧义、索引复用及分段继承的专项行为。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from core.reference_match import parse_documents, match_reference, inherit_reference, _index_features
from core import segment

def cues(texts):return [{'text':text,'start':i*2.,'end':i*2.+1.5} for i,text in enumerate(texts)]

class MultisourceTests(unittest.TestCase):
    def test_five_shuffled_sources_keep_the_correct_source(self):
        texts=['明天带上雨伞去公园散步','今天晚上一起回家吃饭','这件事情我会认真处理','请把门口的快递拿回来','我们下午一起研究方案']
        order=[3,1,4,0,2]
        doc=parse_documents([{'name':str(i),'text':texts[i]+'。'} for i in order])
        result=match_reference(cues(texts),doc)
        self.assertEqual(result['stats']['matched'],5)
        self.assertEqual([c['reference_match']['reference']['source_name'] for c in result['cues']],list(map(str,range(5))))

    def test_shared_short_line_is_ambiguous_without_context(self):
        doc=parse_documents([{'name':'甲','text':'走吧。'},{'name':'乙','text':'走吧。'}])
        match=match_reference(cues(['走吧']),doc)['cues'][0]['reference_match']
        self.assertEqual(match['status'],'ambiguous')
        self.assertEqual({match['reference']['source_name'],*(c['source_name'] for c in match['alternatives'])},{'甲','乙'})

    def test_width_split_inherits_nonoverlapping_reference_ranges(self):
        text='我们明天去公园散步'
        parent=match_reference(cues([text]),parse_documents([{'name':'甲','text':text+'。'}]))['cues'][0]
        with patch.object(segment,'split_text',return_value=[text[:4],text[4:]]):parts,_=segment.segment_cue(parent,6)
        self.assertTrue(all(c['reference_match']['inherited'] for c in parts))
        self.assertEqual(parts[0]['reference_match']['reference']['end'],parts[1]['reference_match']['reference']['start'])

    def test_cross_source_parent_cannot_be_inherited(self):
        parent={'text':'甲乙丙丁','reference_match':{'reference':{'text':'甲乙丙丁','start':0,'end':4,'source_range':[0,2]}}}
        self.assertFalse(inherit_reference(parent,[{'text':'甲乙'},{'text':'丙丁'}]))

    def test_index_reused_even_with_changed_options(self):
        _index_features.cache_clear()
        doc=parse_documents([{'name':'甲','text':'我们明天去公园散步。'}])
        first=match_reference(cues(['我们明天去公园散步']),doc)
        second=match_reference(cues(['我们明天去公园散步']),doc,{'minimum':.7})
        self.assertFalse(first['engine']['index_cache_hit']);self.assertTrue(second['engine']['index_cache_hit'])
        self.assertEqual(_index_features.cache_info().misses,1)

    def test_known_cross_source_group_is_not_recomputed(self):
        doc=parse_documents([{'name':'甲','text':'我们明天去公园散步。'},{'name':'乙','text':'今天晚上一起回家吃饭。'}])
        first=match_reference(cues(['我们明天去公园散步','今天晚上一起回家吃饭']),doc)
        second=match_reference(first['cues'],doc)
        self.assertLess(second['engine']['candidate_queries'],first['engine']['candidate_queries'])
        self.assertEqual(second['stats']['matched'],2)

    def test_repeated_line_can_advance_past_first_candidate_page(self):
        text='我们明天一起去公园散步'
        doc=parse_documents([{'name':'甲','text':('\n'.join([text+'。']*40))}])
        result=match_reference(cues([text]*40),doc,{'max_group':1})
        self.assertEqual(result['stats']['matched'],40)
        spans=[c['reference_match']['reference']['start'] for c in result['cues']]
        self.assertEqual(len(set(spans)),40)

if __name__=='__main__':unittest.main()
