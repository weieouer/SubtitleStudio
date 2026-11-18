"""对齐文字映射：中文数字按原文逐字对应，不因整句数字折算误判增删字。"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core import alignment  # noqa: E402


def words(*items):
    return [{'text': t, 'start': s, 'end': e} for t, s, e in items]


class AlignmentTextMappingTests(unittest.TestCase):
    def test_chinese_numerals_map_per_character_without_false_insertion(self):
        # 真实素材：整句“十万”折算为 100000，对齐器逐字返回“十”“万”，旧逻辑误判为增删字而失败。
        cues = [{'text': '十万神风军都挡不住', 'start': 71.5, 'end': 73.8}]
        chars = '十万神风军都挡不住'
        alignment.apply_word_times(cues, [0], words(*[(c, 71.64 + i * .2, 71.64 + (i + 1) * .2) for i, c in enumerate(chars)]))
        self.assertNotIn('alignment_error', cues[0])
        self.assertEqual(''.join(w['word'] for w in cues[0]['words']), chars)
        self.assertEqual(cues[0]['start'], 71.64)


if __name__ == '__main__':
    unittest.main()
