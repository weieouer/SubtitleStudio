"""播放工作区的真实文本/时间处理与本段任务契约。"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave
import av
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from core import segment, srt_io, media, jobs
from core.reference_match import match_reference
from core.audio_boundaries import acoustic_features, speech_intervals, refine_boundaries
import app


class WorkspaceTests(unittest.TestCase):
    def test_effect_before_text_timestamp_cannot_advance_subtitle_start(self):
        audio=np.zeros(32000,dtype=np.float32)
        audio[15000:15700]=.8*np.sin(2*np.pi*900*np.arange(700)/16000)
        audio[17100:20500]=.12*np.sin(2*np.pi*220*np.arange(3400)/16000)
        cue={'start':1.0,'end':1.3,'text':'speech','words':[{'word':'speech','start':1.07,'end':1.28}]}
        result=refine_boundaries([cue],audio)[0]
        self.assertGreaterEqual(result['start'],cue['start'])
        self.assertEqual(result['audio_refinement']['onset_policy'],'never_advance_from_energy')
        self.assertEqual(result['asr_original']['start'],1.0)

    def test_rms_flux_refinement_trims_silence_and_preserves_original_text_and_time(self):
        audio=np.zeros(16000,dtype=np.float32)
        t=np.arange(4800)/16000
        audio[5600:10400]=.15*np.sin(2*np.pi*220*t)
        items=[{'start':.25,'end':.75,'text':'原始字幕'}]
        result=refine_boundaries(items,audio)
        self.assertAlmostEqual(result[0]['start'],.34,delta=.025)
        self.assertAlmostEqual(result[0]['end'],.66,delta=.025)
        self.assertEqual(result[0]['asr_original']['start'],.25)
        self.assertEqual(result[0]['original_end'],.75)
        self.assertEqual(result[0]['text'],items[0]['text'])
        self.assertEqual(items[0]['start'],.25)
        constant=refine_boundaries(items,np.ones(16000,dtype=np.float32)*.1)
        self.assertEqual((constant[0]['start'],constant[0]['end']),(.25,.75))
        self.assertEqual(constant[0]['audio_refinement']['status'],'uncertain')

    def test_hysteresis_ignores_short_energy_dips_and_monotonic_guard_removes_small_overlap(self):
        energy=np.r_[np.zeros(20),np.ones(15),np.zeros(2),np.ones(15),np.zeros(20)]
        intervals,threshold=speech_intervals(energy,np.zeros(len(energy)))
        self.assertEqual(len(intervals),1);self.assertGreater(threshold['on'],threshold['off'])
        audio=np.ones(16000,dtype=np.float32)*.1
        result=refine_boundaries([{'start':.1,'end':.55,'text':'一'},{'start':.5,'end':.9,'text':'二'}],audio)
        self.assertEqual(result[0]['end'],result[1]['start'])
        self.assertTrue(result[0]['audio_refinement']['monotonic_adjustment'])

    def test_dynamic_programming_preserves_words_text_order_and_limit(self):
        with patch.object(segment,'jieba') as tokenizer:
            tokenizer.lcut.return_value=['今天','我们','学习','字幕','编辑']
            chunks=segment.split_text('今天我们学习字幕编辑',6)
        self.assertEqual(''.join(chunks),'今天我们学习字幕编辑')
        self.assertTrue(all(len(c)<=6 for c in chunks))
        self.assertTrue(all(len(c)%2==0 for c in chunks))

    def test_mkv_remux_keeps_video_audio_and_current_subtitle_track(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.mkv';target=Path(temp)/'export.mkv'
            with av.open(str(source),'w') as container:
                video=container.add_stream('mpeg4',rate=10);video.width=64;video.height=48;video.pix_fmt='yuv420p'
                audio=container.add_stream('pcm_s16le',rate=16000);audio.layout='mono'
                for i in range(10):
                    frame=av.VideoFrame.from_ndarray(np.zeros((48,64,3),dtype=np.uint8),format='rgb24')
                    frame.pts=i
                    for packet in video.encode(frame):container.mux(packet)
                for packet in video.encode():container.mux(packet)
                frame=av.AudioFrame.from_ndarray(np.zeros((1,16000),dtype=np.int16),format='s16',layout='mono');frame.sample_rate=16000;frame.pts=0
                for packet in audio.encode(frame):container.mux(packet)
                for packet in audio.encode():container.mux(packet)
            media.remux_with_subtitles(str(source),srt_io.format_srt([{'start':.1,'end':.8,'text':'校对后的字幕'}]),str(target))
            with av.open(str(target)) as result:
                self.assertEqual([s.type for s in result.streams],['video','audio','subtitle'])
                packets=[bytes(p) for p in result.demux(result.streams.subtitles[0]) if p.size]
                self.assertIn('校对后的字幕'.encode(),b''.join(packets))
            with av.open(str(target)) as result:
                self.assertEqual(sum(1 for _ in result.decode(video=0)),10)
            with av.open(str(target)) as result:
                self.assertEqual(sum(f.samples for f in result.decode(audio=0)),16000)

    def test_reimport_same_content_restores_project_but_same_name_different_content_does_not(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);projects=folder/'projects';projects.mkdir()
            first=folder/'first.wav';first.write_bytes(b'audio-content')
            copy=folder/'another.wav';copy.write_bytes(first.read_bytes())
            project={'media_path':str(first),'media_identity':app._media_identity(str(first)),
                     'revision':3,'updated_at':'2026-11-05 01:00:00','cues':[{'start':0,'end':1,'text':'原文'}]}
            with patch.object(app,'PROJECT_DIR',projects):
                app.atomic_json(app._project_path(str(first)),project)
                restored,revision=app._load_project(str(copy))
                self.assertEqual(restored['cues'][0]['text'],'原文')
                self.assertEqual(revision,0)  # 新目录的保存版本从 0 起，不覆盖旧工程。
                self.assertEqual(app._load_project(str(first))[1],3)
                copy.write_bytes(b'other-content')
                self.assertIsNone(app._load_project(str(copy))[0])

    def test_character_limit_preserves_text_and_time(self):
        for text in ('十个字的限制应该真的有效。下一句话也需要处理！','ABCDEFGHIJKLMNOPQRSTUVWXYZ','中文 English 1234567890。'):
            parts,_=segment.segment_cue({'start':0,'end':10,'text':text},10)
            self.assertEqual(''.join(p['text'] for p in parts),text)
            self.assertTrue(all(len(p['text'])<=10 for p in parts))
            self.assertEqual(parts[0]['start'],0)
            self.assertEqual(parts[-1]['end'],10)

    def test_excluded_cues_not_exported(self):
        items=[{'start':0,'end':1,'text':'保留'}, {'start':1,'end':2,'text':'不要','review_decision':'non_script'}, {'start':2,'end':3,'text':'不要','exclude_export':True}]
        self.assertEqual([c['text'] for c in srt_io.prepare_export_cues(items)],['保留'])
        self.assertEqual(len(items),3)

    def test_reference_scope_uses_absolute_original_positions(self):
        script='我们今天去公园。\n中间场景。\n我们今天去公园。'
        start=script.rfind('我们')
        result=match_reference([{'start':0,'end':1,'text':'我们今天去公园'}],{'text':script},position_bounds=[start,len(script)])
        match=result['cues'][0]['reference_match']
        self.assertEqual(match['reference']['start'],start)
        self.assertEqual(match['status'],'high')

    def test_region_job_decodes_only_requested_audio_and_offsets_result(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);path=folder/'source.wav'
            with wave.open(str(path),'wb') as out:
                out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000);out.writeframes(b'\0\0'*32000)
            self.assertEqual(len(media.decode_audio(str(path),start=.5,end=1.0)),8000)
            def transcribe(clip,*args,**kwargs):
                self.assertEqual(clip,str(path))
                self.assertEqual(len(kwargs['audio']),8000)
                return [{'start':0,'end':.5,'text':'新的字幕','words':[]}],{}
            with patch.object(jobs,'RESULTS_DIR',folder),patch.object(jobs,'transcribe',side_effect=transcribe):
                manager=jobs.JobManager()
                job=manager.submit(str(path),'large-v3','cpu',False,0,task='asr_region',input_cues=[{'start':.5,'end':1}],max_width=10)
                manager.queue.join()
                self.assertEqual(job.status,'done',job.error)
                self.assertEqual((job.cues[0]['start'],job.cues[0]['end']),(.5,1))


if __name__=='__main__':unittest.main()
