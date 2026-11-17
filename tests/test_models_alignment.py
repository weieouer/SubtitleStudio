"""下载与对齐契约测试，使用本地字节与模拟响应，不下载模型权重。"""
from contextlib import ExitStack
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))
from core import model_manager as models
from core.alignment import apply_word_times, _resolve_overlaps
from core import asr, media
from core import model_sources as sources


class SuspectCaptionTests(unittest.TestCase):
    def tearDown(self):
        asr.clear_model_cache()

    def test_repeated_whisper_recognition_reuses_loaded_model_and_device_change_reloads(self):
        with patch('faster_whisper.WhisperModel') as engine, \
             patch.object(asr,'find_model',return_value=Path('fixture-cached-model')), \
             patch.object(media,'decode_audio',return_value=[]):
            engine.return_value.transcribe.side_effect=lambda *a,**k:(iter([]),SimpleNamespace(duration=2,language='zh'))
            asr.transcribe('clip1.wav','large-v3','cpu')
            asr.transcribe('clip2.wav','large-v3','cpu')
            self.assertEqual(engine.call_count,1)
            asr.transcribe('clip3.wav','large-v3','cuda')
            self.assertEqual(engine.call_count,2)

    def test_whisper_marks_suspect_caption_without_dropping_it(self):
        segment = SimpleNamespace(text='仍然保留这条字幕',start=.1,end=1.2,words=[],
                                  avg_logprob=-2.0,no_speech_prob=.95)
        with patch('faster_whisper.WhisperModel') as engine, \
             patch.object(asr,'find_model',return_value=Path('fixture-model')), \
             patch.object(media,'decode_audio',return_value=[]):
            engine.return_value.transcribe.return_value = (iter([segment]),SimpleNamespace(duration=2,language='zh'))
            cues, _ = asr.transcribe('fixture.wav','large-v3','cpu',vad=False)
            self.assertIsNone(engine.return_value.transcribe.call_args.kwargs['no_speech_threshold'])
        self.assertEqual(len(cues),1)
        self.assertEqual(cues[0]['text'],segment.text)
        self.assertTrue(cues[0]['is_noise'])
        self.assertTrue(cues[0]['needs_review'])


class Response(io.BytesIO):
    status=200
    headers={}


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.folder=Path(self.temp.name)
        self.stack=ExitStack()
        self.stack.enter_context(patch.object(models,'MODELS_DIR',self.folder/'models'))
        self.stack.enter_context(patch.object(models,'ENGINE_DIR',self.folder/'engines'))
        models.MODELS_DIR.mkdir()
        self.download=models.ModelDownloads()

    def tearDown(self):
        self.stack.close(); self.temp.cleanup()

    def test_unknown_download_and_builtin_refused(self):
        for name in ('https://example.com/m.bin','../escape','silero-vad'):
            with self.assertRaises(ValueError): self.download.start(name)

    def test_runtime_install_falls_back_and_checks_import_before_ready(self):
        processes=[SimpleNamespace(poll=lambda:1,returncode=1),SimpleNamespace(poll=lambda:0,returncode=0)]
        location=models.ENGINE_DIR/'qwen'
        with patch.object(models,'runtime_ready',return_value=False),patch.object(models,'desired_torch',return_value='cpu'), \
             patch.object(models.venv,'EnvBuilder') as builder, \
             patch.object(models.subprocess,'Popen',side_effect=processes) as install, \
             patch.object(models.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as verify:
            builder.return_value.create.side_effect=lambda path:Path(path).mkdir(parents=True,exist_ok=True)
            self.download._prepare('qwen')
            self.assertEqual(install.call_count,2)
            self.assertIn(models.RUNTIME_INDEXES[0],install.call_args_list[0].args[0])
            self.assertIn(models.RUNTIME_INDEXES[1],install.call_args_list[1].args[0])
            self.assertIn('Qwen3ForcedAligner',verify.call_args.args[0][-1])
            self.assertTrue((location/'ready.json').exists())

    def test_runtime_import_failure_does_not_mark_ready(self):
        location=models.ENGINE_DIR/'qwen'
        with patch.object(models,'runtime_ready',return_value=False),patch.object(models,'desired_torch',return_value='cpu'), \
             patch.object(models.venv,'EnvBuilder') as builder, \
             patch.object(models.subprocess,'Popen',return_value=SimpleNamespace(poll=lambda:0,returncode=0)), \
             patch.object(models.subprocess,'run',return_value=SimpleNamespace(returncode=1)):
            builder.return_value.create.side_effect=lambda path:Path(path).mkdir(parents=True,exist_ok=True)
            with self.assertRaises(RuntimeError):self.download._prepare('qwen')
            self.assertFalse((location/'ready.json').exists())

    def test_cuda_torch_installed_before_engine_packages(self):
        location=models.ENGINE_DIR/'qwen'
        processes=[SimpleNamespace(poll=lambda:0,returncode=0) for _ in range(3)]
        def fake_run(command,**kwargs):
            if any('torch.cuda.is_available' in str(part) for part in command):
                return SimpleNamespace(returncode=0,stdout='cuda\n',stderr='')
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(models,'runtime_ready',return_value=False),patch.object(models,'desired_torch',return_value='cuda'), \
             patch.object(models.venv,'EnvBuilder') as builder, \
             patch.object(models.subprocess,'Popen',side_effect=processes) as install, \
             patch.object(models.subprocess,'run',side_effect=fake_run):
            builder.return_value.create.side_effect=lambda path:Path(path).mkdir(parents=True,exist_ok=True)
            self.download._prepare('qwen')
            cuda_command=install.call_args_list[0].args[0]
            self.assertIn(models.engine_config()['torch']['cuda_index'],cuda_command)
            self.assertIn('torch',cuda_command)
            self.assertIn('--force-reinstall',cuda_command)
            self.assertIn('--upgrade',cuda_command)
            marker=json.loads((location/'ready.json').read_text(encoding='utf-8'))
            self.assertEqual(marker['torch'],'cuda')
            self.assertTrue(marker['cuda_available'])

    def test_failed_cuda_install_restores_cpu_and_keeps_runtime_ready(self):
        location=models.ENGINE_DIR/'qwen'
        processes=[SimpleNamespace(poll=lambda:1,returncode=1),SimpleNamespace(poll=lambda:0,returncode=0),
                   SimpleNamespace(poll=lambda:0,returncode=0)]
        def fake_run(command,**kwargs):
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(models,'runtime_ready',return_value=False),patch.object(models,'desired_torch',return_value='cuda'), \
             patch.object(models.venv,'EnvBuilder') as builder, \
             patch.object(models.subprocess,'Popen',side_effect=processes) as install, \
             patch.object(models.subprocess,'run',side_effect=fake_run):
            builder.return_value.create.side_effect=lambda path:Path(path).mkdir(parents=True,exist_ok=True)
            self.download._prepare('qwen')
            self.assertEqual(install.call_count,3)   # CUDA 失败 + CPU 恢复 + 引擎依赖
            self.assertIn('--force-reinstall',install.call_args_list[0].args[0])
            marker=json.loads((location/'ready.json').read_text(encoding='utf-8'))
            self.assertEqual(marker['torch'],'cpu')
            self.assertIn('CPU',marker['gpu_error'])
        (location/'Scripts').mkdir()
        (location/'Scripts'/'python.exe').touch()
        with patch.object(models,'desired_torch',return_value='cuda'):
            self.assertTrue(models.runtime_ready('qwen'))

    def test_gpu_upgrade_uses_force_reinstall_and_records_verified_cuda(self):
        location=models.ENGINE_DIR/'qwen'
        location.mkdir(parents=True)
        (location/'ready.json').write_text(json.dumps({'engine':'qwen','torch':'cpu'}),encoding='utf-8')
        def fake_run(command,**kwargs):
            if any('torch.cuda.is_available' in str(part) for part in command):
                return SimpleNamespace(returncode=0,stdout='cuda\n',stderr='')
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(models.subprocess,'Popen',return_value=SimpleNamespace(poll=lambda:0,returncode=0)) as install, \
             patch.object(models.subprocess,'run',side_effect=fake_run):
            self.download._run_gpu(['qwen'])
            command=install.call_args.args[0]
            self.assertIn(models.engine_config()['torch']['cuda_index'],command)
            self.assertIn('--force-reinstall',command)
            self.assertIn('--upgrade',command)
            marker=json.loads((location/'ready.json').read_text(encoding='utf-8'))
            self.assertEqual(marker['torch'],'cuda')
            self.assertTrue(marker['cuda_available'])
            self.assertEqual(self.download.get()['status'],'done')

    def test_gpu_upgrade_failure_restores_cpu_and_reports_retry(self):
        location=models.ENGINE_DIR/'qwen'
        location.mkdir(parents=True)
        (location/'ready.json').write_text(json.dumps({'engine':'qwen','torch':'cpu'}),encoding='utf-8')
        processes=[SimpleNamespace(poll=lambda:1,returncode=1),SimpleNamespace(poll=lambda:0,returncode=0)]
        with patch.object(models.subprocess,'Popen',side_effect=processes) as install, \
             patch.object(models.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')):
            self.download._run_gpu(['qwen'])
            self.assertEqual(install.call_count,2)
            marker=json.loads((location/'ready.json').read_text(encoding='utf-8'))
            self.assertEqual(marker['torch'],'cpu')
            self.assertIn('CPU',marker['gpu_error'])
            self.assertEqual(self.download.get()['status'],'error')
            (location/'Scripts').mkdir()
            (location/'Scripts'/'python.exe').touch()
            with patch.object(models,'desired_torch',return_value='cuda'):
                self.assertTrue(models.runtime_ready('qwen'))

    def test_gpu_download_requires_an_existing_engine_environment(self):
        with self.assertRaises(ValueError):self.download.start_gpu()

    def test_gpu_status_distinguishes_missing_cpu_cuda_and_fallback(self):
        with patch.object(models,'nvidia_gpu_present',return_value=True):
            status=models.gpu_status()
            self.assertEqual(status['state'],'missing')
            self.assertFalse(status['downloadable'])
            qwen=models.ENGINE_DIR/'qwen';qwen.mkdir(parents=True)
            (qwen/'Scripts').mkdir()
            (qwen/'Scripts'/'python.exe').touch()
            (qwen/'ready.json').write_text(json.dumps({'engine':'qwen','torch':'cpu'}),encoding='utf-8')
            self.assertEqual(models.gpu_status()['state'],'cpu')
            (qwen/'ready.json').write_text(json.dumps({'engine':'qwen','torch':'cuda','cuda_available':True}),encoding='utf-8')
            self.assertEqual(models.gpu_status()['state'],'cuda')
            (qwen/'ready.json').write_text(json.dumps({'engine':'qwen','torch':'cuda','cuda_available':False,
                                                        'gpu_error':'CUDA 已安装，但当前显卡/驱动不可用'}),encoding='utf-8')
            self.assertEqual(models.gpu_status()['state'],'failed')
            (qwen/'ready.json').write_text(json.dumps({'engine':'qwen','torch':'cpu','gpu_error':'GPU 安装失败，已恢复 CPU'}),encoding='utf-8')
            status=models.gpu_status()
            self.assertEqual(status['state'],'failed')
            self.assertIn('CPU',status['error'])

    def test_catalog_primary_three_and_builtin_vad(self):
        self.assertEqual(list(models.CATALOG),['large-v3','fireredasr2-aed','qwen3-asr-1.7b'])
        self.assertEqual(set(models.AUXILIARY),{'qwen3-forcedaligner-0.6b','silero-vad'})
        self.assertTrue(models.weights_present('silero-vad'))

    def test_local_model_aliases_ready_without_runtime_or_weight_download(self):
        whisper=models.MODELS_DIR/'Whisper large-v3'; whisper.mkdir()
        (whisper/'model.bin').write_bytes(b'fixture')
        self.assertIsNone(asr.find_model('large-v3'))
        for filename in ('config.json','tokenizer.json'):(whisper/filename).write_bytes(b'{}')
        self.assertEqual(asr.find_model('large-v3'),whisper)
        aligner=models.MODELS_DIR/'ForcedAligner-0.6b'; aligner.mkdir()
        files=('config.json','model.safetensors','tokenizer_config.json','vocab.json','merges.txt','preprocessor_config.json')
        for filename in files:(aligner/filename).write_bytes(b'fixture')
        self.assertEqual(asr.find_model('large-v3'),whisper)
        item=next(m for m in models.list_auxiliary() if m['name']=='qwen3-forcedaligner-0.6b')
        self.assertTrue(item['available'])
        self.assertTrue(item['downloaded'])
        self.assertFalse(item['runtime_ready'])
        with patch.object(self.download,'_files',side_effect=AssertionError('must not query weights')),patch.object(self.download,'_prepare') as prepare:
            self.download._run('qwen3-forcedaligner-0.6b')
        prepare.assert_called_once_with('qwen')
        self.assertEqual(self.download.get()['status'],'done')

    def test_resume_download_verifies_hash_before_atomic_publish(self):
        payload=b'a real small model fixture'
        target=models.MODELS_DIR/'large-v3'/'model.bin'
        target.parent.mkdir()
        partial=target.with_name('model.bin.part'); partial.write_bytes(payload[:5])
        item={'name':'model.bin','size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
        def request(url,headers):
            self.assertEqual(headers,{'Range':'bytes=5-'})
            response=Response(payload[5:]); response.status=206
            response.headers={'Content-Range':f'bytes 5-{len(payload)-1}/{len(payload)}'}
            return response
        with patch.object(self.download,'_request',side_effect=request):
            self.download._download_file('large-v3','revision',item,0,len(payload))
        self.assertEqual(target.read_bytes(),payload)
        self.assertFalse(partial.exists())

    def test_completed_partial_resumes_without_http_416(self):
        target=models.MODELS_DIR/'large-v3'/'model.bin'; target.parent.mkdir()
        target.with_name('model.bin.part').write_bytes(b'abc')
        with patch.object(self.download,'_request',side_effect=AssertionError('must not request')):
            self.download._download_file('large-v3','revision',{'name':'model.bin','size':3},0,3)
        self.assertEqual(target.read_bytes(),b'abc')

    def test_manifest_traversal_rejected(self):
        response=Response(json.dumps({'sha':'a'*40,'siblings':[{'rfilename':'../escape','size':1}]}).encode())
        with patch.object(self.download,'_request',return_value=response):
            with self.assertRaises(ValueError): sources.source_manifest('hf-mirror','fixture/repo',self.download._request,self.download.check)

    def test_source_failure_switches_and_resumes_same_hash(self):
        payload=b'content with the same hash'
        target=models.MODELS_DIR/'large-v3'/'model.bin'; target.parent.mkdir()
        target.with_name('model.bin.part').write_bytes(payload[:4])
        item={'name':'model.bin','size':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),
              'urls':[{'url':'https://modelscope.cn/fixture','label':'MS'},{'url':'https://hf-mirror.com/fixture','label':'HF'}]}
        urls=[]
        def request(url,headers):
            urls.append(url)
            if len(urls)==1: raise OSError('connection reset')
            self.assertEqual(headers['Range'],'bytes=4-')
            response=Response(payload[4:]); response.status=206
            response.headers={'Content-Range':f'bytes 4-{len(payload)-1}/{len(payload)}'}
            return response
        with patch.object(self.download,'_request',side_effect=request), patch.object(self.download.cancel_event,'wait'):
            self.download._download_file('large-v3','revision',item,0,len(payload))
        self.assertEqual(target.read_bytes(),payload)
        self.assertEqual(len(urls),2)
        self.assertEqual(self.download.get()['source'],'HF')
        self.assertGreater(self.download.get()['speed_bps'],0)

    def test_wrong_resume_range_never_appended_or_published(self):
        target=models.MODELS_DIR/'large-v3'/'model.bin'; target.parent.mkdir()
        partial=target.with_name('model.bin.part');partial.write_bytes(b'abc')
        def request(*args):
            response=Response(b'bad'); response.status=206
            response.headers={'Content-Range':'bytes 0-2/6'}
            return response
        with patch.object(self.download,'_request',side_effect=request),patch.object(self.download.cancel_event,'wait'):
            with self.assertRaises(RuntimeError):self.download._download_file('large-v3','revision',{'name':'model.bin','size':6},0,6)
        self.assertEqual(partial.read_bytes(),b'abc')
        self.assertFalse(target.exists())


    def test_failure_never_marks_model_available(self):
        with patch.object(self.download,'_files',side_effect=OSError('offline')):
            self.download._run('fireredasr2-aed')
        self.assertEqual(self.download.get()['status'],'error')
        self.assertFalse(models.weights_present('fireredasr2-aed'))

    def test_cancel_keeps_download_fragments(self):
        partial=models.MODELS_DIR/'keep.part'; partial.write_bytes(b'abc')
        self.download.cancel()
        with patch.object(self.download,'_files',return_value=('rev',[{'name':'model.bin','size':3}])):
            self.download._run('large-v3')
        self.assertEqual(self.download.get()['status'],'cancelled')
        self.assertTrue(partial.exists())


class ModelLocationTests(unittest.TestCase):
    """模型目录名和目录里预设的名字不一致时，仍要能找到。"""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.folder=Path(self.temp.name)
        self.patches=[patch.object(models,'MODELS_DIR',self.folder),
                      patch.object(asr,'MODELS_DIR',self.folder)]
        for item in self.patches:item.start()

    def tearDown(self):
        for item in self.patches:item.stop()
        self.temp.cleanup()

    def whisper_dir(self,name):
        directory=self.folder/name; directory.mkdir()
        for filename in ('model.bin','config.json','tokenizer.json'):(directory/filename).write_bytes(b'x')
        return directory

    def test_renamed_whisper_folder_is_discovered(self):
        folder=self.whisper_dir('my-whisper-large-v3-ct2')
        self.assertEqual(models.model_directory('large-v3'),folder)
        self.assertEqual(asr.find_model('large-v3'),folder)

    def test_catalog_keeps_only_the_three_main_models(self):
        self.assertEqual(list(models.CATALOG),['large-v3','fireredasr2-aed','qwen3-asr-1.7b'])

    def test_manual_location_overrides_arbitrary_folder_name(self):
        folder=self.whisper_dir('随便起的名字')
        self.assertIsNone(asr.find_model('large-v3'))
        self.assertEqual(models.set_model_location('large-v3',str(folder)),folder)
        self.assertEqual(asr.find_model('large-v3'),folder)
        self.assertTrue((self.folder/'.locations.json').is_file())
        models.set_model_location('large-v3','')
        self.assertIsNone(asr.find_model('large-v3'))

    def test_manual_location_rejects_folder_without_weights(self):
        empty=self.folder/'empty'; empty.mkdir()
        with self.assertRaises(ValueError):models.set_model_location('large-v3',str(empty))
        with self.assertRaises(ValueError):models.set_model_location('large-v3',str(self.folder/'missing'))
        with self.assertRaises(ValueError):models.set_model_location('silero-vad',str(empty))


class NagisaPatchTests(unittest.TestCase):
    """中文项目路径下 DyNet 读不到模型，安装后要自动改写 nagisa 的模型路径。"""
    ORIGINAL = ("base = os.path.dirname(os.path.abspath(__file__))\n"
                "params = base + '/data/nagisa_v001.model'\n")

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def nagisa(self,engine_dir):
        folder=engine_dir/'qwen'/'Lib'/'site-packages'/'nagisa'
        (folder/'data').mkdir(parents=True)
        (folder/'data'/'nagisa_v001.model').write_bytes(b'x')
        (folder/'tagger.py').write_text(self.ORIGINAL,encoding='utf-8')
        return folder

    def patch_with(self,engine_dir):
        with patch.object(models,'ENGINE_DIR',engine_dir):
            models.patch_nagisa_model_path('qwen')

    def test_non_ascii_engine_path_rewritten_once(self):
        engine_dir=self.root/'中文目录'
        folder=self.nagisa(engine_dir)
        self.patch_with(engine_dir)
        self.patch_with(engine_dir)
        source=(folder/'tagger.py').read_text(encoding='utf-8')
        self.assertEqual(source.count(models.NAGISA_MARKER),1)
        self.assertIn('params = _model_path',source)
        self.assertNotIn("params = base + '/data/nagisa_v001.model'",source)

    def test_ascii_engine_path_untouched(self):
        folder=self.nagisa(self.root)
        self.patch_with(self.root)
        self.assertEqual((folder/'tagger.py').read_text(encoding='utf-8'),self.ORIGINAL)


class SourceTests(unittest.TestCase):
    def test_modelscope_file_revision_hash_and_empty_file_preserved(self):
        data={'Code':200,'Success':True,'Data':{'Files':[
            {'Type':'blob','Path':'model.bin','Size':3,'Sha256':hashlib.sha256(b'abc').hexdigest(),'Revision':'a'*40},
            {'Type':'blob','Path':'config.yaml','Size':0,'Sha256':hashlib.sha256(b'').hexdigest(),'Revision':'b'*40},
            {'Type':'blob','Path':'README.md','Size':5,'Revision':'a'*40}]}}
        def request(url,**kwargs):return Response(json.dumps(data).encode())
        manifest=sources.source_manifest('modelscope','fixture/model',request,lambda:None)
        self.assertEqual(len(manifest['files']),2)
        self.assertEqual(manifest['files'][1]['size'],0)
        self.assertIn('Revision='+'b'*40,manifest['files'][1]['urls'][0]['url'])
        self.assertEqual(manifest['files'][0]['sha256'],hashlib.sha256(b'abc').hexdigest())

    def manifest(self,source,digest='a'*64):
        return {'source':source,'label':source,'repo':'fixture/model','revision':'b'*40,'files':[
            {'name':'model.bin','size':3,'sha256':digest,'urls':[{'url':'https://'+source+'/fixture','label':source,'source':source}]}]}

    def test_faster_domestic_source_selected_only_same_hash_added_as_fallback(self):
        updates=[]
        def manifest(source,*args):return self.manifest(source)
        with patch.object(sources,'source_manifest',side_effect=manifest),patch.object(sources,'probe_source',side_effect=lambda m,*args:200 if m['source']=='modelscope' else 50):
            _,files=sources.resolve_sources('large-v3','fixture/model',None,lambda:None,lambda **kw:updates.append(kw))
        self.assertEqual(files[0]['urls'][0]['source'],'modelscope')
        self.assertEqual(len(files[0]['urls']),2)
        self.assertEqual(updates[-1]['probe_bps'],200)
        def different(source,*args):return self.manifest(source,'a'*64 if source=='modelscope' else 'b'*64)
        with patch.object(sources,'source_manifest',side_effect=different),patch.object(sources,'probe_source',return_value=100):
            _,files=sources.resolve_sources('large-v3','fixture/model',None,lambda:None,lambda **kw:None)
        self.assertEqual(len(files[0]['urls']),1)

    def test_official_only_requested_after_both_domestic_sources_fail(self):
        called=[]
        def manifest(source,*args):
            called.append(source)
            if source!='huggingface':raise OSError('unavailable')
            return self.manifest(source)
        with patch.object(sources,'source_manifest',side_effect=manifest),patch.object(sources,'probe_source',return_value=100):
            _,files=sources.resolve_sources('large-v3','fixture/model',None,lambda:None,lambda **kw:None)
        self.assertEqual(called[-1],'huggingface')
        self.assertEqual(files[0]['urls'][0]['source'],'huggingface')

    def test_modelscope_uses_the_registered_repository(self):
        repos={}
        def manifest(source,repo,*args):
            repos[source]=repo
            return self.manifest(source)
        with patch.object(sources,'source_manifest',side_effect=manifest),patch.object(sources,'probe_source',return_value=100):
            sources.resolve_sources('fireredasr2-aed','FireRedTeam/FireRedASR2-AED',None,lambda:None,lambda **kw:None)
        self.assertEqual(repos['modelscope'],'FireRedTeam/FireRedASR2-AED')
        self.assertEqual(repos['hf-mirror'],'FireRedTeam/FireRedASR2-AED')

class AlignmentTests(unittest.TestCase):
    def test_time_alignment_never_replaces_confirmed_text_or_original_times(self):
        cues=[{'id':1,'text':'你好世界。','start':0,'end':2,'original_text':'你号世界。'}]
        original=copy.deepcopy(cues)
        result=apply_word_times(cues,[0],[{'text':'你好','start':.2,'end':.6},{'text':'世界','start':.6,'end':1.2}],offset=5)
        self.assertEqual(result[0]['text'],original[0]['text'])
        self.assertEqual(result[0]['original_text'],'你号世界。')
        self.assertEqual((result[0]['original_start'],result[0]['original_end']),(0,2))
        self.assertEqual((result[0]['start'],result[0]['end']),(5.2,6.2))
        self.assertEqual([w['word'] for w in result[0]['words']],['你好','世界'])
        self.assertEqual(result[0]['words'][0]['start'],5.2)

    def test_word_internal_split_marked_estimated(self):
        cues=[{'text':'你','start':0,'end':1},{'text':'好','start':1,'end':2}]
        result=apply_word_times(cues,[0,1],[{'text':'你好','start':.2,'end':1.2}])
        self.assertTrue(all(c['estimated'] for c in result))
        self.assertEqual(result[0]['end'],result[1]['start'])

    def test_stale_alignment_error_is_cleared_when_alignment_succeeds(self):
        cues=[{'text':'你好世界','start':0,'end':2,'alignment_error':'对齐时长无效'}]
        result=apply_word_times(cues,[0],[{'text':'你好','start':.2,'end':.6},{'text':'世界','start':.6,'end':1.2}])
        self.assertNotIn('alignment_error',result[0])
        self.assertEqual((result[0]['start'],result[0]['end']),(.2,1.2))

    def test_aligner_mismatched_text_rejected(self):
        cues=[{'text':'我们明天去学校','start':0,'end':2}]
        before=copy.deepcopy(cues)
        apply_word_times(cues,[0],[{'text':'完全无关内容','start':.1,'end':1}])
        self.assertIn('不一致',cues[0]['alignment_error'])
        for field in ('text','start','end'):
            self.assertEqual(cues[0][field],before[0][field])

    def test_small_overlap_is_cut_at_midpoint(self):
        cues=[{'start':0,'end':2},{'start':1.5,'end':3}]
        result=_resolve_overlaps(copy.deepcopy(cues),[(0,2),(1.5,3)])
        self.assertAlmostEqual(result[0]['end'],1.75)
        self.assertAlmostEqual(result[1]['start'],1.75)
        self.assertLessEqual(result[1]['start'],result[0]['end'])

    def test_inverted_alignment_falls_back_to_input_times(self):
        cues=[{'start':239.28,'end':240.88},{'start':229.6,'end':232.96}]
        result=_resolve_overlaps(copy.deepcopy(cues),[(225.48,226.68),(231.6,232.94)])
        self.assertEqual((result[0]['start'],result[0]['end']),(225.48,226.68))
        self.assertEqual((result[1]['start'],result[1]['end']),(231.6,232.94))
        self.assertIn('alignment_error',result[0])
        self.assertIn('alignment_error',result[1])
        self.assertLess(result[0]['end'],result[1]['start'])

    def test_restored_pair_still_does_not_overlap_previous_cue(self):
        cues=[{'start':224.76,'end':225.58},{'start':239.28,'end':240.88},{'start':229.6,'end':232.96}]
        result=_resolve_overlaps(copy.deepcopy(cues),[(224.26,225.48),(225.48,226.68),(231.6,232.94)])
        for left,right in zip(result,result[1:]):
            self.assertGreaterEqual(right['start'],left['end']-1e-9)
        for cue in result:
            self.assertGreater(cue['end'],cue['start'])

    def test_bad_previous_boundary_does_not_collapse_restored_cues(self):
        # 对齐器把前一条的结束拉到 232.0，后两条顺序又颠倒；回退窗口必须覆盖更前一条。
        cues=[{'start':224.76,'end':232.0},{'start':239.28,'end':240.88},{'start':229.6,'end':232.96}]
        result=_resolve_overlaps(copy.deepcopy(cues),[(224.26,225.48),(225.48,226.68),(231.6,232.94)])
        for left,right in zip(result,result[1:]):
            self.assertGreaterEqual(right['start'],left['end']-1e-9)
        for cue in result:
            self.assertGreater(cue['end'],cue['start'])


if __name__=='__main__': unittest.main()
