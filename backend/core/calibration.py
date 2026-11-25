"""校准只采用已确认的参考文字；不调整条目结构或音频时间。"""
from __future__ import annotations

from copy import deepcopy

from . import srt_io


def _confirmed(match):
    return match.get('status') in ('high', 'typo', 'homophone', 'missing', 'extra') or (
        match.get('status') in ('ambiguous', 'weak') and match.get('candidate_confirmed') is True)


def normalize_cues(cues, keep_punc=True, reference_fingerprint=None):
    """只复制参考文字；不插入/删除/合并/拆条件目，也不改时间。

    校准不会恢复被用户删除的条目，也不会把参考里多出的句子自动插进字幕；
    这类问题由对比校验标红，等用户确认。

    ``keep_punc`` 只影响工作文本：开启仅保留问号、感叹号、省略号，关闭去掉句读；参考原文仍
    保存在 ``reference_match.reference.text``，``asr_original`` 也不被修改。
    """
    items = deepcopy(cues)
    clean = srt_io.normalize_subtitle_punctuation if keep_punc else srt_io.strip_subtitle_punctuation
    changed, completed, pending = [], [], []
    for index, cue in enumerate(items):
        match = cue.get('reference_match') or {}
        reference = match.get('reference') or {}
        current = str(cue.get('text') or '')
        done = cue.get('modification_complete')
        if (cue.get('review_decision') in ('manual', 'included', 'kept')
                and isinstance(done, dict) and done.get('text') == current and current):
            # 人工判断优先；再次校准不能把已确认的多识别条目覆盖或删除。
            completed.append(index)
            continue
        # 空条目可能来自用户删除；校准绝不把它填回参考句子。
        usable = (bool(current) and bool(reference.get('text'))
                  and not reference.get('reference_only')
                  and not match.get('stale')
                  and match.get('query_text', current) == current
                  and (reference_fingerprint is None
                       or match.get('reference_fingerprint') == reference_fingerprint))
        # 组合句定位可靠不代表分配给每条字幕的文字也可靠；低于该条阈值时交给人工。
        score,minimum=match.get('score'),match.get('minimum_used')
        if (match.get('context') or {}).get('status') == 'bounded':
            score = match.get('context_score', score)
        weak=(isinstance(score,(int,float)) and isinstance(minimum,(int,float)) and score<minimum)
        outside=(match.get('context') or {}).get('status')=='outside'
        if usable and match.get('status') not in ('unmatched', 'context_mismatch') and (weak or outside) and not match.get('candidate_confirmed'):
            ambiguous = bool(match.get('alternatives')) and match.get('status') == 'ambiguous'
            cue['reference_match']={**match,'status':'ambiguous' if ambiguous else 'weak',
                'label':'多候选匹配' if ambiguous else '逐条匹配待确认',
                'candidate_confirmed':False,'warning':('参考位置与上下文冲突，请人工确认' if outside else
                    '此条文字匹配证据不足，请核对参考原文或试听。')}
            cue['match_needs_review']=True
            pending.append(index)
            continue
        if not _confirmed(match) or not usable:
            # 参考不确定时不改文字；关闭标点仍按统一规则整理当前工作文本。
            text = clean(current)
            if text != current:
                cue['text'] = text
                cue['words'] = []
                cue['edited'] = True
                cue['modification_complete'] = False
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
        cue['reference_match'] = {**match, 'status': 'high', 'label': '高度匹配',
                                  'query_text': target, 'stale': False, 'differences': []}
        cue['reference_match'].pop('warning', None)
        cue['match_needs_review'] = False
        cue['verification_stage'] = 'calibrate'
        completed.append(index)
    return {'mode': 'reference_copy', 'cues': items, 'changed_indices': changed,
            'completed_indices': completed, 'pending_indices': pending,
            'count': len(items)}
