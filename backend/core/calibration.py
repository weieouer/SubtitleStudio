"""校准只采用已确认的参考文字；不调整条目结构或音频时间。"""
from __future__ import annotations

from copy import deepcopy

from . import srt_io


def _confirmed(match):
    return match.get('status') in ('high', 'typo', 'homophone', 'missing', 'extra') or (
        match.get('status') == 'ambiguous' and match.get('candidate_confirmed') is True)


def normalize_cues(cues, max_width=20, min_gap=.6, keep_punc=True, keep_interjections=False):
    """只复制参考文字；不插入/删除/合并/拆条件目，也不改时间。

    校准不会恢复被用户删除的条目，也不会把参考里多出的句子自动插进字幕；
    这类问题由对比校验标红，等用户确认。``max_width``、``min_gap`` 和
    ``keep_interjections`` 保留在签名里兼容旧调用，校准本身不再使用它们。

    ``keep_punc`` 只影响工作文本：关闭标点时按统一规则去掉标点；参考原文仍
    保存在 ``reference_match.reference.text``，``asr_original`` 也不被修改。
    """
    items = deepcopy(cues)
    clean = (lambda text: str(text or '')) if keep_punc else srt_io.strip_subtitle_punctuation
    changed, completed, pending = [], [], []
    for index, cue in enumerate(items):
        match = cue.get('reference_match') or {}
        reference = match.get('reference') or {}
        current = str(cue.get('text') or '')
        # 空条目可能来自用户删除；校准绝不把它填回参考句子。
        usable = (bool(current) and bool(reference.get('text'))
                  and not reference.get('reference_only')
                  and not match.get('stale')
                  and match.get('query_text', current) == current)
        if not _confirmed(match) or not usable:
            # 参考不确定时不改文字；关闭标点仍按统一规则整理当前工作文本。
            text = clean(current)
            if text != current:
                cue['text'] = text
                cue['words'] = []
                cue['edited'] = True
                if cue.get('alignment'):
                    cue['alignment'] = {**cue['alignment'], 'stale': True}
                changed.append(index)
            pending.append(index)
            continue
        source = reference['text']
        target = clean(source)
        cue['script_text'] = source
        if current != target:
            cue['text'] = target
            cue['words'] = []
            cue['edited'] = True
            if cue.get('alignment'):
                cue['alignment'] = {**cue['alignment'], 'stale': True}
            changed.append(index)
        cue['reference_match'] = {**match, 'status': 'high', 'label': '参考一致',
                                  'query_text': target, 'stale': False, 'differences': []}
        cue['match_needs_review'] = False
        cue['verification_stage'] = 'calibrate'
        completed.append(index)
    return {'mode': 'reference_copy', 'cues': items, 'changed_indices': changed,
            'completed_indices': completed, 'pending_indices': pending,
            'count': len(items), 'splits': 0, 'merged': 0, 'merge_suggestions': 0}
