"""固定国内模型源探测、短采样测速与同内容备用链接。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import PurePosixPath
import re
import time
from urllib.parse import quote, urlencode

SOURCES = {
    'modelscope': ('魔搭 ModelScope', 'https://modelscope.cn'),
    'hf-mirror': ('HF 国内镜像', 'https://hf-mirror.com'),
    'huggingface': ('Hugging Face 官方备用', 'https://huggingface.co'),
}
PROBE_BYTES = 256 * 1024


def selected_file(filename):
    path = PurePosixPath(filename)
    if not filename or path.is_absolute() or '..' in path.parts or '\\' in filename or ':' in filename:
        raise ValueError('模型仓库文件名无效')
    return not (filename.startswith(('.', 'assets/', 'examples/', 'example/', 'onnx/'))
                or filename.lower().endswith(('.md','.png','.jpg','.wav','.mp3','.onnx')))


def source_manifest(source, repo, request, check):
    label, endpoint = SOURCES[source]
    if source == 'modelscope':
        url = f'{endpoint}/api/v1/models/{repo}/repo/files?Revision=master&Recursive=true'
    else:
        url = f'{endpoint}/api/models/{repo}?blobs=true'
    check()
    with request(url, timeout=6) as response:
        payload = response.read(5 * 1024 * 1024 + 1)
    if len(payload) > 5 * 1024 * 1024:
        raise ValueError('模型清单过大')
    info = json.loads(payload)
    if source == 'modelscope' and (info.get('Code') != 200 or not info.get('Success', True)):
        raise ValueError('魔搭模型清单不可用')
    entries = info.get('Data', {}).get('Files', []) if source == 'modelscope' else info.get('siblings', [])
    revision = 'master' if source == 'modelscope' else info.get('sha')
    files = []
    for entry in entries:
        if source == 'modelscope' and entry.get('Type') != 'blob':
            continue
        filename = entry.get('Path') if source == 'modelscope' else entry.get('rfilename')
        if not isinstance(filename, str) or not selected_file(filename):
            continue
        size = entry.get('Size') if source == 'modelscope' else entry.get('size', entry.get('lfs', {}).get('size', 0))
        digest = entry.get('Sha256') if source == 'modelscope' else entry.get('lfs', {}).get('sha256')
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError('模型文件大小无效')
        if digest and not re.fullmatch(r'[a-fA-F0-9]{64}', digest):
            raise ValueError('模型文件哈希无效')
        pinned = entry.get('Revision') if source == 'modelscope' else revision
        if not isinstance(pinned, str) or not re.fullmatch(r'[a-fA-F0-9]{40,64}', pinned):
            raise ValueError('模型文件版本无效')
        if source == 'modelscope':
            file_url = f'{endpoint}/api/v1/models/{repo}/repo?' + urlencode({'Revision':pinned,'FilePath':filename})
        else:
            file_url = f"{endpoint}/{repo}/resolve/{pinned}/{quote(filename, safe='/')}"
        files.append({'name':filename,'size':size,'sha256':digest.lower() if digest else None,
                      'urls':[{'url':file_url,'source':source,'label':label}], 'source_revision':pinned})
    if not files or len({item['name'] for item in files}) != len(files):
        raise ValueError('模型清单为空或包含重复文件')
    return {'source':source,'label':label,'repo':repo,'revision':revision,'files':files}


def probe_source(manifest, request, check):
    """只采样一小段真实权重，包含连接耗时，避免仅凭主页响应选源。"""
    item = max(manifest['files'], key=lambda f:f['size'])
    count = min(PROBE_BYTES, item['size'])
    if count <= 0:
        raise ValueError('模型源没有有效权重')
    check()
    started = time.monotonic()
    with request(item['urls'][0]['url'], {'Range':f'bytes=0-{count-1}'}, timeout=6) as response:
        data = response.read(count)
        if len(data) != count:
            raise ValueError('模型源采样不完整')
    check()
    return len(data) / max(.001,time.monotonic()-started)


def resolve_sources(name, repo, request, check, update):
    manifests, errors = [], []
    update(message='正在检测国内模型源…', source='', speed_bps=0)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='model-source') as pool:
        futures = {pool.submit(source_manifest, source, r, request, check):source
                   for source,r in [('modelscope',repo),('hf-mirror',repo)]}
        for future in as_completed(futures):
            check()
            try:
                manifests.append(future.result())
            except Exception as error:
                errors.append(str(error))
    update(source_errors=errors)
    if not manifests:
        check()
        update(message='国内源暂不可用，尝试官方备用…')
        try:
            manifests.append(source_manifest('huggingface',repo,request,check))
        except Exception as error:
            raise RuntimeError('国内模型源和官方备用均不可用，请检查网络后重试') from error
    update(message='正在小流量测速，选择较快模型源…')
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='model-probe') as pool:
        futures = {pool.submit(probe_source,m,request,check):m for m in manifests}
        for future in as_completed(futures):
            check()
            manifest = futures[future]
            try:
                manifest['probe_bps'] = future.result()
            except Exception:
                manifest['probe_bps'] = 0
    # 小采样失败可能是服务器不支持 Range，保留其为低优先级备用。
    manifests.sort(key=lambda m:m['probe_bps'],reverse=True)
    primary = manifests[0]
    for item in primary['files']:
        for other in manifests[1:]:
            same = next((f for f in other['files'] if f['name'] == item['name']),None)
            # 只把大小和 SHA-256 完全一致的文件串为备用，不混用不同版本。
            if same and item['sha256'] and same['sha256'] == item['sha256'] and same['size'] == item['size']:
                item['urls'].extend(same['urls'])
    update(source=primary['label'], message='已选择 ' + primary['label'],
           probe_bps=primary['probe_bps'], source_probes=[{'source':m['label'],'bytes_per_second':round(m['probe_bps'])} for m in manifests])
    return primary['revision'], primary['files']
