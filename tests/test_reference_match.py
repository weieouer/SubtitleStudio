"""全文参考校验：保真索引、顺序、歧义和人工边界。"""
from pathlib import Path
import copy
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core.reference_match import normalized, parse_document, match_reference, options_from, sentence_ranges


def cue(text, i=0, **more):
    return {'id':i+1, 'text':text, 'start':i*2., 'end':i*2.+1.5, **more}


class ReferenceTests(unittest.TestCase):
    def test_all_punctuation_splits_reference_but_numeric_symbols_stay_internal(self):
        raw = '你好，世界！明天12:30见；价格3.5元……再见？'
        self.assertEqual([raw[a:b] for a,b in sentence_ranges(raw)],
                         ['你好，','世界！','明天12:30见；','价格3.5元……','再见？'])

    def test_missing_sentence_tail_is_not_high_fragment_match(self):
        result = self.match(['我们明天去学校学'], '我们明天去学校学习。')['cues'][0]
        self.assertEqual(result['script_text'], '我们明天去学校学习。')
        self.assertEqual(result['reference_match']['status'], 'missing')

    def match(self, texts, script, **options):
        return match_reference([cue(t,i) for i,t in enumerate(texts)], parse_document(script), options)

    def test_unicode_normalization_keeps_source_cluster_positions(self):
        raw = 'ＡＢ１２　e\u0301，ﬃ。\r\n你好'
        clean, positions = normalized(raw)
        self.assertEqual(clean, 'ab12éffi你好')
        self.assertEqual(raw[positions[4][0]:positions[4][1]], 'e\u0301')
        self.assertEqual([raw[a:b] for a,b in positions[5:8]], ['ﬃ'] * 3)

    def test_chinese_numerals_are_normalized_for_matching_only(self):
        self.assertEqual(normalized('十二个人')[0], '12个人')
        self.assertEqual(normalized('二零二四年')[0], '2024年')
        self.assertEqual(normalized('一百零五')[0], '105')
        # 口语省略按实际数量理解：一百二=120、一千三=1300。
        self.assertEqual(normalized('一百二')[0], '120')
        self.assertEqual(normalized('一千三')[0], '1300')
        self.assertEqual(normalized('一万二')[0], '12000')
        self.assertEqual(normalized('十二万')[0], '120000')
        self.assertEqual(normalized('一亿')[0], '100000000')
        # "万一""千万不要""百姓""百分之"里的字是词，不换算。
        self.assertEqual(normalized('千万不要')[0], '千万不要')
        self.assertEqual(normalized('万一')[0], '万一')
        self.assertEqual(normalized('百姓')[0], '百姓')
        self.assertEqual(normalized('百分比')[0], '百分比')
        # "一两""两三"是约数，不能当成两位数。
        self.assertEqual(normalized('一两')[0], '一两')
        self.assertEqual(normalized('两三个')[0], '两三个')
        # 小数与阿拉伯写法归一化到同一串，便于比对。
        self.assertEqual(normalized('三点五')[0], '35')
        self.assertEqual(normalized('十二点五')[0], '125')
        self.assertEqual(normalized('一点钟')[0], '1点钟')
        clean, positions = normalized('他买了十二个')
        raw = '他买了十二个'
        self.assertEqual([raw[a:b] for a, b in positions[3:5]], ['十二', '十二'])

    def test_chinese_and_arabic_numbers_match_as_high(self):
        for asr, script in [('他说十二个人来了', '他说12个人来了。'),
                            ('我要买三斤苹果', '我要买3斤苹果。'),
                            ('二零二四年开机', '2024年开机。'),
                            ('他有一百二十块钱', '他有120块钱。'),
                            ('价钱是三点五元', '价钱是3.5元。'),
                            ('花了十二万', '花了120000。')]:
            with self.subTest(asr=asr):
                result = self.match([asr], script)['cues'][0]['reference_match']
                self.assertEqual(result['status'], 'high', result)

    def test_arbitrary_script_and_original_offsets(self):
        raw = '\r\n第一场：清晨\r\n\r\n  我们明天去公园散步。\r\n没有任何角色标记。'
        result = self.match(['我们明天去公园散步'], raw)
        reference = result['cues'][0]['reference_match']['reference']
        self.assertEqual(result['document']['text'], raw)
        self.assertEqual(raw[reference['start']:reference['end']], reference['text'])
        self.assertEqual(reference['line_start'], 4)

    def test_homophone_only_suggests_and_never_changes_asr(self):
        original = [cue('我们明天去公圆散步', needs_review=True)]
        before = copy.deepcopy(original)
        result = match_reference(original, parse_document('我们明天去公园散步。'))
        matched = result['cues'][0]
        self.assertEqual(matched['reference_match']['status'], 'homophone')
        self.assertEqual(matched['text'], original[0]['text'])
        self.assertEqual((matched['start'], matched['end']), (0, 1.5))
        self.assertEqual(original, before)
        self.assertTrue(matched['needs_review'])
        self.assertEqual(matched['reference_match']['differences'][0]['asr'], '圆')

    def test_missing_extra_and_typo_categories(self):
        for asr, reference, expected in [('我们明天学校学习','我们明天去学校学习','missing'),
                                         ('我们明天去去学校学习','我们明天去学校学习','extra'),
                                         ('我们明天去天空学习','我们明天去学校学习','typo')]:
            with self.subTest(expected=expected):
                result = self.match([asr], reference)['cues'][0]['reference_match']
                self.assertEqual(result['status'], expected, result)

    def test_one_cue_spans_adjacent_paragraphs(self):
        result = self.match(['你终于回来了我们都在等你'], '你终于回来了。\n我们都在等你。')
        reference = result['cues'][0]['reference_match']['reference']
        self.assertEqual((reference['paragraph_start'], reference['paragraph_end']), (1,2))

    def test_multiple_cues_share_one_reference_without_duplicated_suggestions(self):
        result = self.match(['我们明天', '去公园散步'], '我们明天去公园散步。')
        first, second = result['cues']
        self.assertEqual(normalized(first['script_text'] + second['script_text'])[0], '我们明天去公园散步')
        self.assertLess(first['reference_match']['reference']['start'], second['reference_match']['reference']['start'])

    def test_global_sequence_does_not_cross_or_reuse_positions(self):
        result = self.match(['今天我们去学校', '明天我们去公园'], '明天我们去公园。\n今天我们去学校。', max_group=1)
        ranges = [c['reference_match']['reference'] for c in result['cues'] if c['reference_match']['reference']]
        self.assertEqual(len(ranges), 1)

    def test_repeated_sentence_is_ambiguous_until_context_resolves_it(self):
        result = self.match(['我们明天去公园'], '我们明天去公园。\n先完成作业。\n我们明天去公园。', max_group=1)
        self.assertEqual(result['cues'][0]['reference_match']['status'], 'ambiguous')
        resolved = self.match(['先完成作业', '我们明天去公园'], '我们明天去公园。\n先完成作业。\n我们明天去公园。', max_group=1)
        self.assertEqual(resolved['cues'][1]['reference_match']['status'], 'high')
        self.assertEqual(resolved['cues'][1]['reference_match']['reference']['paragraph_start'], 3)

    def test_explicit_stage_direction_stays_reference_only(self):
        result = self.match(['缓缓走向窗边'], '【动作：他缓缓走向窗边】')
        matched = result['cues'][0]
        self.assertFalse(matched['script_text'])
        self.assertEqual(matched['reference_match']['status'], 'unmatched')
        self.assertTrue(matched['reference_match']['alternatives'][0]['reference_only'])

    def test_short_homophones_require_stronger_evidence(self):
        result = self.match(['名天'], '明天')
        self.assertEqual(result['cues'][0]['reference_match']['status'], 'unmatched')

    def test_skip_preserved_and_explicit_rematch_releases_it(self):
        cues = [cue('我们明天去学校', review_decision='skipped')]
        doc = parse_document('我们明天去学校。')
        self.assertEqual(match_reference(cues, doc)['cues'][0]['script_text'], '')
        self.assertTrue(match_reference(cues, doc, force_indices=[0])['cues'][0]['script_text'])

    def test_no_match_does_not_block_later_subtitle(self):
        result = self.match(['今天天气像外星飞船', '我们明天去学校'], '我们明天去学校。', max_group=1)
        self.assertEqual(result['cues'][0]['reference_match']['status'], 'unmatched')
        self.assertEqual(result['cues'][1]['reference_match']['status'], 'high')

    def test_thresholds_validated_and_empty_script_is_safe(self):
        for settings in ({'minimum':float('nan')}, {'high':.2,'minimum':.7}, {'pinyin_weight':.9}, {'max_group':2.5}):
            with self.assertRaises(ValueError): options_from(settings)
        self.assertEqual(self.match(['你好'], '')['cues'][0]['reference_match']['status'], 'unmatched')


if __name__ == '__main__': unittest.main()
