"""Whisper 幻觉字幕：只标记供人工复核，不自动排除；文字、时间和导出范围都保留。"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core.hallucination import analyze, mark


def cue(text, start=0.0, end=1.0, **more):
    return {'id': 1, 'text': text, 'start': start, 'end': end, **more}


class HallucinationTests(unittest.TestCase):
    def test_training_boilerplate_split_into_three_cues_is_marked(self):
        cues = [cue('请不舍点赞 订阅', 0, 54.17),
                cue('转发 打赏支持明镜与', 54.17, 73.58),
                cue('点点栏目', 73.58, 138.43)]
        marked = mark(cues)
        self.assertEqual(len(marked), 3)
        for item in cues:
            self.assertTrue(item['is_noise'])
            self.assertTrue(item['needs_review'])
            self.assertFalse(item.get('exclude_export'))
            self.assertTrue(item['hallucination']['confirmed'])
            self.assertEqual(item['text'], item['text'].strip())
        self.assertIn('known-text', cues[0]['hallucination']['reasons'])
        self.assertIn('sparse', cues[0]['hallucination']['reasons'])

    def test_normal_dialogue_is_untouched(self):
        cues = [cue('谢谢观看', 0, 1.2), cue('我们明天去公园散步吧', 2, 4.5),
                cue('千万别忘了带钥匙', 5, 7)]
        self.assertEqual(mark(cues), [])
        self.assertFalse(any('exclude_export' in item for item in cues))
        self.assertFalse(any('hallucination' in item for item in cues))

    def test_dense_line_with_common_word_is_not_marked(self):
        # 只命中常见词、时长和字数正常时，单一信号不足以下结论。
        self.assertIsNone(analyze(cue('谢谢观看', 0, 1.0)))
        self.assertIsNone(analyze(cue('我要订阅这个频道', 0, 2.0)))

    def test_mid_density_suspect_is_flagged_but_still_exported(self):
        # 8 秒 5 个字 + 模型判定非人声：提示人工确认，但不自动排除，避免误删真实台词。
        item = cue('王爷发怒了', 0, 8.6, no_speech_prob=.58)
        info = analyze(item)
        self.assertIsNotNone(info)
        self.assertFalse(info['confirmed'])
        self.assertEqual(mark([item]), [])
        self.assertFalse(item.get('exclude_export'))
        self.assertFalse(item.get('is_noise'))
        self.assertTrue(item['needs_review'])

    def test_extreme_sparse_line_is_flagged_but_not_excluded(self):
        item = cue('肃经身啊', 727.7, 812.8)
        self.assertTrue(analyze(item)['confirmed'])
        self.assertEqual(mark([item]), [item])
        self.assertFalse(item.get('exclude_export'))
        self.assertTrue(item['is_noise'])

    def test_long_sparse_line_without_no_speech_is_not_confirmed(self):
        self.assertIsNone(analyze(cue('嗯', 0, 10)))
        suspect = analyze(cue('嗯', 0, 10, no_speech_prob=.55))
        self.assertIsNotNone(suspect)
        self.assertFalse(suspect['confirmed'])

    def test_strong_phrase_alone_confirms_on_long_span(self):
        info = analyze(cue('字幕由某某字幕组制作', 0, 8))
        self.assertIsNotNone(info)
        self.assertTrue(info['confirmed'])
        self.assertIn('known-text', info['reasons'])
        self.assertTrue(analyze(cue('点点栏目', 0, 5))['confirmed'])

    def test_marked_cue_is_not_reflagged_after_user_restores_export(self):
        item = cue('请不吝点赞 订阅', 0, 54.17)
        mark([item])
        item['exclude_export'] = False
        item['review_decision'] = 'included'
        self.assertEqual(mark([item]), [])
        self.assertFalse(item['exclude_export'])

    def test_three_identical_consecutive_cues_are_flagged_but_still_exported(self):
        cues = [cue('请不吝点赞', 0, 2), cue('请不吝点赞', 2, 4), cue('请不吝点赞', 4, 6)]
        self.assertEqual(mark(cues), [])
        for item in cues:
            self.assertTrue(item['needs_review'])
            self.assertEqual(item['hallucination']['reasons'], ['repeated-text'])
            self.assertFalse(item['hallucination']['confirmed'])
            self.assertFalse(item.get('exclude_export'))
            self.assertFalse(item.get('is_noise'))

    def test_two_identical_cues_are_not_treated_as_a_loop(self):
        cues = [cue('我们走吧', 0, 1), cue('我们走吧', 1, 2)]
        self.assertEqual(mark(cues), [])
        self.assertFalse(any('hallucination' in item for item in cues))

    def test_legacy_auto_exclusion_is_released_but_manual_non_export_stays(self):
        auto = cue('请不吝点赞 订阅', 0, 54.17, exclude_export=True,
                   hallucination={'confirmed': True, 'automatic_exclusion': True, 'reasons': ['known-text']})
        manual = cue('手动不导出', 60, 61, exclude_export=True, review_decision='non_script')
        mark([auto, manual])
        self.assertFalse(auto['exclude_export'])
        self.assertFalse(auto['hallucination']['automatic_exclusion'])
        self.assertTrue(manual['exclude_export'])

    def test_original_text_and_times_are_preserved(self):
        item = cue('请不吝点赞 订阅 转发 打赏支持明镜与点点栏目', 0, 120)
        before = dict(item)
        mark([item])
        self.assertEqual((item['text'], item['start'], item['end']),
                         (before['text'], before['start'], before['end']))


if __name__ == '__main__':
    unittest.main()
