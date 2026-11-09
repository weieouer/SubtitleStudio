"""校准后的句子整理；不调用模型，不改原始ASR或相邻未选字幕。"""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata

from . import segment, srt_io
from .interjections import is_interjection, strip_boundary_interjections


def _letters(text):
    return ''.join(ch for ch in unicodedata.normalize('NFKC', text or '')
                   if unicodedata.category(ch)[0] in ('L', 'N'))


def _sentence_cuts(text):
    """只采用明确句界，不把所有逗号当成两句话，也不误拆小数/时间。"""
    cuts = []
    for match in re.finditer(r'[。！？!?；;]+|(?<![.\d])\.(?![.\d])|\n+', text):
        if _letters(text[:match.start()]) and _letters(text[match.end():]):
            cuts.append(match.end())
    return cuts


def _reference_text(cue):
    """返回可用于恢复句界的参考原文；只采纳文字与字幕逐字相同的可靠参考。

    参考文档常把两句话分成两行或两句，ASR 却合并成一条无标点字幕。
    只要去掉标点后与参考完全一致，就能安全地按参考句界恢复标点并拆条，
    不改变任何字词。采用参考后 match 会被标记为过期，因此高匹配允许 stale。
    """
    match = cue.get('reference_match') or {}
    reference = match.get('reference') or {}
    if not reference or reference.get('reference_only'):
        return ''
    status = match.get('status')
    reliable = (status == 'high'
                or (status in ('typo', 'homophone', 'missing', 'extra') and not match.get('stale'))
                or (status == 'ambiguous' and match.get('candidate_confirmed')))
    if not reliable:
        return ''
    source = reference.get('text') or cue.get('script_text') or ''
    return source if source and _letters(cue.get('text', '')) == _letters(source) else ''


def _interjection_reference(cue):
    """返回可用于判断语气词的可靠参考原文；过期、异位或未确认的不算。"""
    match = cue.get('reference_match') or {}
    reference = match.get('reference') or {}
    if not reference or reference.get('reference_only') or match.get('stale'):
        return ''
    status = match.get('status')
    reliable = (status == 'high'
                or status in ('typo', 'homophone', 'missing', 'extra')
                or (status == 'ambiguous' and match.get('candidate_confirmed')))
    if not reliable:
        return ''
    return reference.get('text') or cue.get('script_text') or ''


def _prepare_interjections(items, keep_interjections):
    """按开关和参考删除整条语气词，去掉句首、句末语气词。

    关闭时：整条语气词直接删除，文字里的句首/句末语气词全部去掉。
    开启时：整条语气词保留；可靠参考里没有的语气词才去掉。
    手动“加入导出”的条目不受自动删除影响。
    """
    kept, interjections, trimmed, removed = [], 0, 0, 0
    for cue in items:
        original = cue.get('text', '')
        flagged = is_interjection(original)
        if flagged:
            interjections += 1
        if cue.get('review_decision') == 'included':
            cue['interjection'] = flagged
            kept.append(cue)
            continue
        text = original
        if not keep_interjections:
            text = '' if flagged else strip_boundary_interjections(original)
        else:
            reference = _interjection_reference(cue)
            if flagged:
                # 参考里没有这个语气词时，即使开启开关也不要单独保留。
                if reference and _letters(original) not in _letters(reference):
                    text = ''
            else:
                stripped = strip_boundary_interjections(original)
                # 去掉后与可靠参考逐字一致，说明参考确实没有这个词。
                if stripped != original and reference and _letters(stripped) == _letters(reference):
                    text = stripped
        if text == '' and _letters(original):
            removed += 1
            continue
        if text != original:
            cue['text'] = text
            cue['_layout_changed'] = True
            trimmed += 1
        cue['interjection'] = is_interjection(cue.get('text', ''))
        kept.append(cue)
    return kept, interjections, trimmed, removed


def _split_sentence(cue, cut, max_width):
    children, count = segment.segment_cue(cue, max_width, split_at=cut)
    if not count:
        return children, count
    times = segment._char_times(cue['text'], cue.get('words'))
    left = next((t for t in reversed(times[:cut]) if t is not None), None)
    right = next((t for t in times[cut:] if t is not None), None)
    # 句末标点本身没有词时间；使用它两侧真实词的首尾，不把静音按字符比例填满。
    if left and right and cue['start'] < left[1] <= right[0] < cue['end']:
        positions = segment._word_positions(cue['text'], cue.get('words') or [])
        for child, lo, hi, start, end in (
                (children[0], 0, cut, cue['start'], left[1]),
                (children[1], cut, len(cue['text']), right[0], cue['end'])):
            words, estimated = segment._piece_words(cue, positions, lo, hi, start, end)
            child.update(start=round(start, 3), end=round(end, 3), words=words,
                         estimated=bool(cue.get('estimated') or estimated))
        if right[0] > left[1]:
            children[0]['pause_after'] = children[0]['end']
            children[1]['pause_before'] = children[1]['start']
    return children, count


def _join_evidence(left, right, max_width, min_gap):
    """判断两条相邻字幕是否像同一句被误拆；只返回证据，不直接合并。"""
    a, b = left.get('text', ''), right.get('text', '')
    gap = float(right['start']) - float(left['end'])
    if (not 1 <= min(segment.significant_width(a), segment.significant_width(b)) <= 2
            or max(segment.significant_width(a), segment.significant_width(b)) < 3
            or gap < -.001 or gap > .25 or (min_gap > 0 and gap + 1e-6 >= min_gap)
            or segment.text_width(srt_io.strip_subtitle_punctuation(a+b)) > max_width
            or segment._ends_with_punct(a) and a.rstrip().rstrip(''.join(segment.CLOSERS)).endswith(segment.TERMINAL)
            or _sentence_cuts(a+b) or left.get('manual_split') or right.get('manual_split')
            or left.get('source') == 'sentence_split' or right.get('source') == 'sentence_split'
            or left.get('alignment_error') or right.get('alignment_error')
            or left.get('interjection') or right.get('interjection')
            or bool(left.get('exclude_export')) != bool(right.get('exclude_export'))
            or (left.get('review_decision') == 'non_script') != (right.get('review_decision') == 'non_script')
            or (left.get('role') or '') != (right.get('role') or '')):
        return None
    # 长拖尾/独立单字答话不能仅凭距离被当成碎片。
    single = left if segment.significant_width(a) <= segment.significant_width(b) else right
    if float(single['end']) - float(single['start']) > 1.2:
        return None
    lm, rm = left.get('reference_match') or {}, right.get('reference_match') or {}
    lr, rr = lm.get('reference') or {}, rm.get('reference') or {}
    reference = (lr and rr and not lm.get('stale') and not rm.get('stale')
                 and lm.get('status') == rm.get('status') == 'high'
                 and lr.get('source_id') is not None and lr.get('source_id') == rr.get('source_id')
                 and 0 <= rr.get('start', -100) - lr.get('end', 0) <= 2
                 and lr.get('paragraph_end') == rr.get('paragraph_start'))
    same = (left.get('asr_original') and left.get('asr_original') == right.get('asr_original')
            and left.get('split_parent') == right.get('split_parent'))
    # 相邻逐字ASR不一定有共同父条；完整词跨越接缝是更强的合并证据。
    lexical = False
    if segment.jieba is not None:
        offset = 0
        for token in segment.jieba.lcut(a+b):
            if offset < len(a) < offset+len(token) and segment.significant_width(token) > 1:
                lexical = True
            offset += len(token)
    if not (reference or same or lexical):
        return None
    if lr and rr and lr.get('source_id') != rr.get('source_id'):
        return None
    evidence = 'word' if lexical else 'reference' if reference else 'same'
    return {'evidence': evidence,
            'score': (3*bool(lexical)+2*bool(reference)+bool(same), -max(0, gap))}


_TERMINAL_END = re.compile(r'[。！？!?；;]+$|(?<![.\d])\.$')
_CLOSER_CHARS = '）)】」』”"\'…'


def _ends_sentence(text):
    """参考片段是否以句末标点或换行结束；逗号、顿号只表示同一句内部停顿。"""
    text = (text or '').rstrip()
    if '\n' in text or '\r' in text:
        return True
    text = text.rstrip(_CLOSER_CHARS).rstrip()
    return bool(text) and bool(_TERMINAL_END.search(text))


def _reliable_reference(cue):
    """返回与当前字幕逐字一致的可靠参考，以及匹配对象。

    采用参考后 match 会被标记为过期；此时字幕文字已经就是参考原文，
    仍可用于判断两条是否同属一句，因此这里以“文字逐字一致”为准。
    """
    match = cue.get('reference_match') or {}
    reference = match.get('reference') or {}
    if not reference or reference.get('reference_only'):
        return None, match
    status = match.get('status')
    reliable = (status == 'high' or status in ('typo', 'homophone', 'missing', 'extra')
                or (status == 'ambiguous' and match.get('candidate_confirmed')))
    if not reliable:
        return None, match
    text = reference.get('text') or ''
    if not text or _letters(cue.get('text', '')) != _letters(text):
        return None, match
    return reference, match


def _same_reference_sentence(left, right):
    """两条字幕是否同属参考里的同一句。

    同一行、片段相邻、左侧没有句末标点才算同一句；参考换行/句末标点
    是明确的句界，不受停顿阈值影响。
    """
    if not left or not right or left.get('source_id') != right.get('source_id'):
        return False
    try:
        left_end, right_start = int(left.get('end')), int(right.get('start'))
    except (TypeError, ValueError):
        return False
    # 片段之间允许空白；超过两个字符说明中间还有参考文字，不能确认是同一句。
    if not 0 <= right_start - left_end <= 2:
        return False
    if left.get('paragraph_end') != right.get('paragraph_start'):
        return False
    return not _ends_sentence(left.get('text'))


def _can_auto_merge(left, right, max_width, min_gap):
    """同一句、停顿未超阈值、合并后不超每行字数，才允许自动合并。"""
    if not (left.get('text') and right.get('text')):
        return False
    for cue in (left, right):
        if (cue.get('manual_split') or cue.get('alignment_error') or cue.get('word_timing_error')
                or cue.get('interjection') or cue.get('source') == 'sentence_split'):
            return False
    if (left.get('role') or '') != (right.get('role') or ''):
        return False
    if bool(left.get('is_noise')) != bool(right.get('is_noise')):
        return False
    if bool(left.get('exclude_export')) != bool(right.get('exclude_export')):
        return False
    if (left.get('review_decision') == 'non_script') != (right.get('review_decision') == 'non_script'):
        return False
    try:
        gap = float(right.get('start', 0)) - float(left.get('end', 0))
    except (TypeError, ValueError):
        return False
    if gap < -.001:
        return False
    # 停顿达到阈值就保持分句；阈值 0 表示关闭停顿规则。
    if min_gap > 0 and gap + 1e-6 >= min_gap:
        return False
    left_ref, _ = _reliable_reference(left)
    right_ref, _ = _reliable_reference(right)
    if not left_ref or not right_ref:
        return False
    if not _same_reference_sentence(left_ref, right_ref):
        return False
    return segment.text_width((left.get('text') or '') + (right.get('text') or '')) <= max_width


def _merge_pair(left, right):
    """合并同一句的相邻两条；文字顺序直接拼接，时间取首条开始到末条结束。"""
    merged = deepcopy(left)
    merged['text'] = (left.get('text') or '') + (right.get('text') or '')
    merged['start'] = float(left.get('start', 0))
    merged['end'] = float(right.get('end', merged['start']))
    left_words, right_words = left.get('words') or [], right.get('words') or []
    merged['words'] = deepcopy(left_words) + deepcopy(right_words) if left_words and right_words else []
    merged['estimated'] = bool(left.get('estimated') or right.get('estimated') or not merged['words'])
    merged['edited'] = True
    merged['modification_complete'] = False
    merged['_calibration_changed'] = True
    merged['_calibration_complete'] = False
    merged['source'] = 'same_sentence_merge'
    merged['match_needs_review'] = False
    if merged.get('alignment'):
        merged['alignment'] = {**merged['alignment'], 'stale': True}
    merged.pop('merge_suggestion', None)
    for key in ('width_split', 'manual_split', 'split_parent', 'segmentation'):
        merged.pop(key, None)
    # 两条之间的边界变成合并后条目的内部停顿，不再作为条首/条尾空档。
    merged.pop('pause_after', None)
    if 'pause_after' in right:
        merged['pause_after'] = right['pause_after']
    if 'pause_before' in merged and not (merged['start'] - .001 <= merged['pause_before'] <= merged['end'] + .001):
        merged.pop('pause_before')
    # 参考区间按同一句拼接；后续对齐结束后的重新校验会再刷新一次。
    left_match, right_match = left.get('reference_match') or {}, right.get('reference_match') or {}
    left_ref, right_ref = left_match.get('reference') or {}, right_match.get('reference') or {}
    if left_ref and right_ref and left_ref.get('source_id') == right_ref.get('source_id'):
        gap = int(right_ref.get('start', 0)) - int(left_ref.get('end', 0))
        if 0 <= gap <= 2:
            reference = {**left_ref,
                         'text': (left_ref.get('text') or '') + (right_ref.get('text') or ''),
                         'end': right_ref.get('end'),
                         'paragraph_end': right_ref.get('paragraph_end', left_ref.get('paragraph_end'))}
            merged['reference_match'] = {**left_match, 'reference': reference,
                                         'query_text': merged['text'], 'stale': True}
        else:
            merged['reference_match'] = None
    else:
        merged['reference_match'] = None
    for key in ('script_text', 'script_index', 'match_ratio', 'match_status'):
        merged.pop(key, None)
    sources = segment._merge_sources(left, right)
    if sources:
        starts = [float(s['start']) for s in sources if isinstance(s.get('start'), (int, float))]
        ends = [float(s['end']) for s in sources if isinstance(s.get('end'), (int, float))]
        merged['asr_sources'] = sources
        merged['original_text'] = ''.join(str(s.get('text') or '') for s in sources)
        merged['original_start'] = min(starts) if starts else merged['start']
        merged['original_end'] = max(ends) if ends else merged['end']
        merged['asr_original'] = {'text': merged['original_text'], 'start': merged['original_start'],
                                  'end': merged['original_end'], 'id': sources[0].get('id') or left.get('id')}
    return merged


def _merge_same_sentence(cues, max_width, min_gap):
    """校准内的自动合并：只合并可靠参考中同属一句的相邻字幕。"""
    result, merged = [], 0
    for cue in cues:
        if result and _can_auto_merge(result[-1], cue, max_width, min_gap):
            result[-1] = _merge_pair(result[-1], cue)
            merged += 1
        else:
            result.append(cue)
    return result, merged


def _suggest_fragments(cues, max_width, min_gap):
    """把误拆的短碎片标为“建议合并”，由用户多选后手动合并，不自动改结构。"""
    used, pairs = set(), []
    for i in range(len(cues)):
        if i in used or not 1 <= segment.significant_width(cues[i].get('text', '')) <= 2:
            continue
        choices = []
        for j in (i-1, i):
            if 0 <= j < len(cues)-1 and j not in used and j+1 not in used:
                evidence = _join_evidence(cues[j], cues[j+1], max_width, min_gap)
                if evidence is not None:
                    choices.append((evidence['score'], j, evidence['evidence']))
        if not choices:
            continue
        _, j, evidence = max(choices, key=lambda item: item[0])
        direction = 'previous' if j == i-1 else 'next'
        partner = cues[j] if direction == 'previous' else cues[j+1]
        where = '上一条' if direction == 'previous' else '下一条'
        why = {'word': '可组成完整词', 'reference': '参考显示同属一句', 'same': '来自同一条原话'}[evidence]
        cues[i]['merge_suggestion'] = {
            'direction': direction,
            'partner_display_id': str(partner.get('display_id') or partner.get('id') or ''),
            'reason': f'疑似拆出的短碎片，与{where}{why}，建议勾选相邻两条后手动合并',
        }
        used.update((j, j+1))
        pairs.append((j, j+1))
    return len(pairs), pairs


def normalize_cues(cues, max_width=20, min_gap=.6, keep_punc=True, keep_interjections=False):
    clean_text = srt_io.normalize_subtitle_punctuation if keep_punc else srt_io.strip_subtitle_punctuation
    items = deepcopy(cues)
    # 旧的合并建议可能已因手工编辑失效，校准会按当前结构重新检测。
    for cue in items:
        cue.pop('merge_suggestion', None)
    # 同字的可靠参考也用于恢复标点；不采用异位、歧义或过期参考。
    for cue in items:
        match = cue.get('reference_match') or {}
        done = cue.get('modification_complete')
        acknowledged = (isinstance(done, dict) and all(done.get(k) == cue.get(k) for k in ('text', 'start', 'end')))
        cue['_can_complete'] = bool(acknowledged or (match.get('status') == 'high' and not match.get('stale')))
        # 以参考原文（优先于 script_text）恢复句界和标点；字幕里没有标点也能拆分。
        source = _reference_text(cue)
        if source:
            cue['script_text'] = source
            if cue['text'] != source:
                cue['text'] = source
                cue['_layout_changed'] = True
    items, interjections, trimmed, removed = _prepare_interjections(items, keep_interjections)
    output, splits, punctuation = [], 0, 0
    for cue in items:
        pieces = [cue]
        # 逐个明确句界拆开，独立处理本条，不把尾部拼入下一条。
        while True:
            last = pieces[-1]
            cuts = _sentence_cuts(last.get('text', ''))
            if not cuts:
                break
            children, count = _split_sentence(last, cuts[0], max_width)
            if not count:
                break
            for child in children:
                child['width_split'] = False
                child['source'] = 'sentence_split'
                child['_layout_changed'] = True
            pieces[-1:] = children
            splits += count
        # 先依原始句界独立分句，再去掉隐藏标点，避免逗号/句号造成无谓超宽拆条。
        for piece in pieces:
            clean = clean_text(piece['text'])
            if clean != piece['text']:
                piece['text'] = clean
                piece['_layout_changed'] = True
                punctuation += 1
        # 停顿拆条由“对齐字幕”负责；校准只按明确句界和每行字数整理内容。
        # 停顿阈值仅用于判断两条相邻是否像同一句被拆开，不在这里再拆条。
        pieces, width_count, _ = segment.segment_cues(pieces, max_width)
        splits += width_count
        for piece in pieces:
            raw = piece['text']
            piece['text'] = clean_text(raw)
            changed = bool(piece.pop('_layout_changed', False) or width_count or raw != piece['text'])
            if raw != piece['text']:
                punctuation += 1
            # 清理父条停顿和修改确认；子条只保留自身范围内的派生边界。
            for key in ('pause_before', 'pause_after'):
                if key in piece and not piece['start']-.001 <= piece[key] <= piece['end']+.001:
                    piece.pop(key)
            if changed:
                piece['edited'] = True
                piece['modification_complete'] = False
                match = piece.get('reference_match')
                if match and match.get('query_text') != piece['text']:
                    piece['reference_match'] = {**match, 'stale': True}
            can_complete = piece.pop('_can_complete', False)
            complete = changed and can_complete
            output.append((piece, changed, complete))
    for piece, changed, complete in output:
        piece['_calibration_changed'] = bool(changed)
        piece['_calibration_complete'] = bool(complete)
    final = [c for c, _, _ in output]
    # 旧版本校准曾给语气词标“不导出”；重新校准时恢复，
    # 新版本在关闭开关时直接删除整条语气词，不再保留空行。
    restored = 0
    for cue in final:
        if cue.pop('interjection_excluded', False) and cue.get('review_decision') != 'non_script':
            if cue.get('exclude_export'):
                cue['exclude_export'] = False
                restored += 1
        cue['interjection'] = is_interjection(cue.get('text', ''))
    # 同一句、停顿未超阈值且不超字数时自动并回一条；参考断句不参与停顿合并。
    final, merged = _merge_same_sentence(final, max_width, min_gap)
    changed_indices = [i for i, cue in enumerate(final) if cue.pop('_calibration_changed', False)]
    completed_indices = [i for i, cue in enumerate(final) if cue.pop('_calibration_complete', False)]
    suggestions, suggestion_pairs = _suggest_fragments(final, max_width, min_gap)
    return {'mode': 'calibration', 'cues': final, 'merged': merged,
            'merge_suggestions': suggestions,
            'merge_suggestion_pairs': [list(pair) for pair in suggestion_pairs],
            'suggested_indices': [i for i, cue in enumerate(final) if cue.get('merge_suggestion')],
            'splits': splits, 'punctuation_changes': punctuation,
            'interjections': sum(1 for cue in final if cue.get('interjection')),
            'interjection_removed': removed,
            'interjection_trimmed': trimmed,
            # 保留旧字段，供尚未更新的前端读取；新版不再靠“不导出”隐藏语气词。
            'interjection_excluded': 0,
            'interjection_restored': restored,
            'changed_indices': changed_indices,
            'completed_indices': completed_indices}
