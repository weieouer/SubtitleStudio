"""报告R-01至R-20的后端回归；全部文件写入临时目录。"""
from __future__ import annotations

import concurrent.futures
from array import array
import copy
import hashlib
import http.client
import json
import math
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import app
from core import jobs, media, segment, srt_io, reference_match
from core.storage import atomic_json


def cue(text="你好", start=0, end=1, **extra):
    return {"text": text, "start": start, "end": end, **extra}


class CoreTests(unittest.TestCase):
    def test_cursor_split_preserves_word_gap_text_and_asr_history(self):
        original={'text':'东西拿到了？','start':1.,'end':3.,'asr_original':{'text':'原始记录','start':0.,'end':4.},
                  'words':[{'word':'东西','start':1.,'end':1.4},{'word':'拿到了','start':2.,'end':3.}]}
        before=copy.deepcopy(original)
        pieces,count=segment.segment_at_cursor(original,2,20)
        self.assertEqual(count,1)
        self.assertEqual([c['text'] for c in pieces],['东西','拿到了？'])
        self.assertEqual((pieces[0]['end'],pieces[1]['start']),(1.4,2.))
        self.assertTrue(all(c['manual_split'] for c in pieces))
        self.assertTrue(all(c['asr_original']==before['asr_original'] for c in pieces))
        self.assertEqual(original,before)

    def test_cursor_split_missing_or_stale_words_never_claims_precise_alignment(self):
        for words in [[],[{'word':'你好世界','start':0.,'end':4.}]]:
            with self.subTest(words=words):
                original={'text':'你好世界','start':0.,'end':4.,'words':words,'alignment':{'stale':True},
                          'timing_review':{'text':'你好世界','start':0.,'end':4.,'source':'model'}}
                pieces,count=segment.segment_at_cursor(original,2)
                self.assertEqual(count,1)
                self.assertEqual([c['text'] for c in pieces],['你好','世界'])
                self.assertTrue(all(c['estimated'] and c['needs_review'] and c['alignment']['stale'] for c in pieces))
                self.assertTrue(all(not c['words'] and 'timing_review' not in c for c in pieces))

    def test_cursor_split_unicode_and_empty_edges(self):
        pieces,count=segment.segment_at_cursor({'text':'甲😀乙丙','start':0.,'end':2.},2)
        self.assertEqual(count,1);self.assertEqual([c['text'] for c in pieces],['甲😀','乙丙'])
        for offset in [None,True,0,-1,4,1.5]:
            with self.subTest(offset=offset),self.assertRaises(ValueError):
                segment.segment_at_cursor({'text':'甲乙丙丁','start':0.,'end':2.},offset)
        with self.assertRaises(ValueError):segment.segment_at_cursor({'text':'a\u0301乙','start':0.,'end':2.},1)

    def test_cursor_split_drops_line_breaks_at_cut_and_keeps_neighbour_timing(self):
        # Shift+回车的换行只充当切点：不留在任何一条字幕里，两侧沿用相邻词的时间。
        original={'text':'甲乙'+chr(10)+'丙丁','start':0.,'end':5.,
                  'words':[{'word':'甲','start':0.,'end':1.},{'word':'乙','start':1.,'end':2.},
                           {'word':'丙','start':3.,'end':4.},{'word':'丁','start':4.,'end':5.}]}
        pieces,count=segment.segment_at_cursor(original,3,20)
        self.assertEqual(count,1)
        self.assertEqual([c['text'] for c in pieces],['甲乙','丙丁'])
        self.assertEqual((pieces[0]['end'],pieces[1]['start']),(2.5,2.5))
        self.assertFalse(any(c.get('estimated') for c in pieces))

    def test_cursor_split_needs_text_on_both_sides_and_enough_time(self):
        with self.assertRaises(ValueError):
            segment.segment_at_cursor({'text':'甲'+chr(10)+chr(10),'start':0.,'end':2.},2)
        with self.assertRaisesRegex(ValueError,'时间过短'):
            segment.segment_at_cursor({'text':'甲乙丙丁','start':0.,'end':0.001},2)

    def test_non_overwrite_export_retries_collision_and_never_truncates(self):
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder) / '视频.mp4'
            first = app._write_text_output(str(media), None, 'a')
            second = app._write_text_output(str(media), None, 'b')
            self.assertEqual(first.read_text(encoding='utf-8'), 'a')
            self.assertEqual(second.name, '视频_2.srt')
            self.assertEqual(second.read_text(encoding='utf-8'), 'b')

    def test_overwrite_export_keeps_old_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder) / '视频.mp4'
            target = Path(folder) / '视频.srt'
            target.write_text('old', encoding='utf-8')
            with patch.object(app.os, 'replace', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    app._overwrite_text_output(str(media), None, 'new')
            self.assertEqual(target.read_text(encoding='utf-8'), 'old')
            self.assertFalse(list(Path(folder).glob('.subtitle-export-*')))

    def test_failed_non_overwrite_export_does_not_leave_partial_file(self):
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder) / '视频.mp4'
            original_open = Path.open

            class Broken:
                def __enter__(self):
                    return self

                def __exit__(self, *arguments):
                    self.handle.close()
                    return False

                def write(self, text):
                    raise OSError('disk full')

            def failing_open(path, mode='r', *arguments, **keywords):
                handle = original_open(path, mode, *arguments, **keywords)
                if 'x' in mode:
                    broken = Broken()
                    broken.handle = handle
                    return broken
                return handle

            with patch.object(Path, 'open', failing_open):
                with self.assertRaises(OSError):
                    app._write_text_output(str(media), None, '字幕')
            self.assertFalse(list(Path(folder).glob('视频*.srt')))

    def test_export_text_options_preserve_original_and_semantic_punctuation(self):
        items = [cue("你好，世界！\n价格3.5元，50%，don't，GPT-4。", role="甲"), cue("！！", 1, 2), cue("  ", 2, 3)]
        original = copy.deepcopy(items)
        result = srt_io.prepare_export_cues(items, keep_punctuation=False, single_line=True)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "你好世界 价格3.5元50%don'tGPT-4")
        self.assertEqual(items, original)
        self.assertIn("甲：", srt_io.format_srt(result, with_role=True))
        self.assertEqual(len(srt_io.prepare_export_cues(items, skip_empty=False)), 3)
        self.assertEqual(srt_io.prepare_export_cues([cue('Hello,world! 12:30 1,000.5')], keep_punctuation=False)[0]['text'], 'Helloworld 12:30 1,000.5')
        # 全角百分号与半角同样保留为数字单位，去掉标点后不留连续空格。
        self.assertEqual(srt_io.prepare_export_cues([cue('50％  好， 走吧')], keep_punctuation=False)[0]['text'], '50％ 好 走吧')

    def gap_cue(self, text="你好，走吧！"):
        return cue(text, 0, 3, words=[{"word": "你好", "start": 0, "end": .9},
                                     {"word": "走吧", "start": 2, "end": 2.9}])

    def test_gap_preserves_punctuation_and_spacing(self):
        for text in ("你好，走吧！", " 你好， 走吧！ "):
            parts, n = segment.split_on_gaps([self.gap_cue(text)], .6)
            self.assertEqual(n, 1)
            self.assertEqual("".join(p["text"] for p in parts), text)

    def test_gap_does_not_revert_corrected_text(self):
        original = self.gap_cue("您好，走吧！")
        parts, n = segment.split_on_gaps([original], .6)
        self.assertEqual(n, 0)
        self.assertEqual(parts[0]["text"], original["text"])
        self.assertTrue(parts[0]["gap_split_skipped"])

    def test_gap_without_words_is_unchanged(self):
        original = cue()
        self.assertEqual(segment.split_on_gaps([original], .6), ([original], 0))

    def test_shared_word_time_stays_inside_original(self):
        text = "甲乙丙丁戊己庚辛壬癸子丑"
        parts, _ = segment.segment_cue(cue(text, 0, 2, words=[{"word": text, "start": 0, "end": 2}]), 6)
        self.assertEqual(parts[0]["start"], 0)
        self.assertEqual(parts[-1]["end"], 2)
        self.assertEqual("".join(p["text"] for p in parts), text)
        for i, part in enumerate(parts):
            self.assertLess(part["start"], part["end"])
            if i:
                self.assertGreaterEqual(part["start"], parts[i - 1]["end"])

    def test_short_interval_never_expands(self):
        for end in (.001, .01, .1, 2):
            original = cue("一二三四五六七八九十一二三四五六七八九十", 0, end)
            parts, _ = segment.segment_cue(original, 6)
            self.assertEqual("".join(p["text"] for p in parts), original["text"])
            self.assertLessEqual(parts[-1]["end"], end)

    def test_width_split_preserves_outer_whitespace(self):
        original = "  一二三四五六七八九十。 \n"
        parts, _ = segment.segment_cue(cue(original, 0, 3), 6)
        self.assertEqual("".join(part["text"] for part in parts), original)

    def test_merge_preserves_complete_words(self):
        left = cue("你好", 0, 1, words=[{"word": "你好", "start": 0, "end": 1}], script_text="旧建议")
        right = cue("走吧", 1.1, 2, words=[{"word": "走吧", "start": 1.1, "end": 2}])
        parts, n = segment.merge_cues([left, right])
        self.assertEqual(n, 1)
        self.assertEqual(len(parts[0]["words"]), 2)
        self.assertNotIn("script_text", parts[0])
        self.assertEqual(left["text"], "你好")

    def test_overlap_is_not_merged(self):
        self.assertEqual(segment.merge_cues([cue("甲", 0, 2), cue("乙", 1, 3)])[1], 0)

    def test_merge_keeps_both_asr_sources_and_original_span(self):
        left = cue("你好", 0, 1, asr_original={"text": "你好", "start": 0, "end": 1, "id": 1},
                   original_text="你好", original_start=0, original_end=1)
        right = cue("走吧", 1.1, 2, asr_original={"text": "走吧", "start": 1.1, "end": 2, "id": 2},
                    original_text="走吧", original_start=1.1, original_end=2)
        parts, n = segment.merge_cues([left, right])
        self.assertEqual(n, 1)
        merged = parts[0]
        self.assertEqual([s["text"] for s in merged["asr_sources"]], ["你好", "走吧"])
        self.assertEqual(merged["original_text"], "你好走吧")
        self.assertEqual(merged["original_start"], 0)
        self.assertEqual(merged["original_end"], 2)
        self.assertEqual(merged["asr_original"], {"text": "你好走吧", "start": 0, "end": 2, "id": 1})
        self.assertEqual(left["original_text"], "你好")

    def test_merge_invalidates_stale_derived_results(self):
        left = cue("你好", 0, 1, reference_match={"text": "旧建议", "ratio": 1},
                   script_text="旧建议", script_index=3, match_ratio=1, match_needs_review=True,
                   alignment={"engine": "aligner", "original_start": 0, "original_end": 1},
                   is_noise=False, review_decision="pending")
        right = cue("走吧", 1.1, 2)
        parts, n = segment.merge_cues([left, right])
        self.assertEqual(n, 1)
        merged = parts[0]
        self.assertIsNone(merged["reference_match"])
        self.assertFalse(merged["match_needs_review"])
        self.assertTrue(merged["alignment"]["stale"])
        for key in ("script_text", "script_index", "match_ratio", "review_decision"):
            self.assertNotIn(key, merged)

    def test_noise_and_normal_cues_are_not_auto_merged(self):
        left = cue("音乐", 0, 1, is_noise=True)
        right = cue("你好", 1.1, 2, is_noise=False)
        parts, n = segment.merge_cues([left, right])
        self.assertEqual(n, 0)
        self.assertEqual(len(parts), 2)

    def test_match_is_monotonic_and_unique(self):
        result = reference_match.match_reference([cue("甲"), cue("乙")], {'text':"角色：乙\n角色：甲"})
        indexes = [h["cue_index"] for h in result["hits"]]
        self.assertEqual(indexes, sorted(set(indexes)))
        result = reference_match.match_reference([cue("你好")], {'text':"甲：你好\n乙：你好"})
        self.assertEqual(result["stats"]["matched"], 1)
        self.assertEqual(result["cues"][0]["text"], "你好")
        self.assertEqual(result["cues"][0]["reference_match"]["status"], "ambiguous")

    def test_match_can_relocate_beyond_first_window(self):
        result = reference_match.match_reference([cue("甲甲甲甲") for _ in range(20)] + [cue("乙乙乙乙")],
                                                {'text':"角色：乙乙乙乙"})
        self.assertEqual(result["hits"][0]["cue_index"], 20)

    def test_match_preserves_asr_review_reason(self):
        for line in ("角色：你好", "角色：完全无关台词"):
            result = reference_match.match_reference([cue(needs_review=True)], {'text':line})
            self.assertTrue(result["cues"][0]["needs_review"])

    def test_export_rejects_bad_times_and_overlap(self):
        for items in ([cue(end=20)], [cue(start=float("nan"))], [cue(end=float("inf"))],
                      [cue(start=-1)], [cue(start=1, end=1)], [cue("甲", 0, 2), cue("乙", 1, 3)]):
            self.assertTrue(srt_io.validate_cues(items, 10))

    def test_srt_roundtrip_utf8_crlf(self):
        items, warnings = srt_io.parse_srt_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n第二行\n")
        self.assertFalse(warnings)
        encoded = srt_io.format_srt(items).encode("utf-8")
        self.assertFalse(encoded.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", encoded)
        self.assertEqual(items[0]["text"], "你好\n第二行")

    def test_query_path_decodes_once(self):
        for name in ("name%20.mp4", "name%2F.mp4", "加+号 空格.mp4"):
            self.assertEqual(app._qget(app.parse_qs("path=" + quote(name)), "path"), name)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="subtitle-regression-")
        cls.folder = Path(cls.tmp.name)
        cls.patches = [patch.object(app, key, cls.folder / key) for key in
                       ("PROJECT_DIR", "CONFIG_DIR", "OUTPUT_DIR", "UPLOADS_DIR")]
        for p in cls.patches:
            p.start()
        for key in ("PROJECT_DIR", "CONFIG_DIR", "OUTPUT_DIR", "UPLOADS_DIR"):
            getattr(app, key).mkdir()
        cls.source = cls.folder / "source.srt"
        cls.source.write_text(srt_io.format_srt([cue()]), encoding="utf-8")
        cls.binary = cls.folder / "sample.mp4"
        cls.binary.write_bytes(bytes(range(250)) * 4)
        class QuietHandler(app.Handler):
            def log_message(self, *args):
                pass
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, auth=True, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json"}
        if auth:
            request_headers["X-SubtitleStudio-Token"] = app.SESSION_TOKEN
        request_headers.update(headers or {})
        payload = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        try:
            connection.request(method, path, payload, request_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def json_request(self, method, path, body=None, **kwargs):
        status, _, raw = self.request(method, path, body, **kwargs)
        return status, json.loads(raw)

    def test_session_token_is_in_html_and_required_for_api(self):
        status, _, body = self.request("GET", "/", auth=False)
        self.assertEqual(status, 200)
        self.assertIn(app.SESSION_TOKEN.encode(), body)
        self.assertNotIn(b"__SUBTITLE_SESSION_TOKEN__", body)
        self.assertEqual(self.request("GET", "/api/health", auth=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/health")[0], 200)

    def test_film_icon_is_served(self):
        status, headers, raw = self.request("GET", "/favicon.ico", auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\0\0\1\0"))
        self.assertIn("image/", headers["Content-Type"])
        self.assertEqual(self.request("GET", "/assets/studio-film.svg", auth=False)[0], 200)

    def test_foreign_host_and_origin_rejected(self):
        for headers in ({"Host": "review.invalid"}, {"Origin": "http://review.invalid"},
                        {"Sec-Fetch-Site": "cross-site"}, {"X-SubtitleStudio-Token": "错".encode("utf-8").decode("latin1")}):
            self.assertEqual(self.request("GET", "/api/config", headers=headers)[0], 403)
        self.assertEqual(self.request("OPTIONS", "/api/config")[0], 403)

    def test_local_origin_allowed(self):
        self.assertEqual(self.request("GET", "/api/config", headers={"Origin": "http://127.0.0.1:" + str(self.server.server_port)})[0], 200)

    def test_static_traversal_rejected(self):
        self.assertEqual(self.request("GET", "/../backend/app.py")[0], 404)

    def test_ranges_and_media_query_token(self):
        url = "/api/media/stream?path=" + quote(str(self.binary)) + "&token=" + app.SESSION_TOKEN
        for header, expected_status, expected_length, expected_range in [
            ("bytes=-100", 206, 100, "bytes 900-999/1000"),
            ("bytes=0-2000", 206, 1000, "bytes 0-999/1000"),
            ("bytes=950-", 206, 50, "bytes 950-999/1000"),
            ("bytes=5-3", 416, 0, "bytes */1000"),
            ("bytes=1000-", 416, 0, "bytes */1000"),
            ("bytes=-0", 416, 0, "bytes */1000"),
            ("bytes=0-1,3-4", 416, 0, "bytes */1000"),
        ]:
            with self.subTest(range=header):
                status, headers, raw = self.request("GET", url, headers={"Range": header}, auth=False)
                self.assertEqual((status, len(raw), headers.get("Content-Range")), (expected_status, expected_length, expected_range))
                self.assertEqual(int(headers["Content-Length"]), len(raw))
                if header == "bytes=-100":
                    self.assertEqual(raw, self.binary.read_bytes()[-100:])

    def test_cursor_split_api_uses_exact_character_offset(self):
        body={'mode':'cursor','split_at':2,'cues':[cue('甲😀乙丙',0,2)],'max_width':16}
        status,result=self.json_request('POST','/api/segment',body)
        self.assertEqual(status,200);self.assertEqual(result['splits'],1)
        self.assertEqual([c['text'] for c in result['cues']],['甲😀','乙丙'])
        for value in [None,True,0,4,1.5,'2']:
            with self.subTest(value=value):
                self.assertEqual(self.json_request('POST','/api/segment',{**body,'split_at':value})[0],400)

    def test_calibration_api_copies_reference_only_and_rejects_invalid_gap(self):
        matched = cue('中毒的人', .2, 1)
        matched['reference_match'] = {
            'status': 'typo', 'query_text': '中毒的人',
            'reference': {'text': '中毒的人。', 'source_id': 'A', 'start': 0, 'end': 5}}
        body = {'mode': 'calibration', 'cues': [cue('中', 0, .2), matched],
                'max_width': 10, 'gap_seconds': .4}
        status, result = self.json_request('POST', '/api/segment', body)
        self.assertEqual(status, 200)
        self.assertEqual(result['mode'], 'reference_copy')
        self.assertEqual([c['text'] for c in result['cues']], ['中', '中毒的人'])
        self.assertEqual((result['cues'][0]['start'], result['cues'][0]['end']), (0, .2))
        self.assertEqual(result['changed_indices'], [])
        self.assertEqual(result['completed_indices'], [1])

    def test_invalid_schema_returns_400(self):
        cases = [("/api/segment", {"cues": [1]}), ("/api/segment", {"cues": [cue(words=[1])]}),
                 ("/api/srt/export", {"cues": [cue()], "options": []}),
                 ("/api/script/match", {"cues": [cue()], "lines": ["x"]}),
                 ("/api/projects/save", {"source_path": str(self.source), "cues": [cue()], "expected_revision": True}),
                 ("/api/config", {"layout": {"left": "bad"}})]
        for endpoint, body in cases:
            with self.subTest(endpoint=endpoint, body=body):
                self.assertEqual(self.request("POST", endpoint, body)[0], 400)
        self.assertEqual(self.request("POST", "/api/segment", raw=b'{"cues":[],"max_width":NaN}')[0], 400)

    def test_oversize_body_closes_connection(self):
        with patch.object(app, "MAX_BODY", 1):
            status, headers, _ = self.request("POST", "/api/config", {"theme": "light"})
            self.assertEqual(status, 400)
        self.assertEqual(self.request("POST", "/api/config", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 400)

    def test_standalone_srt_project_and_revision_conflict(self):
        source = self.folder / "standalone.srt"
        source.write_text(self.source.read_text(encoding="utf-8"), encoding="utf-8")
        body = {"source_path": str(source), "cues": [cue("已校对")], "expected_revision": 0}
        status, result = self.json_request("POST", "/api/projects/save", body)
        self.assertEqual(status, 200)
        self.assertEqual(result["revision"], 1)
        self.assertEqual(self.request("POST", "/api/projects/save", body)[0], 409)
        status, loaded = self.json_request("GET", "/api/projects/load?path=" + quote(str(source)))
        self.assertEqual(loaded["project"]["cues"][0]["text"], "已校对")
        self.assertEqual(loaded["project"]["media_path"], "")
        body["expected_revision"] = 1
        self.assertEqual(self.json_request("POST", "/api/projects/save", body)[1]["revision"], 2)

    def test_concurrent_save_only_one_same_revision_wins(self):
        source = self.folder / "concurrent.srt"
        source.write_text("", encoding="utf-8")
        body = {"source_path": str(source), "cues": [cue()], "expected_revision": 0}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: self.request("POST", "/api/projects/save", body)[0], range(2)))
        self.assertEqual(sorted(statuses), [200, 409])

    def test_missing_source_project_can_load_save_and_keep_conflict_protection(self):
        source=self.folder/'moved.srt';source.write_text('',encoding='utf-8')
        body={'source_path':str(source),'cues':[cue('原字幕')],'expected_revision':0}
        self.assertEqual(self.json_request('POST','/api/projects/save',body)[0],200)
        source.unlink()
        status,loaded=self.json_request('GET','/api/projects/load?path='+quote(str(source)))
        self.assertEqual(status,200);self.assertFalse(loaded['source_available'])
        self.assertEqual(loaded['project']['cues'][0]['text'],'原字幕')
        self.assertEqual(self.json_request('POST','/api/projects/save',body)[0],409)
        body.update(expected_revision=1,cues=[cue('失联后编辑')])
        self.assertEqual(self.json_request('POST','/api/projects/save',body)[1]['revision'],2)
        unknown=self.folder/'never-saved.srt'
        body.update(source_path=str(unknown),expected_revision=0)
        self.assertEqual(self.json_request('POST','/api/projects/save',body)[0],404)

    def test_missing_media_keeps_identity_for_relinking_by_content(self):
        source=self.folder/'identity.wav'
        with wave.open(str(source),'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000);output.writeframes(b'\0\0'*160)
        content=source.read_bytes()
        body={'source_path':str(source),'cues':[cue('保留字幕')],'initial_cues':[cue('原始字幕')],'expected_revision':0}
        self.assertEqual(self.json_request('POST','/api/projects/save',body)[0],200)
        source.unlink();body['expected_revision']=1
        self.assertEqual(self.json_request('POST','/api/projects/save',body)[0],200)
        moved=self.folder/'new-name.wav';moved.write_bytes(content)
        status,result=self.json_request('GET','/api/projects/load?path='+quote(str(moved)))
        self.assertEqual(status,200);self.assertEqual(result['project']['cues'][0]['text'],'保留字幕')
        self.assertEqual(result['project']['initial_cues'][0]['text'],'原始字幕')
        self.assertEqual(result['revision'],0)

    def test_conflict_backup_preserves_full_local_project_without_overwriting_remote(self):
        source = self.folder / 'backup-source.srt'
        source.write_text('', encoding='utf-8')
        body = {'source_path': str(source), 'cues': [cue('远端')], 'expected_revision': 0}
        self.assertEqual(self.request('POST', '/api/projects/save', body)[0], 200)
        body.update(cues=[cue('本页')], initial_cues=[cue('最初')], script_name='参考')
        status, backup = self.json_request('POST', '/api/projects/backup', body)
        self.assertEqual(status, 200)
        saved = json.loads(Path(backup['path']).read_text(encoding='utf-8'))
        self.assertEqual(saved['cues'][0]['text'], '本页')
        self.assertEqual(saved['initial_cues'][0]['text'], '最初')
        self.assertEqual(saved['script_name'], '参考')
        loaded = self.json_request('GET', '/api/projects/load?path=' + quote(str(source)))[1]
        self.assertEqual(loaded['project']['cues'][0]['text'], '远端')
        self.assertEqual(loaded['project']['revision'], 1)
        self.assertFalse(list(app.PROJECT_DIR.glob("*.tmp")))

    def test_cross_process_file_lock_serializes_revision_update(self):
        target = self.folder / "cross-process.json"
        code = "import sys,json,time;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from core.storage import locked_file,atomic_json;p=Path(sys.argv[2]);\nwith locked_file(p):\n v=json.loads(p.read_text())['revision'] if p.exists() else 0\n time.sleep(.1)\n atomic_json(p,{'revision':v+1})\n"
        children = [subprocess.Popen([sys.executable, "-c", code, str(ROOT / "backend"), str(target)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for child in children:
            _, error = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, error.decode("utf-8", errors="replace"))
        self.assertEqual(json.loads(target.read_text())["revision"], 2)

    def test_export_encoding_collision_and_duration(self):
        body = {"cues": [cue()], "options": {"output_dir": str(self.folder / "export")}}
        outputs = []
        for _ in range(2):
            status, response = self.json_request("POST", "/api/srt/export", body)
            self.assertEqual(status, 200)
            outputs.append(Path(response["path"]))
        self.assertNotEqual(*outputs)
        self.assertEqual(outputs[0].read_bytes(), srt_io.format_srt([cue()]).encode("utf-8"))

    def test_mkv_export_api_contains_audio_and_subtitles_without_overwrite(self):
        import av
        audio=self.folder/'mkv-source.wav'
        with wave.open(str(audio),'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000);output.writeframes(b'\0\0'*16000)
        body={'format':'mkv','media_path':str(audio),'cues':[cue('校对字幕',.1,.4),cue('排除',.5,.8,exclude_export=True)]}
        paths=[]
        for _ in range(2):
            status,result=self.json_request('POST','/api/srt/export',body)
            self.assertEqual(status,200,result);paths.append(result['path'])
            self.assertEqual(result['count'],1)
            with av.open(result['path']) as exported:
                self.assertEqual([s.type for s in exported.streams],['audio','subtitle'])
                text=b''.join(bytes(p) for p in exported.demux(exported.streams.subtitles[0]) if p.size)
                self.assertIn('校对字幕'.encode(),text);self.assertNotIn('排除'.encode(),text)
        self.assertNotEqual(*paths)

    def test_saved_project_library_lists_old_project_and_persists_initial_and_review_status(self):
        source=self.folder/'library-test.srt';source.write_bytes(self.source.read_bytes())
        items=[cue('已校对',review_complete=True)]
        status,result=self.json_request('POST','/api/projects/save',{'source_path':str(source),'cues':items,
            'initial_cues':[cue('原文')],'settings':{'width':10},'expected_revision':0})
        self.assertEqual(status,200,result)
        status,result=self.json_request('GET','/api/projects')
        self.assertEqual(status,200,result)
        self.assertTrue(any(p['source_path']==str(source) and p['available'] and p['count']==1 for p in result['projects']))
        saved=self.json_request('GET','/api/projects/load?path='+quote(str(source)))[1]['project']
        self.assertTrue(saved['cues'][0]['review_complete']);self.assertEqual(saved['initial_cues'][0]['text'],'原文')

    def test_legacy_missing_reference_metadata_is_ignored_without_changing_real_cues(self):
        body={'source_path':str(self.source),'cues':[cue('真实字幕')],'expected_revision':0,
              'reference_gaps':[{'text':'旧版本的空白参考项','before_cue':0}]}
        status,saved=self.json_request('POST','/api/projects/save',body)
        self.assertEqual(status,200,saved)
        loaded=self.json_request('GET','/api/projects/load?path='+quote(str(self.source)))[1]['project']
        self.assertNotIn('reference_gaps',loaded)
        self.assertEqual([item['text'] for item in loaded['cues']],['真实字幕'])

    def test_reference_full_text_and_offsets_survive_project_save(self):
        text = '\r\n场景：清晨\r\n  我们明天去公园散步。\r\n'
        status, parsed = self.json_request('POST','/api/script/parse',{'text':text})
        self.assertEqual(status,200)
        self.assertEqual(parsed['document']['text'],text)
        status, matched = self.json_request('POST','/api/script/match',{
            'cues':[cue('我们明天去公圆散步')], 'lines':parsed['lines'], 'document':parsed['document']})
        self.assertEqual(status,200,matched)
        self.assertEqual(matched['cues'][0]['reference_match']['status'],'homophone')
        reference=matched['cues'][0]['reference_match']['reference']
        self.assertEqual(text[reference['start']:reference['end']],reference['text'])
        source=self.folder/'reference-project.srt'; source.write_bytes(self.source.read_bytes())
        body={'source_path':str(source),'cues':matched['cues'],'script_lines':matched['lines'],
              'script_document':parsed['document'],'expected_revision':0}
        self.assertEqual(self.json_request('POST','/api/projects/save',body)[0],200)
        loaded=self.json_request('GET','/api/projects/load?path='+quote(str(source)))[1]['project']
        self.assertEqual(loaded['script_document']['text'],text)
        self.assertEqual(loaded['cues'][0]['original_text'],'我们明天去公圆散步')

    def test_multiple_scripts_keep_continuous_order_unicode_positions_and_names_after_save(self):
        text = '开场😀\r\n我们明天去公园散步。\r\n' + '\n' + '下一场\n今天晚上一起回家。'
        status, parsed = self.json_request('POST','/api/script/parse',{'text':text})
        self.assertEqual(status,200)
        status, matched = self.json_request('POST','/api/script/match',{
            'cues':[cue('我们明天去公园散步',0,1),cue('今天晚上一起回家',1,2)],
            'document':parsed['document'],'lines':parsed['lines']})
        self.assertEqual(status,200,matched)
        refs=[item['reference_match']['reference'] for item in matched['cues']]
        self.assertLessEqual(refs[0]['end'],refs[1]['start'])
        for ref in refs:
            self.assertEqual(text[ref['start']:ref['end']],ref['text'])
        source=self.folder/'multi-script.srt';source.write_bytes(self.source.read_bytes())
        names=['第一份.txt','第二份.md']
        body={'source_path':str(source),'cues':matched['cues'],'script_lines':matched['lines'],
              'script_document':matched['document'],'script_names':names,'script_name':names[-1],'expected_revision':0}
        self.assertEqual(self.json_request('POST','/api/projects/save',body)[0],200)
        loaded=self.json_request('GET','/api/projects/load?path='+quote(str(source)))[1]['project']
        self.assertEqual(loaded['script_document']['text'],text)
        self.assertEqual(loaded['script_names'],names)
        self.assertEqual(loaded['script_name'],names[-1])
        body['script_names']=[{}];body['expected_revision']=1
        self.assertEqual(self.json_request('POST','/api/projects/save',body)[0],400)

    def test_imported_outro_is_only_flagged_and_remains_exportable(self):
        status,parsed=self.json_request('POST','/api/srt/parse',{'text':'1\n00:00:00,000 --> 00:00:30,000\n感谢观看点赞关注一键三连\n'})
        self.assertEqual(status,200,parsed)
        cue=parsed['cues'][0]
        self.assertTrue(cue['hallucination'])
        self.assertFalse(cue.get('exclude_export'))
        self.assertNotIn('automatic_exclusion',cue['hallucination'])

    def test_unordered_sources_survive_parse_save_load_and_rematch(self):
        documents=[{'name':'B.txt','text':'今天晚上一起回家吃饭。😀'},
                   {'name':'A.txt','text':'我们明天去公园散步。'}]
        status,parsed=self.json_request('POST','/api/script/parse',{'documents':documents})
        self.assertEqual(status,200,parsed)
        status,matched=self.json_request('POST','/api/script/match',{'document':parsed['document'],
            'cues':[cue('我们明天去公园散步',0,1),cue('今天晚上一起回家吃饭',1,2)]})
        self.assertEqual(status,200,matched)
        self.assertEqual([c['reference_match']['reference']['source_name'] for c in matched['cues']],['A.txt','B.txt'])
        source=self.folder/'unordered-project.srt';source.write_bytes(self.source.read_bytes())
        body={'source_path':str(source),'cues':matched['cues'],'script_lines':matched['lines'],
              'script_document':matched['document'],'script_names':['B.txt','A.txt'],'expected_revision':0}
        self.assertEqual(self.json_request('POST','/api/projects/save',body)[0],200)
        loaded=self.json_request('GET','/api/projects/load?path='+quote(str(source)))[1]['project']
        self.assertEqual(loaded['script_document']['sources'],matched['document']['sources'])
        status,again=self.json_request('POST','/api/script/match',{'document':loaded['script_document'],'cues':loaded['cues']})
        self.assertEqual(status,200,again);self.assertEqual(again['stats']['matched'],2)

    def test_srt_drop_upload_is_preserved_working_copy(self):
        content=srt_io.format_srt([cue('拖入字幕。')]).encode('utf-8')
        status,_,raw=self.request('POST','/api/upload?kind=srt',raw=content,
            headers={'Content-Type':'application/octet-stream','X-Upload-Name':quote('字幕.srt')})
        self.assertEqual(status,200,raw)
        saved=Path(json.loads(raw)['path'])
        self.assertEqual(saved.read_bytes(),content)
        self.assertIn(self.folder/'UPLOADS_DIR',saved.parents)
        self.assertEqual(self.json_request('POST','/api/srt/parse',{'path':str(saved)})[1]['cues'][0]['text'],'拖入字幕。')

    def test_alignment_job_and_catalog_contract(self):
        import core.alignment as alignment
        self.assertEqual(len(self.json_request('GET','/api/models')[1]['models']),3)
        self.assertEqual(self.json_request('POST','/api/models/download',{'model':'not-a-model'})[0],400)
        request_id=hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:16]
        items=[cue('确认过的字幕',original_text='原始 ASR',original_start=0,original_end=1)]
        aligned=[dict(items[0],start=.1,end=1.1)]
        with patch.object(alignment,'align_cues',return_value=(aligned,{'duration':2})), \
             patch.object(jobs,'RESULTS_DIR',self.folder), \
             patch.object(app,'list_auxiliary',return_value=[{'name':'qwen3-forcedaligner-0.6b','available':True}]):
            status,response=self.json_request('POST','/api/alignment/jobs',{'path':str(self.binary),'cues':items,'device':'cpu','request_id':request_id})
            self.assertEqual(status,200,response)
            app.JOBS.queue.join()
            completed=self.json_request('GET','/api/jobs/'+request_id+'?result=1')[1]['job']
            self.assertEqual(completed['task'],'align')
            self.assertEqual(completed['cues'][0]['text'],'确认过的字幕')
            self.assertEqual(completed['cues'][0]['original_text'],'原始 ASR')
            self.assertEqual(completed['cues'][0]['original_start'],0)

    def test_download_and_recognition_start_are_mutually_exclusive(self):
        release = threading.Event()
        class Downloads:
            status = 'idle'
            def get(self): return {'status':self.status}
            def start(self, name): self.status='downloading'; return self.get()
        def transcribe(*args, **kwargs):
            release.wait(3)
            return [cue()], {}
        identity=hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:16]
        with patch.object(app,'DOWNLOADS',Downloads()), patch.object(jobs,'transcribe',side_effect=transcribe), \
             patch.object(jobs,'RESULTS_DIR',self.folder), \
             patch.object(app,'list_models',return_value=[{'name':'medium','available':True}]):
            try:
                requests=[('/api/jobs',{'path':str(self.binary),'model':'medium','request_id':identity}),
                          ('/api/models/download',{'model':'large-v3'})]
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    statuses=list(pool.map(lambda item:self.json_request('POST',item[0],item[1])[0],requests))
                self.assertEqual(sorted(statuses),[200,400])
            finally:
                release.set(); app.JOBS.queue.join()

    def test_export_options_filter_empty_and_never_readd_removed_punctuation(self):
        body = {"cues": [cue("你好，\n世界！", role="甲"), cue("！！", 1, 2), cue(" ", 2, 3)],
                "options": {"output_dir": str(self.folder / "options"), "keep_punctuation": False,
                            "add_punctuation": True, "single_line": True, "skip_empty": True, "with_role": True}}
        status, response = self.json_request("POST", "/api/srt/export", body)
        self.assertEqual(status, 200, response)
        self.assertEqual(response["count"], 1)
        exported, warnings, _ = srt_io.parse_srt_file(response["path"])
        self.assertFalse(warnings)
        self.assertEqual(exported[0]["text"], "甲：你好 世界")
        body["options"]["skip_empty"] = False
        self.assertEqual(self.json_request("POST", "/api/srt/export", body)[0], 400)
        body["options"]["single_line"] = "true"
        self.assertEqual(self.json_request("POST", "/api/srt/export", body)[0], 400)

    def test_job_idempotency_and_light_poll(self):
        request_id = hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:16]
        with patch.object(jobs, "transcribe", return_value=([cue()], {})), \
                patch.object(jobs, "RESULTS_DIR", self.folder), \
                patch.object(app, "list_models", return_value=[{"name": "medium", "available": True}]):
            payload = {"path": str(self.binary), "request_id": request_id, "model": "medium", "device": "cpu"}
            first = self.json_request("POST", "/api/jobs", payload)
            second = self.json_request("POST", "/api/jobs", payload)
            self.assertEqual(first[1]["job"]["id"], second[1]["job"]["id"])
            app.JOBS.queue.join()
            status, light = self.json_request("GET", "/api/jobs/" + request_id)
            self.assertNotIn("cues", light["job"])
            self.assertEqual(light["job"]["status"], "done")
            result = self.json_request("GET", "/api/jobs/" + request_id + "?result=1")[1]
            self.assertEqual(result["job"]["cues"][0]["text"], "你好")
            subtitle = Path(result["job"]["subtitle_path"])
            self.assertTrue(subtitle.is_file())
            self.assertEqual(srt_io.parse_srt_file(str(subtitle))[0][0]["text"], "你好")

    def test_interjections_are_removed_when_off_and_marked_when_kept(self):
        heard = [cue("哈哈哈", 0, .6), cue("你呀，快走", .6, 1.6)]

        def run(extra):
            request_id = hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:16]
            with patch.object(jobs, "transcribe", return_value=(copy.deepcopy(heard), {})), \
                    patch.object(jobs, "RESULTS_DIR", self.folder), \
                    patch.object(app, "list_models", return_value=[{"name": "medium", "available": True}]):
                status, response = self.json_request("POST", "/api/jobs", {
                    "path": str(self.binary), "model": "medium", "device": "cpu",
                    "request_id": request_id, **extra})
                self.assertEqual(status, 200, response)
                app.JOBS.queue.join()
                return self.json_request("GET", "/api/jobs/" + request_id + "?result=1")[1]["job"]

        default = run({})
        # 关闭“语气词”：整条笑声直接删除，不再留一条空字幕等待校准。
        self.assertEqual([item["text"] for item in default["cues"]], ["你呀，快走"])
        self.assertEqual(default["info"]["interjections_removed"], 1)
        self.assertNotIn("interjections", default["info"])
        self.assertFalse(default["cues"][0].get("exclude_export"))
        self.assertFalse(default["keep_interjections"])
        kept = run({"keep_interjections": True})
        self.assertEqual([item["text"] for item in kept["cues"]], ["哈哈哈", "你呀，快走"])
        self.assertEqual(kept["info"]["interjections"]["count"], 1)
        self.assertEqual(kept["info"]["interjections"]["texts"], ["哈哈哈"])
        self.assertTrue(kept["cues"][0]["interjection"])
        self.assertTrue(kept["keep_interjections"])
        # 旧字段仍可提交，true 表示关闭“保留语气词”。
        legacy = run({"drop_interjections": True})
        self.assertFalse(legacy["keep_interjections"])

    def test_cancelled_queued_job_balances_queue(self):
        entered, release = threading.Event(), threading.Event()
        def fake(*args, **kwargs):
            entered.set()
            release.wait(3)
            return [cue()], {}
        with patch.object(jobs, "transcribe", side_effect=fake), patch.object(jobs, "RESULTS_DIR", self.folder):
            first = app.JOBS.submit(str(self.binary), "medium", "cpu", True)
            self.assertTrue(entered.wait(2))
            second = app.JOBS.submit(str(self.binary), "medium", "cpu", True)
            app.JOBS.cancel(second.id)
            release.set()
            app.JOBS.queue.join()
            self.assertEqual(second.status, "cancelled")
            self.assertEqual(app.JOBS.queue.unfinished_tasks, 0)

    def test_decode_can_cancel_during_frames(self):
        path = self.folder / "audio.wav"
        with wave.open(str(path), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(16000)
            fh.writeframes(b"\0\0" * 16000)
        event = threading.Event()
        event.set()
        with self.assertRaises(media.DecodeCancelled):
            media.decode_audio(str(path), cancel_event=event)

    def test_streaming_waveform_and_error_reporting(self):
        path = self.folder / "wave.wav"
        with wave.open(str(path), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(16000)
            fh.writeframes(b"\0\0" * 32000)
        with patch.object(media, "CACHE_DIR", self.folder):
            result = media.waveform(str(path), 100)
            self.assertIsNone(result["error"])
            self.assertLessEqual(len(result["peaks"]), 100)
            self.assertAlmostEqual(result["duration"], 2)
            self.assertTrue(all(p == 0 for p in result["peaks"]))
            invalid = media.waveform(str(self.binary), 100)
            self.assertTrue(invalid["error"])

    def test_zoom_waveform_decodes_requested_time_region(self):
        path = self.folder / "region.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(array("h", [int(10000 * math.sin(2 * math.pi * 440 * i / 16000))
                                       if i < 16000 else 0 for i in range(32000)]).tobytes())
        with patch.object(media, "CACHE_DIR", self.folder):
            voiced = media.waveform_region(str(path), .2, .5, 200)
            silent = media.waveform_region(str(path), 1.5, 1.9, 200)
            self.assertGreater(max(voiced["peaks"]), .2)
            self.assertTrue(all(value == 0 for value in silent["peaks"]))
            self.assertEqual((voiced["start"], voiced["end"], voiced["buckets"]), (.2, .5, 200))
            url = "/api/waveform?path=" + quote(str(path)) + "&start=.2&end=.5&buckets=200"
            status, result = self.json_request("GET", url)
            self.assertEqual(status, 200)
            self.assertGreater(max(result["peaks"]), .2)
            self.assertEqual(self.request("GET", "/api/waveform?path=" + quote(str(path)) + "&start=nan&end=.5")[0], 400)


@unittest.skipUnless(os.name == "nt", "Windows窗口图标测试")
class WindowIconTests(unittest.TestCase):
    def test_two_tone_icon_with_transparent_background(self):
        assets = ROOT / "frontend/assets"
        self.assertEqual((assets / "studio-film.ico").read_bytes()[:4], b"\0\0\1\0")
        svg = (assets / "studio-film.svg").read_text(encoding="utf-8")
        self.assertIn('fill="#111111"', svg)
        self.assertIn('fill="#ffffff"', svg)

    def test_native_window_icon_and_taskbar_properties(self):
        import ctypes as c
        from ctypes import wintypes as w
        from core.window_icon import set_window_icon
        user = c.windll.user32
        user.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, c.c_int, c.c_int,
                                        c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p]
        user.CreateWindowExW.restype = w.HWND
        user.DestroyWindow.argtypes = [w.HWND]
        user.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        user.SendMessageW.restype = c.c_ssize_t
        hwnd = user.CreateWindowExW(0, "STATIC", "SubtitleStudio icon test", 0, 0, 0, 100, 100, None, None, None, None)
        self.assertTrue(hwnd)
        try:
            result = set_window_icon(hwnd, ROOT / "frontend/assets/studio-film.ico", ROOT / "main.py")
            self.assertTrue(result["window_icon"])
            self.assertTrue(result["taskbar"])
            self.assertTrue(user.SendMessageW(hwnd, 0x7F, 0, 0))
            self.assertTrue(user.SendMessageW(hwnd, 0x7F, 1, 0))
        finally:
            user.DestroyWindow(hwnd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
