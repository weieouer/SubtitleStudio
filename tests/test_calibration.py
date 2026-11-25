"""校准：只复制已确认的参考文字，不改结构、时间、删除状态或原始记录。"""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core import calibration, srt_io


def cue(text, start=0, end=1, **extra):
    return dict(text=text, start=start, end=end, **extra)


def reference(text, start=0, end=10):
    return {'source_id': 'A', 'start': start, 'end': end,
            'paragraph_start': 1, 'paragraph_end': 1, 'text': text}


def with_match(text, reference_text=None, status='high', **extra):
    item = cue(text)
    item['reference_match'] = {'status': status, 'query_text': text,
                               'reference': reference(reference_text if reference_text is not None else text),
                               **extra}
    return item


class ReferenceCopyTests(unittest.TestCase):
    def test_weak_per_cue_match_cannot_be_overridden_by_group_match(self):
        item=with_match('当前字幕','不可靠的参考',status='typo',score=.35,minimum_used=.64,
                        group={'score':.99})
        result=calibration.normalize_cues([item])
        self.assertEqual(result['cues'][0]['text'],'当前字幕')
        self.assertEqual(result['cues'][0]['reference_match']['status'],'weak')
        self.assertIn('证据不足',result['cues'][0]['reference_match']['warning'])
        self.assertEqual(result['pending_indices'],[0])
        self.assertEqual(result['completed_indices'],[])

    def test_no_candidate_retains_its_diagnosis_after_calibration(self):
        for status in ('unmatched', 'context_mismatch'):
            item = cue('需试听', reference_match={'status': status, 'reference': None,
                       'score': 0., 'minimum_used': .88, 'warning': '此处无台词'})
            out = calibration.normalize_cues([item])['cues'][0]
            self.assertEqual(out['reference_match'], item['reference_match'])

    def test_explicit_candidate_confirmation_can_resolve_weak_match(self):
        item=with_match('原句','人工确认的参考',status='ambiguous',score=.35,minimum_used=.64,
                        candidate_confirmed=True)
        result=calibration.normalize_cues([item])
        self.assertEqual(result['cues'][0]['text'],'人工确认的参考')
        self.assertEqual(result['completed_indices'],[0])

    def test_copies_confirmed_reference_text_and_preserves_time(self):
        words = [{'word': '你先走', 'start': 1, 'end': 2}]
        item = cue('你先走我来处里', 1, 3, words=words,
                   original_text='你先走我来处理', original_start=1, original_end=3,
                   alignment={'stale': False, 'method': 'qwen'})
        item['reference_match'] = {
            'status': 'typo', 'query_text': '你先走我来处里',
            'reference': reference('你先走，我来处理。')}
        before = copy.deepcopy(item)

        result = calibration.normalize_cues([item], keep_punc=True)
        out = result['cues'][0]

        self.assertEqual(result['mode'], 'reference_copy')
        self.assertEqual(out['text'], '你先走我来处理')
        self.assertEqual((out['start'], out['end']), (1, 3))
        self.assertEqual(out['original_text'], '你先走我来处理')
        self.assertEqual((out['original_start'], out['original_end']), (1, 3))
        self.assertEqual(out['words'], [])
        self.assertTrue(out['edited'])
        self.assertTrue(out['alignment']['stale'])
        self.assertEqual(out['script_text'], '你先走，我来处理。')
        self.assertEqual(out['reference_match']['query_text'], out['text'])
        self.assertEqual(out['reference_match']['status'], 'high')
        self.assertFalse(out['match_needs_review'])
        self.assertEqual(out['verification_stage'], 'calibrate')
        self.assertEqual(result['changed_indices'], [0])
        self.assertEqual(result['completed_indices'], [0])
        self.assertEqual(result['pending_indices'], [])
        self.assertNotIn('splits', result)
        self.assertNotIn('merged', result)
        self.assertNotIn('merge_suggestions', result)
        self.assertEqual(item, before, '不得修改调用方传入的条目')

    def test_each_confirmed_status_copies_reference(self):
        for status in ('high', 'typo', 'homophone', 'missing', 'extra'):
            with self.subTest(status=status):
                item = with_match('参考文字', '正确的参考文字。', status=status)
                out = calibration.normalize_cues([item])['cues'][0]
                self.assertEqual(out['text'], '正确的参考文字')

    def test_confirmed_ambiguous_requires_user_confirmation(self):
        confirmed = with_match('参考', '参考文字。', status='ambiguous', candidate_confirmed=True)
        self.assertEqual(calibration.normalize_cues([confirmed])['cues'][0]['text'], '参考文字')
        unconfirmed = with_match('参考', '参考文字。', status='ambiguous')
        out = calibration.normalize_cues([unconfirmed])
        self.assertEqual(out['cues'][0]['text'], '参考')
        self.assertEqual(out['completed_indices'], [])
        self.assertEqual(out['pending_indices'], [0])

    def test_unreliable_matches_are_left_for_review(self):
        cases = {
            'unmatched': with_match('原字幕', '别处台词', status='unmatched'),
            'context_mismatch': with_match('原字幕', '别处台词', status='context_mismatch'),
            'stale': with_match('原字幕', '别处台词', stale=True),
        }
        reference_only = with_match('原字幕', '别处台词')
        reference_only['reference_match']['reference']['reference_only'] = True
        cases['reference_only'] = reference_only
        stale_query = with_match('改过的字幕', '参考文字')
        stale_query['reference_match']['query_text'] = '改之前'
        cases['query_mismatch'] = stale_query
        for name, item in cases.items():
            with self.subTest(case=name):
                result = calibration.normalize_cues([item])
                self.assertEqual(result['cues'][0]['text'], '原字幕' if name != 'query_mismatch' else '改过的字幕')
                self.assertEqual(result['changed_indices'], [])
                self.assertEqual(result['pending_indices'], [0])

    def test_reference_fingerprint_mismatch_blocks_copy(self):
        item = with_match('原字幕', '参考文字。')
        item['reference_match']['reference_fingerprint'] = 'aaaa'
        blocked = calibration.normalize_cues([item], reference_fingerprint='bbbb')
        self.assertEqual(blocked['cues'][0]['text'], '原字幕')
        self.assertEqual(blocked['pending_indices'], [0])
        allowed = calibration.normalize_cues([item], reference_fingerprint='aaaa')
        self.assertEqual(allowed['cues'][0]['text'], '参考文字')

    def test_never_adds_or_removes_cues(self):
        items = [with_match('我们', '我们', status='high'),
                 with_match('走吧', '走吧', status='high')]
        second = items[1]
        second['reference_match'] = {
            'status': 'missing', 'query_text': '走吧',
            'reference': reference('走吧，明天见。')}
        result = calibration.normalize_cues(items)
        self.assertEqual(len(result['cues']), 2)
        self.assertEqual([c['start'] for c in result['cues']], [0, 0])
        self.assertNotIn('merge_suggestion', result['cues'][0])

    def test_does_not_restore_deleted_or_reference_only_sentences(self):
        item = cue('', 0, 1, deleted=True)
        item['reference_match'] = {
            'status': 'missing', 'query_text': '',
            'reference': reference('被删掉的句子', start=3, end=9)}
        result = calibration.normalize_cues([item])
        self.assertEqual(result['cues'][0]['text'], '')
        self.assertEqual(len(result['cues']), 1)

    def test_does_not_delete_interjections_or_change_flags(self):
        item = with_match('哈哈哈', '哈哈哈')
        item['interjection'] = True
        result = calibration.normalize_cues([item])
        self.assertEqual(result['cues'][0]['text'], '哈哈哈')
        self.assertTrue(result['cues'][0]['interjection'])
        self.assertNotIn('interjection_removed', result)

    def test_already_matching_reference_is_completed_but_not_changed(self):
        item = with_match('参考一致', '参考一致。')
        result = calibration.normalize_cues([item])
        self.assertEqual(result['changed_indices'], [])
        self.assertEqual(result['completed_indices'], [0])
        self.assertEqual(result['cues'][0]['text'], '参考一致')
        self.assertEqual(result['cues'][0]['verification_stage'], 'calibrate')


class PunctuationTests(unittest.TestCase):
    def test_keep_punctuation_preserves_reference_markup(self):
        item = with_match('“你好世界”', '“你好，世界！”')
        out = calibration.normalize_cues([item], keep_punc=True)['cues'][0]
        self.assertEqual(out['text'], '你好世界！')
        self.assertEqual(out['reference_match']['reference']['text'], '“你好，世界！”')

    def test_strip_punctuation_applies_unified_rules_and_keeps_raw_reference(self):
        item = with_match('“你好世界”3.5%', '“你好，世界！” 3.5% 12:30 1,000.5 well-known，')
        out = calibration.normalize_cues([item], keep_punc=False)['cues'][0]
        self.assertEqual(out['text'], '你好世界 3.5% 12:30 1,000.5 well-known')
        self.assertEqual(out['reference_match']['query_text'], out['text'])
        self.assertEqual(out['script_text'], '“你好，世界！” 3.5% 12:30 1,000.5 well-known，')
        self.assertEqual(out['reference_match']['reference']['text'],
                         '“你好，世界！” 3.5% 12:30 1,000.5 well-known，')

    def test_strip_punctuation_also_cleans_unconfirmed_work_text(self):
        item = cue('原句。')
        out = calibration.normalize_cues([item], keep_punc=False)['cues'][0]
        self.assertEqual(out['text'], '原句')
        self.assertTrue(out['edited'])

    def test_calibration_never_splits_or_merges(self):
        result = calibration.normalize_cues([cue('你先回家，我随后就到！')])
        self.assertEqual([c['text'] for c in result['cues']], ['你先回家我随后就到！'])
        self.assertEqual(len(result['cues']), 1)


class ExportTests(unittest.TestCase):
    def test_default_export_preserves_reference_punctuation(self):
        text = '“你好，世界。” 3.5% 12:30 1,000.5 well-known，'
        self.assertEqual(srt_io.normalize_subtitle_punctuation(text), '你好世界 3.5% 12:30 1,000.5 well-known')
        self.assertEqual(srt_io.prepare_export_cues([cue(text)])[0]['text'], '你好世界 3.5% 12:30 1,000.5 well-known')

    def test_punctuation_off_export_uses_strip_rules_and_protects_numbers(self):
        self.assertEqual(srt_io.prepare_export_cues(
            [cue('你先走，我来处理！')], keep_punctuation=False)[0]['text'], '你先走我来处理')
        self.assertEqual(srt_io.strip_subtitle_punctuation('3.5% 12:30 1,000.5 well-known'),
                         '3.5% 12:30 1,000.5 well-known')

    def test_end_punctuation_never_invents_marks(self):
        self.assertEqual(srt_io.add_end_punctuation('我们回家。'), '我们回家')
        self.assertEqual(srt_io.add_end_punctuation('我们回家'), '我们回家')
        self.assertEqual(srt_io.add_end_punctuation('你好吗'), '你好吗？')
        self.assertEqual(srt_io.add_end_punctuation('走吧'), '走吧！')


if __name__ == '__main__':
    unittest.main()
