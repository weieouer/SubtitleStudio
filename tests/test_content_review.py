"""内容差异与缺失项：检测只标注，自动校准不增删，人工确认保持。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core.calibration import normalize_cues
from core.reference_match import match_reference, parse_documents


def cues(texts):
    return [{'id':i+1,'text':text,'start':i*3.,'end':i*3.+1.5} for i,text in enumerate(texts)]


class ContentReviewTests(unittest.TestCase):
    def test_unmatched_script_parts_do_not_generate_blank_subtitles(self):
        doc=parse_documents([
            {'name':'当前','text':'开头缺失的对白。\n我们明天一起出发。\n中间没有识别的一句。\n请你记得带好钥匙。\n结尾还有一句台词。'},
            {'name':'未对应','text':'另一个剧本完全未对应的对白。'}])
        original=cues(['我们明天一起出发','请你记得带好钥匙']);before=deepcopy(original)
        result=match_reference(original,doc)
        self.assertEqual([c['text'] for c in result['cues']],[c['text'] for c in original])
        self.assertEqual(original,before)
        self.assertEqual(result['gaps'],[])
        self.assertTrue(all(c['text'].strip() for c in result['cues']))
        self.assertEqual([(c['start'],c['end']) for c in result['cues']],[(c['start'],c['end']) for c in original])

    def test_role_labels_narration_and_punctuation_are_not_missing_dialogue(self):
        result=match_reference(cues(['我们明天一起出发']),{'text':'第一场：公园\n甲：我们明天一起出发。'})
        self.assertEqual(result['gaps'],[])

    def test_second_previous_line_resolves_repeated_previous_anchor(self):
        first='先检查一下仓库里的备用钥匙'
        repeated='我们都已经知道了'
        misplaced='今天晚上大家一起去吃饭'
        last='请你现在把门口快递拿回来'
        doc=parse_documents([{'name':'当前','text':'。'.join([first,misplaced,repeated,last])+'。'},
                             {'name':'另处','text':repeated+'。这里的事情已经结束了。'}])
        result=match_reference(cues([first,repeated,misplaced,last]),doc)
        match=result['cues'][2]['reference_match']
        self.assertEqual(match['status'],'context_mismatch')
        self.assertIsNone(match['reference'])
        self.assertIn(first,match['context']['previous_context'])
        self.assertEqual(len(result['cues']),4)

    def test_calibration_fixes_extra_characters_but_keeps_unmatched_and_missing_items(self):
        original=cues(['我们明天去去学校','额外识别的内容','上下文不符的内容'])
        original[0]['reference_match']={'status':'extra','query_text':original[0]['text'],
                                        'reference':{'text':'我们明天去学校'}}
        for item,status in zip(original[1:],['unmatched','context_mismatch']):
            item['reference_match']={'status':status,'query_text':item['text'],'reference':None}
        before=deepcopy(original);result=normalize_cues(original)
        self.assertEqual([c['text'] for c in result['cues']],['我们明天去学校',before[1]['text'],before[2]['text']])
        self.assertEqual(result['completed_indices'],[0]);self.assertEqual(result['pending_indices'],[1,2])
        self.assertEqual(original,before)
        self.assertTrue(all(not c.get('exclude_export') for c in result['cues']))

    def test_calibration_respects_manually_approved_text(self):
        item=cues(['人工确认保留的识别'])[0]
        item.update(edited=True,review_decision='manual',modification_complete={'text':item['text']})
        item['reference_match']={'status':'typo','reference':{'text':'旧参考不应覆盖人工判断'}}
        self.assertEqual(normalize_cues([item])['cues'][0],item)

    def test_punctuation_cleanup_does_not_approve_an_unmatched_cue(self):
        item=cues(['没有对应参考。'])[0]
        item['reference_match']={'status':'unmatched','query_text':item['text'],'reference':None}
        result=normalize_cues([item],keep_punc=False)
        self.assertEqual(result['pending_indices'],[0]);self.assertEqual(result['completed_indices'],[])
        self.assertFalse(result['cues'][0]['modification_complete'])


if __name__=='__main__':unittest.main()
