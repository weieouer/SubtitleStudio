"""字幕时长上限与语音活动收紧；不运行模型、不读真实素材。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core import audio_boundaries
from core.duration import duration_bounds, spoken_length, trim_to_speech


class DurationBoundsTests(unittest.TestCase):
    def test_spoken_length_ignores_punctuation_and_spaces(self):
        self.assertEqual(spoken_length('你好，世界！'), 4)
        self.assertEqual(spoken_length(' OK 3. '), 3)
        self.assertEqual(spoken_length('。！？'), 0)

    def test_bounds_grow_with_length_and_have_a_floor(self):
        short = duration_bounds('好')
        self.assertEqual(short[0], 1)
        self.assertAlmostEqual(short[2], 1.45)
        longer = duration_bounds('我们明天去学校')
        self.assertEqual(longer[0], 7)
        self.assertGreater(longer[1], short[1])
        self.assertGreater(longer[2], short[2])
        self.assertGreaterEqual(longer[2], longer[1])

    def test_empty_text_has_no_bounds(self):
        self.assertEqual(duration_bounds(''), (0, 0.0, 0.0))


class TrimToSpeechTests(unittest.TestCase):
    def test_picks_the_speech_run_under_the_words_and_ignores_the_effect(self):
        _, target, cap = duration_bounds('走')
        runs = [(1.0, 1.4), (4.0, 4.6)]
        words = [{'start': .9, 'end': 4.7}]
        start, end = trim_to_speech(runs, words, (0., 6.), target, cap)
        self.assertAlmostEqual(start, 3.98)
        self.assertAlmostEqual(end, 4.62)

    def test_refuses_when_words_are_split_across_distant_runs(self):
        _, target, cap = duration_bounds('走吧')
        runs = [(1.0, 1.4), (3.0, 3.4)]
        words = [{'start': .9, 'end': 1.2}, {'start': 3.1, 'end': 3.3}]
        self.assertIsNone(trim_to_speech(runs, words, (0., 4.), target, cap))

    def test_refuses_when_the_only_speech_run_is_itself_too_long(self):
        _, target, cap = duration_bounds('好')
        self.assertIsNone(trim_to_speech([(0.0, 5.0)], [{'start': 0., 'end': 4.9}],
                                         (0., 5.), target, cap))

    def test_nearby_runs_merge_and_no_words_means_no_trim(self):
        _, target, cap = duration_bounds('好吧')
        runs = [(1.0, 1.4), (1.5, 1.9)]
        words = [{'start': 1.0, 'end': 1.9}]
        start, end = trim_to_speech(runs, words, (1., 2.), target, cap)
        self.assertAlmostEqual(start, .98)
        self.assertAlmostEqual(end, 1.92)
        self.assertIsNone(trim_to_speech(runs, [], (1., 2.), target, cap))


class CapDurationsTests(unittest.TestCase):
    def test_short_cue_pulled_by_effect_is_trimmed_to_speech(self):
        cue = {'text': '好', 'start': 2.0, 'end': 8.0,
               'original_start': 2.0, 'original_end': 8.0,
               'words': [{'word': '好', 'start': 2.0, 'end': 7.8}],
               'alignment': {'engine': 'fixture'}}
        with patch.object(audio_boundaries, '_speech_runs', return_value=[(5.0, 5.6)]):
            repairs = audio_boundaries.cap_durations([cue], [0.0] * (16000 * 10))
        self.assertEqual(len(repairs), 1)
        self.assertAlmostEqual(cue['start'], 4.98)
        self.assertAlmostEqual(cue['end'], 5.62)
        self.assertEqual(cue['duration_trim']['method'], 'speech_activity')
        self.assertTrue(cue['alignment']['duration_trimmed'])
        self.assertTrue(cue['needs_review'])

    def test_without_speech_evidence_falls_back_to_input_time(self):
        cue = {'text': '好', 'start': 0.0, 'end': 6.0,
               'original_start': 0.0, 'original_end': 1.2,
               'words': [{'word': '好', 'start': 0.0, 'end': 5.9}]}
        with patch.object(audio_boundaries, '_speech_runs', return_value=[]):
            repairs = audio_boundaries.cap_durations([cue], [0.0] * (16000 * 10))
        self.assertEqual(repairs[0]['method'], 'input_fallback')
        self.assertEqual((cue['start'], cue['end']), (0.0, 1.2))
        self.assertEqual(cue['words'], [])
        self.assertIn('字幕长度', cue['alignment_error'])

    def test_long_input_is_marked_but_not_cut_without_evidence(self):
        cue = {'text': '好', 'start': 0.0, 'end': 6.0,
               'original_start': 0.0, 'original_end': 6.0, 'words': []}
        with patch.object(audio_boundaries, '_speech_runs', return_value=[]):
            repairs = audio_boundaries.cap_durations([cue], [0.0] * (16000 * 10))
        self.assertEqual(repairs, [])
        self.assertEqual((cue['start'], cue['end']), (0.0, 6.0))
        self.assertEqual(cue['duration_trim']['method'], 'unresolved')
        self.assertTrue(cue['needs_review'])

    def test_plausible_duration_is_untouched(self):
        cue = {'text': '好', 'start': 0.0, 'end': 1.2, 'words': []}
        with patch.object(audio_boundaries, '_speech_runs', side_effect=AssertionError('不应探测')):
            audio_boundaries.cap_durations([cue], [0.0] * (16000 * 10))
        self.assertNotIn('duration_trim', cue)

    def test_unresolved_long_words_are_disabled_before_further_segmentation(self):
        cue = {'text': '四个汉字', 'start': 730.08, 'end': 812.96,
               'original_start': 727.71, 'original_end': 812.75,
               'words': [{'word': '四个汉字', 'start': 730.08, 'end': 812.96}],
               'alignment': {'engine': 'fixture'}, 'pause_after': 812.96}
        with patch.object(audio_boundaries, '_speech_runs', return_value=[]):
            audio_boundaries.cap_durations([cue], [0.0] * 16000, input_times=[(727.71, 812.75)])
        self.assertEqual((cue['start'], cue['end']), (730.08, 812.96))
        self.assertEqual(cue['words'], [])
        self.assertTrue(cue['alignment']['stale'])
        self.assertIn('未能定位', cue['alignment_error'])
        self.assertNotIn('pause_after', cue)

    def test_short_cue_placed_on_effect_is_reanchored_to_speech_near_input(self):
        cue = {'text': '走吧', 'start': 0.5, 'end': 1.8,
               'original_start': 5.0, 'original_end': 6.5,
               'words': [{'word': '走吧', 'start': 0.5, 'end': 1.7}]}
        with patch.object(audio_boundaries, '_speech_runs', return_value=[(5.1, 6.4)]):
            repairs = audio_boundaries.cap_durations([cue], [0.0] * (16000 * 10))
        self.assertEqual(repairs[0]['method'], 'speech_reanchor')
        self.assertAlmostEqual(cue['start'], 5.08)
        self.assertAlmostEqual(cue['end'], 6.42)

    def test_legitimately_shifted_cue_is_not_moved(self):
        cue = {'text': '走吧', 'start': 5.0, 'end': 6.5,
               'original_start': 0.5, 'original_end': 1.8,
               'words': [{'word': '走吧', 'start': 5.0, 'end': 6.4}]}
        with patch.object(audio_boundaries, '_speech_runs', return_value=[(5.0, 6.5)]):
            audio_boundaries.cap_durations([cue], [0.0] * (16000 * 10))
        self.assertEqual((cue['start'], cue['end']), (5.0, 6.5))
        self.assertNotIn('duration_trim', cue)


if __name__ == '__main__':
    unittest.main()
