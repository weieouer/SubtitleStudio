"""语气词识别与去除：整条删除，句首/句末清理，句中正常语气词保留。"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core.interjections import (
    drop_interjections, is_interjection, mark_interjections,
    strip_boundary_interjections, strip_interjection_cues,
)


def cue(text, ident=1):
    return {'id': ident, 'text': text, 'start': 0.0, 'end': 1.0}


class InterjectionTests(unittest.TestCase):
    def test_standalone_laughter_sigh_and_disdain_are_dropped(self):
        for text in ('哈哈', '哈哈哈！', '呵呵', '嘿嘿', '哼哼', '唉……', '哎',
                     '切', '啧', '哼！', '噗嗤', '唉声叹气', '（笑声）', '【叹气】'):
            with self.subTest(text=text):
                self.assertTrue(is_interjection(text))

    def test_dialogue_particles_inside_a_sentence_are_not_whole_interjections(self):
        for text in ('你呀，就是太心软了', '真的吗？', '我们走吧', '我的天哪呢',
                     '哈哈，你真会说笑', '唉，别提了', '走啊，快点'):
            with self.subTest(text=text):
                self.assertFalse(is_interjection(text))

    def test_single_particles_are_interjections_but_meaningful_feed_is_kept(self):
        # 单独的“啊/哦/呀/哼/呵/嗯？”整条删除；接电话的“喂”仍算正常对白。
        for text in ('啊？', '哦。', '呀', '嗯？', '哼', '呵'):
            with self.subTest(text=text):
                self.assertTrue(is_interjection(text))
        self.assertFalse(is_interjection('喂'))

    def test_repeated_weak_particles_become_interjections(self):
        self.assertTrue(is_interjection('啊啊'))
        self.assertTrue(is_interjection('啊啊啊'))
        self.assertTrue(is_interjection('哦哦哦'))

    def test_empty_and_punctuation_only_cues_are_not_interjections(self):
        for text in ('', '   ', '……', '——', '。'):
            with self.subTest(text=text):
                self.assertFalse(is_interjection(text))

    def test_boundary_particles_are_stripped_from_dialogue(self):
        cases = {
            '啊，我们走吧': '我们走吧',
            '我们走吧啊': '我们走吧',
            '我们走吧。啊！': '我们走吧！',
            '我们走吧，啊！': '我们走吧！',
            '走吧，啊？': '走吧？',
            '走吧，啊，快点': '走吧，快点',
            '嗯？': '',
            '哈哈，你真会说笑': '你真会说笑',
            '唉，别提了': '别提了',
            # 句中语气词、引号里的对白、会截断正常词的情况保持不动。
            '你呀，就是太心软了': '你呀，就是太心软了',
            '走啊，快点': '走啊，快点',
            '他说：“啊！”': '他说：“啊！”',
            '哈哈镜，真有趣': '哈哈镜，真有趣',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(strip_boundary_interjections(text), expected)

    def test_strip_cues_deletes_whole_interjections_and_trims_boundaries(self):
        cues = [cue('哈哈哈', 1), cue('你呀，快走', 2), cue('我们走吧啊', 3), cue('嗯？', 4)]
        kept, removed, trimmed = strip_interjection_cues(cues)
        self.assertEqual([item['id'] for item in kept], [2, 3])
        self.assertEqual([item['text'] for item in kept], ['你呀，快走', '我们走吧'])
        self.assertEqual((removed, trimmed), (2, 1))
        self.assertTrue(kept[1]['edited'])
        self.assertFalse(kept[1]['modification_complete'])

    def test_drop_returns_kept_cues_and_summaries(self):
        cues = [cue('哈哈哈', 1), cue('你呀，快点', 2), cue('唉', 3)]
        kept, dropped = drop_interjections(cues)
        self.assertEqual([item['id'] for item in kept], [2])
        self.assertEqual(dropped, [{'id': 1, 'text': '哈哈哈'}, {'id': 3, 'text': '唉'}])

    def test_mark_keeps_all_cues_and_flags_whole_interjections(self):
        cues = [cue('哈哈哈', 1), cue('你呀，快点', 2), cue('唉', 3)]
        flagged = mark_interjections(cues)
        self.assertEqual([item['id'] for item in flagged], [1, 3])
        self.assertEqual([item['interjection'] for item in cues], [True, False, True])
        self.assertEqual([item['text'] for item in cues], ['哈哈哈', '你呀，快点', '唉'])


if __name__ == '__main__':
    unittest.main()
