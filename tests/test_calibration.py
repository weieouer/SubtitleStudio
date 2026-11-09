"""校准后的完整词/句子和标点回归；临时字典，不改用户工程。"""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core import calibration, srt_io


def cue(text, start=0, end=1, **extra):
    return dict(text=text, start=start, end=end, **extra)


def reference(text, start, end):
    return {'status': 'high', 'query_text': text, 'reference': {
        'source_id': 'A', 'start': start, 'end': end, 'paragraph_start': 1, 'paragraph_end': 1}}


def with_reference_text(match, text):
    match['reference']['text'] = text
    return match


class CalibrationTests(unittest.TestCase):
    def test_emotional_ellipsis_is_preserved_but_not_a_sentence_cut(self):
        for text in ('我……真的不知道。', '我...真的不知道。'):
            result = calibration.normalize_cues([cue(text)], 20, 0)
            self.assertEqual([c['text'] for c in result['cues']], ['我……真的不知道'])
        self.assertEqual(srt_io.prepare_export_cues([cue('你……好吗？')], keep_punctuation=False)[0]['text'], '你好吗')

    def test_two_character_fragment_is_marked_for_manual_merge(self):
        original={'text':'我们从来没有见过他','start':0,'end':2,'id':1}
        result=calibration.normalize_cues([cue('我们',0,.3,asr_original=original),cue('从来没有见过他',.3,2,asr_original=original)],20,.4)
        self.assertEqual([c['text'] for c in result['cues']],['我们','从来没有见过他'])
        self.assertEqual(result['merged'],0)
        self.assertEqual(result['merge_suggestions'],1)
        self.assertEqual(result['cues'][0]['merge_suggestion']['direction'],'next')

    def test_single_character_is_marked_for_merge_without_changing_time_or_asr(self):
        items = [cue('中', 0, .2), cue('毒的人', .2, 1)]
        before = copy.deepcopy(items)
        result = calibration.normalize_cues(items, 10, .4)
        self.assertEqual([c['text'] for c in result['cues']], ['中', '毒的人'])
        self.assertEqual(result['merged'],0)
        self.assertEqual(result['merge_suggestions'],1)
        self.assertEqual(result['cues'][0]['merge_suggestion']['direction'],'next')
        self.assertNotIn('merge_suggestion',result['cues'][1])
        self.assertEqual(items, before)

    def test_choose_belonging_word_instead_of_previous_sentence(self):
        result = calibration.normalize_cues([cue('他是', 0, .5), cue('中', .5, .7), cue('毒的人', .7, 1.5)], 10, .4)
        self.assertEqual([c['text'] for c in result['cues']], ['他是', '中', '毒的人'])
        self.assertEqual(result['suggested_indices'],[1])
        self.assertEqual(result['cues'][1]['merge_suggestion']['direction'],'next')

    def test_same_sentence_reference_auto_merges_single_character_subject(self):
        a, b = cue('我', 0, .2), cue('从来没有见过他', .2, 1)
        a['reference_match'] = with_reference_text(reference(a['text'], 0, 1), a['text'])
        b['reference_match'] = with_reference_text(reference(b['text'], 1, 8), b['text'])
        result=calibration.normalize_cues([a, b], 12, .4)
        self.assertEqual([c['text'] for c in result['cues']], ['我从来没有见过他'])
        self.assertEqual(result['merged'],1)
        self.assertEqual(result['merge_suggestions'],0)

    def test_same_sentence_auto_merge_respects_pause_width_and_reference_sentence(self):
        def pair(left_ref, right_ref, gap=.1, width=20):
            a, b = cue('我们', 0, .5), cue('走吧', .5+gap, 1.5)
            a['reference_match'] = with_reference_text(reference(a['text'], 0, 1), left_ref)
            b['reference_match'] = with_reference_text(reference(b['text'], 1, 2), right_ref)
            return calibration.normalize_cues([a, b], width, .4)
        # 同一句、停顿未达阈值、宽度允许：自动并回一条。
        merged = pair('我们', '走吧')
        self.assertEqual([c['text'] for c in merged['cues']], ['我们走吧'])
        self.assertEqual(merged['merged'], 1)
        self.assertEqual(merged['merge_suggestions'], 0)
        # 停顿达到阈值：保持分开。
        paused = pair('我们', '走吧', gap=.6)
        self.assertEqual([c['text'] for c in paused['cues']], ['我们', '走吧'])
        self.assertEqual(paused['merged'], 0)
        # 参考里是两句话：停顿未达阈值也保持分开。
        sentences = pair('我们。', '走吧')
        self.assertEqual([c['text'] for c in sentences['cues']], ['我们', '走吧'])
        self.assertEqual(sentences['merged'], 0)
        # 合并后超过每行字数：保持分开。
        wide = pair('我们', '走吧', width=3)
        self.assertEqual(wide['merged'], 0)

    def test_obey_width_pause_manual_boundary_and_export_state(self):
        for left, right, width, gap in [
            (cue('中', 0, .2), cue('毒的人', .2, 1), 3, .4),
            (cue('中', 0, .2), cue('毒的人', .4, 1), 10, .2),
            (cue('中', 0, .2, manual_split=True), cue('毒的人', .2, 1), 10, .4),
            (cue('中', 0, .2, exclude_export=True), cue('毒的人', .2, 1), 10, .4),
        ]:
            result=calibration.normalize_cues([left, right], width, gap)
            self.assertEqual(result['merged'], 0)
            self.assertEqual(result['merge_suggestions'], 0)

    def test_independent_reply_with_terminal_is_not_swallowed(self):
        result = calibration.normalize_cues([cue('好！', 0, .2), cue('我们出发', .2, 1)], 12, .4)
        self.assertEqual([c['text'] for c in result['cues']], ['好！', '我们出发'])
        self.assertEqual(result['merge_suggestions'],0)

    def test_restore_reference_punctuation_even_when_characters_already_match(self):
        item = cue('你先走我来处理', 0, 2, script_text='你先走，我来处理。')
        item['reference_match'] = reference(item['text'], 0, 10)
        result = calibration.normalize_cues([item], 20, .4)
        self.assertEqual(result['cues'][0]['text'], '你先走我来处理')
        self.assertEqual(result['changed_indices'], [0])

    def test_two_sentences_split_without_comma_join_or_spilling_into_next(self):
        words = [{'word': t, 'start': a, 'end': b} for t, a, b in
                 [('你去哪里', 0, .8), ('我去学校', 1, 1.8)]]
        original = cue('你去哪里？我去学校。', 0, 2, words=words)
        following = cue('下一条保持原样', 2.2, 3)
        result = calibration.normalize_cues([original, following], 20, 0)
        self.assertEqual([c['text'] for c in result['cues']], ['你去哪里？', '我去学校', '下一条保持原样'])
        self.assertTrue(all(not c.get('width_split') for c in result['cues']))
        self.assertEqual({k: v for k, v in result['cues'][-1].items() if k != 'interjection'}, following)
        self.assertFalse(result['cues'][-1]['interjection'])
        self.assertLessEqual(result['cues'][0]['end'], result['cues'][1]['start'])
        self.assertEqual(result['cues'][0]['end'], .8)
        self.assertEqual(result['cues'][1]['start'], 1)
        self.assertEqual(result['cues'][0]['pause_after'], .8)
        self.assertEqual(result['cues'][1]['end'], 2)

    def test_pause_splitting_belongs_to_alignment_not_calibration(self):
        words = [{'word': '你好', 'start': 0, 'end': .5},
                 {'word': '走吧', 'start': 1.5, 'end': 2}]
        result = calibration.normalize_cues([cue('你好走吧', 0, 2, words=words)], 20, .5)
        self.assertEqual([c['text'] for c in result['cues']], ['你好走吧'])
        self.assertEqual(result['splits'], 0)

    def test_width_split_drops_dangling_comma_and_preserves_word_content(self):
        result = calibration.normalize_cues([cue('你先回家，我随后就到！', 0, 3)], 6, 0)
        self.assertTrue(all(len(c['text']) <= 6 and not c['text'].endswith('，') for c in result['cues']))
        self.assertEqual(''.join(c['text'] for c in result['cues']).replace('，', ''), '你先回家我随后就到！')

    def test_uncertain_reference_does_not_add_foreign_punctuation(self):
        item = cue('原来的台词', script_text='其他人的台词！')
        item['reference_match'] = {'status': 'ambiguous', 'reference': {'text': item['script_text']}}
        self.assertEqual(calibration.normalize_cues([item])['cues'][0]['text'], item['text'])

    def test_punctuation_only_cleanup_does_not_acknowledge_missing_or_obvious_reference(self):
        item = cue('原句。', reference_match={'status': 'context_mismatch', 'query_text': '原句。'})
        result = calibration.normalize_cues([item])
        self.assertEqual(result['cues'][0]['text'], '原句')
        self.assertEqual(result['completed_indices'], [])
        self.assertFalse(result['cues'][0]['modification_complete'])

    def test_hidden_commas_and_period_do_not_cause_width_split(self):
        result = calibration.normalize_cues([cue('你先走，我来处理。')], 8, 0)
        self.assertEqual(len(result['cues']), 1)
        self.assertEqual(result['cues'][0]['text'], '你先走我来处理')

    def test_two_reference_sentences_split_when_cue_has_no_punctuation(self):
        # 参考把两句话分成两行/两句，ASR 却合成一条没有标点；校准应恢复句界并自动拆条。
        for reference_text, expected in (
                ('你去哪里？我去学校。', ['你去哪里？', '我去学校']),
                ('你去哪里\n我去学校', ['你去哪里', '我去学校'])):
            item = cue('你去哪里我去学校', 0, 2)
            item['reference_match'] = {
                'status': 'high', 'query_text': item['text'], 'stale': True,
                'reference': {'text': reference_text, 'source_id': 'A', 'start': 0,
                              'end': len(reference_text), 'paragraph_start': 1, 'paragraph_end': 2}}
            result = calibration.normalize_cues([item], 20, 0)
            with self.subTest(reference=reference_text):
                self.assertEqual([c['text'] for c in result['cues']], expected)
                self.assertEqual(result['splits'], 1)

    def test_calibration_deletes_interjections_and_cleans_sentence_edges(self):
        items = [cue('哈哈哈', 0, .5), cue('你呀，快走', .5, 1.5), cue('我们走吧啊', 1.5, 2.5)]
        result = calibration.normalize_cues(items, 20, .4)
        self.assertEqual([c['text'] for c in result['cues']], ['你呀快走', '我们走吧'])
        self.assertEqual(result['interjection_removed'], 1)
        self.assertEqual(result['interjection_trimmed'], 1)
        self.assertEqual(result['interjection_excluded'], 0)
        # 开启开关：整条语气词保留；没有可靠参考时不去句末语气词。
        kept = calibration.normalize_cues([cue('哈哈哈', 0, .5), cue('我们走吧啊', .5, 1.5)],
                                          20, .4, keep_interjections=True)
        self.assertEqual([c['text'] for c in kept['cues']], ['哈哈哈', '我们走吧啊'])
        self.assertTrue(kept['cues'][0]['interjection'])
        # 手动“加入导出”不被自动删除。
        manual = cue('哈哈', 0, .5, review_decision='included')
        still = calibration.normalize_cues([manual], 20, .4)
        self.assertEqual([c['text'] for c in still['cues']], ['哈哈'])

    def test_calibration_removes_interjections_absent_from_reliable_reference(self):
        def with_ref(text, reference_text):
            item = cue(text, 0, 1)
            item['reference_match'] = reference(reference_text, 0, len(reference_text))
            item['reference_match']['reference']['text'] = reference_text
            return item

        # 参考没有句末“啊”，开启开关也去掉。
        result = calibration.normalize_cues([with_ref('我们走吧啊', '我们走吧')], 20, .4,
                                            keep_interjections=True)
        self.assertEqual([c['text'] for c in result['cues']], ['我们走吧'])
        # 参考本身有“啊”，开启时保留。
        kept = calibration.normalize_cues([with_ref('我们走吧啊', '我们走吧啊')], 20, .4,
                                          keep_interjections=True)
        self.assertEqual([c['text'] for c in kept['cues']], ['我们走吧啊'])
        # 整条“嗯？”不在参考里，开启开关也删除。
        dropped = calibration.normalize_cues([with_ref('嗯？', '我们走吧')], 20, .4,
                                             keep_interjections=True)
        self.assertEqual(dropped['cues'], [])
        self.assertEqual(dropped['interjection_removed'], 1)

    def test_punctuation_style_matches_export_and_preserves_numbers(self):
        text = '“你好，世界。” 3.5% 12:30 1,000.5 well-known，'
        expected = '你好世界 3.5% 12:30 1,000.5 well-known'
        self.assertEqual(srt_io.normalize_subtitle_punctuation(text), expected)
        self.assertEqual(srt_io.prepare_export_cues([cue(text)])[0]['text'], expected)
        self.assertEqual(srt_io.prepare_export_cues([cue('你先走，我来处理！')], keep_punctuation=False)[0]['text'], '你先走我来处理')
        self.assertEqual(srt_io.add_end_punctuation('我们回家。'), '我们回家')
        self.assertEqual(srt_io.normalize_subtitle_punctuation('，你好吗？，\n，好的！'), '你好吗？\n好的！')
        # “标点符号”开关参与校准：关闭时连问叹号也从校准结果中去掉；对齐不处理标点。
        self.assertEqual(calibration.normalize_cues([cue('你先走！')])['cues'][0]['text'], '你先走！')
        self.assertEqual(calibration.normalize_cues([cue('你先走！')], keep_punc=False)['cues'][0]['text'], '你先走')


if __name__ == '__main__':
    unittest.main()
