"""字幕消除的真实媒体时间戳、选区约束、任务去重和失败清理。"""
import hashlib
import json
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import Mock, patch

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import app
import removal_runner
from core import removal_jobs, removal_scan
from removal_mask import text_regions


def video_file(path, frames=47, audio=False):
    with av.open(str(path), 'w') as output:
        video = output.add_stream('libx264', rate=24)
        video.width, video.height, video.pix_fmt = 96, 64, 'yuv420p'
        tracks = []
        if audio:
            for _ in range(2):
                stream = output.add_stream('pcm_s16le', rate=48000)
                stream.layout = 'mono'
                tracks.append(stream)
        for i in range(frames):
            image = np.full((64, 96, 3), i % 80, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(image, format='rgb24')
            frame.pts, frame.time_base = i, Fraction(1, 24)
            for packet in video.encode(frame):
                output.mux(packet)
        for packet in video.encode():
            output.mux(packet)
        if audio:
            for stream in tracks:
                for i in range(12):
                    frame = av.AudioFrame.from_ndarray(np.zeros((1, 4096), dtype=np.int16), format='s16', layout='mono')
                    frame.sample_rate, frame.pts, frame.time_base = 48000, i * 4096, Fraction(1, 48000)
                    for packet in stream.encode(frame):
                        output.mux(packet)
                for packet in stream.encode():
                    output.mux(packet)


class PaintedRegion:
    def __init__(self, request):
        pass

    def process(self, arrays, active, geometry):
        x0, y0, x1, y1 = geometry[0]
        results = [image.copy() for image in arrays]
        for image, enabled in zip(results, active):
            if enabled:
                image[y0:y1, x0:x1] = 240
        return results


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'source.mkv'
        video_file(self.source)

    def request(self, **values):
        result = {'path': str(self.source), 'region': {'x': .2, 'y': .5, 'width': .6, 'height': .2},
                  'start': 0, 'end': 1, 'output_dir': str(self.root), 'max_edge': 512, 'request_id': 'a' * 32}
        result.update(values)
        return result

    def test_invalid_regions_and_times_are_rejected_before_model_is_used(self):
        for region in ({'x': .8, 'y': .5, 'width': .5, 'height': .1}, {'x': 0, 'y': 0, 'width': 0, 'height': 0},
                       {'x': False, 'y': 0, 'width': .5, 'height': .1}):
            with self.assertRaises(ValueError):
                removal_jobs.validate_request(self.request(region=region))
        with self.assertRaises(ValueError):
            removal_jobs.validate_request(self.request(start=1.2, end=1))
        with self.assertRaises(ValueError):
            removal_jobs.validate_request(self.request(max_edge=2048))

    def test_audio_only_source_is_rejected(self):
        audio = self.root / 'audio.wav'
        with av.open(str(audio), 'w') as output:
            stream = output.add_stream('pcm_s16le', rate=16000)
            stream.layout = 'mono'
            frame = av.AudioFrame.from_ndarray(np.zeros((1, 1600), dtype=np.int16), format='s16', layout='mono')
            frame.sample_rate = 16000
            for packet in stream.encode(frame):
                output.mux(packet)
        with self.assertRaises(ValueError):
            removal_jobs.video_info(str(audio))

    def test_hdr_input_is_rejected_instead_of_silently_reducing_color_depth(self):
        stream = Mock()
        stream.codec_context.format.components = [Mock(bits=10)]
        container = Mock()
        container.streams.video = [stream]
        with patch.object(removal_jobs.media, 'probe', return_value={'width': 96, 'height': 64, 'duration': 1}), \
                patch.object(removal_jobs.av, 'open') as open_video:
            open_video.return_value.__enter__.return_value = container
            with self.assertRaisesRegex(ValueError, 'HDR'):
                removal_jobs.video_info(str(self.source))

    def test_bounded_windows_emit_each_frame_once_and_keep_pts(self):
        request = self.request(preview=False)
        request.update(source=str(self.root), model_dir=str(self.root), device='cpu', work_dir=str(self.root),
                       repaired=str(self.root / 'repaired.mp4'), progress_file=str(self.root / 'progress.json'),
                       result_file=str(self.root / 'result.json'))
        removal_runner.run(request, processor_factory=PaintedRegion)
        report = json.loads(Path(request['result_file']).read_text())
        self.assertEqual(report['frames'], 47)
        with av.open(str(self.source)) as original, av.open(request['repaired']) as repaired:
            first = list(original.decode(video=0))
            second = list(repaired.decode(video=0))
            self.assertEqual(len(second), 47)
            np.testing.assert_allclose([float(f.time) for f in first], [float(f.time) for f in second], atol=.001)
            self.assertGreater(second[5].to_ndarray(format='rgb24')[36, 30, 0], 200)
            self.assertLess(second[5].to_ndarray(format='rgb24')[5, 5, 0], 20)
            self.assertLess(second[35].to_ndarray(format='rgb24')[36, 30, 0], 100)

    def test_preview_only_contains_selected_range_and_starts_at_zero(self):
        request = self.request(preview=True, start=.5, end=1)
        request.update(source=str(self.root), model_dir=str(self.root), device='cpu', work_dir=str(self.root),
                       repaired=str(self.root / 'preview.mp4'), progress_file=str(self.root / 'progress.json'),
                       result_file=str(self.root / 'result.json'))
        removal_runner.run(request, processor_factory=PaintedRegion)
        with av.open(request['repaired']) as output:
            frames = list(output.decode(video=0))
            self.assertEqual(len(frames), 12)
            self.assertEqual(frames[0].time, 0)
        self.assertEqual(json.loads(Path(request['result_file']).read_text())['origin_start'], .5)

    def test_mux_keeps_both_original_audio_tracks(self):
        source = self.root / 'audio-source.mkv'
        video_file(source, audio=True)
        target = self.root / 'output.mkv'
        removal_jobs.mux_audio(source, self.source, target)
        with av.open(str(target)) as output:
            self.assertEqual(len(output.streams.audio), 2)
            self.assertEqual(len(list(output.decode(video=0))), 47)

    def test_duplicate_request_reuses_task_and_different_parameters_fail(self):
        manager = removal_jobs.RemovalJobs()
        with patch.object(removal_jobs, 'model_status', return_value={'available': True, 'cuda_available': False, 'device': 'cpu'}), \
                patch.object(removal_jobs.threading, 'Thread'):
            first = manager.submit(self.request())
            second = manager.submit(self.request())
            self.assertEqual(first['id'], second['id'])
            with self.assertRaises(ValueError):
                manager.submit(self.request(end=.9))

    def test_worker_failure_removes_own_placeholder_without_touching_input(self):
        manager = removal_jobs.RemovalJobs()
        request = removal_jobs.validate_request(self.request())
        manager.jobs[request['id']] = {'id': request['id'], 'status': 'queued', 'started': 0}
        import threading
        manager.events[request['id']] = threading.Event()
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        process = Mock(returncode=1)
        process.poll.return_value = 1
        with patch.object(removal_jobs, 'RESULTS_DIR', self.root / 'jobs'), patch.object(removal_jobs.subprocess, 'Popen', return_value=process):
            manager._run(request, {'device': 'cpu'})
        self.assertEqual(manager.jobs[request['id']]['status'], 'error')
        self.assertFalse(list(self.root.glob('*_无字幕*')))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), digest)

    def test_asr_and_alignment_cannot_start_during_video_repair(self):
        with patch.object(app.REMOVAL_JOBS, 'busy', return_value=True):
            with self.assertRaises(ValueError):
                app.Handler._create_job_locked(Mock(), {})
            with self.assertRaises(ValueError):
                app.Handler._create_alignment_job(Mock(), {})

    def test_large_frame_context_stays_in_bounds_and_multiple_of_eight(self):
        roi, crop, size = removal_runner.crop_geometry(1920, 1080, {'x': 0, 'y': .9, 'width': 1, 'height': .1}, 512)
        self.assertEqual(roi, (0, 972, 1920, 1080))
        self.assertLessEqual(max(size), 512)
        self.assertTrue(all(n % 8 == 0 for n in size))
        self.assertGreaterEqual(crop[1], 0)


class RemovalTimelineTests(unittest.TestCase):
    def test_composite_keeps_every_pixel_outside_text_mask(self):
        original = np.random.default_rng(4).integers(0, 256, (32, 80, 3), dtype=np.uint8)
        mask = np.zeros((32, 80), dtype=bool)
        mask[12:22, 25:55] = True
        result = removal_runner.composite(original, np.full_like(original, 123), mask)
        np.testing.assert_array_equal(result[~mask], original[~mask])
        np.testing.assert_array_equal(result[15:19, 30:50], 123)
        np.testing.assert_array_equal(removal_runner.composite(original, original*0, mask*False), original)

    def test_scene_cut_splits_context_but_motion_does_not_split_every_frame(self):
        frames = [np.full((36, 64, 3), level, dtype=np.uint8) for level in (30, 32, 34, 210, 212)]
        self.assertEqual(list(removal_runner.scene_ranges(frames)), [(0, 3), (3, 5)])
        self.assertEqual(list(removal_runner.scene_ranges(frames[:1])), [(0, 1)])

    def test_text_map_filters_noise_and_keeps_two_separate_lines(self):
        probability = np.zeros((64, 128), dtype=np.float32)
        probability[0, 0] = 1
        probability[15:23, 30:80] = .9
        probability[40:48, 40:100] = .85
        _binary, regions = text_regions(probability, {})
        self.assertEqual(regions, [(30, 15, 80, 23), (40, 40, 100, 48)])
        self.assertEqual(text_regions(probability * .1, {})[1], [])

    def test_empty_or_invalid_enabled_segments_never_fall_back_to_full_video(self):
        for value in ([], [{'start':0,'end':1,'enabled':False}], [{'start':1,'end':0}], [{'start':0,'end':1,'enabled':1}]):
            with self.assertRaises(ValueError):
                removal_jobs.validate_segments(value, 3)
        self.assertEqual(removal_jobs.validate_segments([],2,require_enabled=False),[])

    def test_text_presence_samples_merge_into_separate_editable_segments(self):
        segments=removal_scan.merge_samples([(0,False),(.5,True),(.75,True),(1,False),(2,True),(2.25,True)],.25,3)
        self.assertEqual(len(segments),2)
        self.assertTrue(all(s['enabled'] and s['source']=='detected' for s in segments))
        self.assertEqual(removal_scan.merge_samples([(0,False),(.25,False)],.25,1),[])

    def test_scan_cancel_for_old_id_cannot_cancel_a_new_task(self):
        scanner=removal_scan.SubtitleScanner()
        scanner.state.update(id='new',status='running')
        scanner.history['old']={'id':'old','status':'done'}
        self.assertEqual(scanner.cancel('old')['id'],'old')
        self.assertFalse(scanner.event.is_set())

    def test_only_enabled_segments_are_processed(self):
        request={'start':0,'end':3,'segments':[{'start':.5,'end':1,'enabled':True},{'start':1.5,'end':2,'enabled':False}]}
        self.assertFalse(removal_runner.active_at(request,.25))
        self.assertTrue(removal_runner.active_at(request,.75))
        self.assertFalse(removal_runner.active_at(request,1.75))
        request['segments']=[]
        self.assertFalse(removal_runner.active_at(request,.75))

    def test_variable_frame_timestamps_keep_gaps_after_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'vfr.mp4'
            stamps=[0,40,120,160,330,900]
            with av.open(str(source),'w') as output:
                stream=output.add_stream('libx264',rate=25)
                stream.width,stream.height,stream.pix_fmt=96,64,'yuv420p'
                stream.time_base=stream.codec_context.time_base=Fraction(1,1000)
                for stamp in stamps:
                    frame=av.VideoFrame.from_ndarray(np.zeros((64,96,3),dtype=np.uint8),format='rgb24')
                    frame.pts,frame.time_base=stamp,Fraction(1,1000)
                    for packet in stream.encode(frame):output.mux(packet)
                for packet in stream.encode():output.mux(packet)
            request={'path':str(source),'region':{'x':.2,'y':.5,'width':.6,'height':.2},'start':0,'end':2,'preview':False,'max_edge':512,
                     'source':str(root),'model_dir':str(root),'device':'cpu','work_dir':str(root),'repaired':str(root/'out.mp4'),
                     'progress_file':str(root/'progress.json'),'result_file':str(root/'result.json')}
            removal_runner.run(request,processor_factory=PaintedRegion)
            with av.open(str(source)) as first,av.open(request['repaired']) as second:
                times=[float(f.time) for f in first.decode(video=0)]
                self.assertGreater(np.ptp(np.diff(times)),.1)
                np.testing.assert_allclose(times,[float(f.time) for f in second.decode(video=0)],atol=.001)


if __name__ == '__main__':
    unittest.main()
