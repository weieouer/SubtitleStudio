"""构建可搬移的完整运行环境；只在打包机安装依赖，安装版不调用 pip。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / 'installer/build/prepared-runtime'
# 构建配方变化时必须让旧运行库失效；修改本脚本或下面的配置文件时同步递增。
RECIPE_VERSION = 6
LAYOUT = ('python/python.exe', 'python/pythonw.exe', 'ml/python.exe',
          'ml/Lib/site-packages/torch/__init__.py',
          'ml/Lib/site-packages/qwen_asr/__init__.py',
          'ml/Lib/site-packages/fireredasr2s/fireredasr2/__init__.py')

# 独立 Python 和推理 Python 都必须带齐 CRT，不能让系统安装掩盖漏包。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from backend.core.windows_runtime import VC_DLLS
from tools.prepare_vc_runtime import copy_crt
LAYOUT += tuple(f'{environment}/{name}' for environment in ('python', 'ml') for name in VC_DLLS)


def clean_environment():
    result = {key: value for key, value in os.environ.items()
              if not key.startswith('PIP_') and key not in ('VIRTUAL_ENV', 'PYTHONPATH', 'PYTHONHOME')}
    result.update(PYTHONNOUSERSITE='1', PYTHONUTF8='1', PYTHONUNBUFFERED='1', PIP_CONFIG_FILE=os.devnull,
                  PIP_CACHE_DIR=str(ROOT / 'data/cache/pip'))
    return result


def _python_identity():
    """把便携 Python 的版本和架构计入配方，换解释器后不能复用旧站包。"""
    executable = ROOT / 'runtime/python/python.exe'
    if not executable.is_file():
        return 'missing'
    try:
        result = subprocess.run(
            [str(executable), '-I', '-c', 'import sys,platform;print(sys.version.split()[0],platform.machine())'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return f'unknown-{executable.stat().st_size}'


def runtime_key(cuda=False):
    """配方指纹覆盖脚本、锁文件、引擎配置、SDK 固定版本与解释器身份。"""
    digest = hashlib.sha256()
    digest.update(f'recipe={RECIPE_VERSION}\ntorch={"cuda" if cuda else "cpu"}\n'.encode())
    digest.update(Path(__file__).read_bytes())
    digest.update((Path(__file__).with_name('prepare_vc_runtime.py')).read_bytes())
    digest.update((Path(__file__).resolve().parents[1] / 'backend/core/windows_runtime.py').read_bytes())
    for name in ('requirements-lock.txt', 'config/engines.json', 'config/firered.json'):
        path = ROOT / name
        if path.is_file():
            digest.update(name.encode() + b'\n' + path.read_bytes() + b'\n')
    digest.update(_python_identity().encode())
    for environment in ('.venv','data/engines/qwen'):
        for metadata in sorted((ROOT/environment/'Lib/site-packages').glob('*.dist-info/METADATA')):
            digest.update(metadata.relative_to(ROOT).as_posix().encode()+b'\0'+metadata.read_bytes()+b'\0')
    return digest.hexdigest()


def prepared_runtime(base=None):
    """复用前统一校验清单、配方指纹和关键文件，缺失时返回可读原因。"""
    base = Path(base or DESTINATION)
    try:
        info = json.loads((base / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'ready': False, 'reason': '缺少运行库清单', 'manifest': {}}
    if not isinstance(info,dict) or info.get('format') not in (1,2):
        return {'ready':False,'reason':'运行库清单格式不受支持','manifest':{}}
    if info.get('ready') is not True:
        return {'ready': False, 'reason': '运行库上次准备未完成', 'manifest': info}
    missing = [name for name in LAYOUT if not (base / name).is_file()]
    if missing:
        return {'ready': False, 'reason': '运行库缺少关键文件：' + '、'.join(missing), 'manifest': info}
    cuda = info.get('torch_flavor') == 'cuda'
    if info.get('torch_flavor') not in ('cpu', 'cuda'):
        return {'ready': False, 'reason': '运行库未记录 CPU/CUDA 类型', 'manifest': info}
    if info.get('dependency_sha256') != runtime_key(cuda):
        return {'ready': False, 'reason': '运行库与当前构建配方不一致，需要重新准备', 'manifest': info}
    return {'ready': True, 'reason': '', 'manifest': info}


def run(python, *arguments):
    subprocess.run([str(python), *arguments], cwd=ROOT, env=clean_environment(), check=True)


def clone_python(target):
    base = ROOT / 'runtime/python'
    if not (base / 'python.exe').is_file():
        raise RuntimeError('缺少打包用便携 Python')
    if target.exists():
        resolved = target.resolve()
        if target.is_symlink() or not resolved.is_relative_to(DESTINATION.resolve()) or resolved == DESTINATION.resolve():
            raise ValueError('只能重建本次打包专用的运行环境目录')
        shutil.rmtree(resolved)
    shutil.copytree(base, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    copy_crt(target)
    return target / 'python.exe'


def packages_from(source, target):
    if not source.is_dir():
        raise RuntimeError(f'缺少已验证的构建依赖目录：{source}')
    shutil.copytree(source, target / 'Lib/site-packages', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.log'))


def download(url, destination, attempts=3):
    """构建机网络可能中断；保留已完成文件，失败时明确提示重试。"""
    partial = destination.with_name(destination.name + '.part')
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'SubtitleStudio-build'})
            with urllib.request.urlopen(request, timeout=60) as response, partial.open('wb') as output:
                shutil.copyfileobj(response, output)
            if partial.stat().st_size < 100:
                raise RuntimeError('下载内容异常')
            partial.replace(destination)
            return
        except (OSError, urllib.error.URLError, RuntimeError) as error:
            partial.unlink(missing_ok=True)
            if attempt == attempts - 1:
                raise RuntimeError(f'无法下载 {url}；请检查网络或代理后重新执行：{error}') from error
            time.sleep(1.5 * (attempt + 1))


def cached_wheel(name, version):
    """固定 FireRed 额外依赖的版本，构建机断网时也能从已缓存的 wheel 安装。"""
    stem = re.sub(r'[-_.]+', '_', name).lower()
    for path in (ROOT / 'data/cache/runtime-build/wheels').glob('*.whl'):
        parts = path.name[:-4].split('-')
        if len(parts) >= 2 and re.sub(r'[-_.]+', '_', parts[0]).lower() == stem and parts[1] == version:
            return str(path)
    raise RuntimeError(f'缺少已缓存的依赖 wheel：{name}=={version}；请先放入 data/cache/runtime-build/wheels')


def check_dependencies(python):
    """运行 pip check；gradio 只提供网页界面、不参与推理，允许它与固定 Pillow 的版本声明冲突。"""
    result = subprocess.run([str(python), '-m', 'pip', 'check'], cwd=ROOT, env=clean_environment(),
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    lines = [line.strip() for line in (result.stdout or '').splitlines() if line.strip()]
    blocking = [line for line in lines if not ('gradio' in line and 'pillow' in line.lower())]
    if result.returncode and blocking:
        raise RuntimeError('推理运行库依赖不完整：' + '；'.join(blocking))


def install(python, packages, cuda=False):
    if not cuda:
        packages = local_cached_wheels(packages)
    index = 'https://download.pytorch.org/whl/cu126' if cuda else 'https://pypi.org/simple'
    run(python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
        '--timeout', '120', '--retries', '3', '--index-url', index, *packages)


def local_cached_wheels(packages):
    """复用已校验环境对应的 pip 缓存，避免在慢网络下重复取大型 CPU wheel。"""
    from packaging.tags import sys_tags
    supported_tags = {str(tag) for tag in sys_tags()}
    wanted = {name.lower().replace('_','-'): version for package in packages if '==' in package for name,version in [package.split('==',1)]}
    requested = set(wanted)
    if not requested:
        return packages
    cache = ROOT / 'data/cache/runtime-build/wheels'
    cache.mkdir(parents=True, exist_ok=True)
    found = {}
    for base in (ROOT / 'data/cache/pip/http-v2', Path(os.environ.get('LOCALAPPDATA', '')) / 'pip/Cache/http-v2'):
        if not base.is_dir():
            continue
        for body in base.rglob('*.body'):
            if len(found) == len(requested):
                break
            try:
                if body.stat().st_size < 1000:
                    continue
                with zipfile.ZipFile(body) as archive:
                    metadata = next((name for name in archive.namelist() if name.endswith('.dist-info/METADATA')), '')
                    if not metadata:
                        continue
                    text = archive.read(metadata).decode('utf-8')
                    name = re.search(r'^Name: (.+)$', text, re.MULTILINE)[1].strip().lower().replace('_','-')
                    version = re.search(r'^Version: (.+)$', text, re.MULTILINE)[1].strip()
                    if name not in requested or name in found or version.split('+')[0] != wanted[name] or 'cu' in version:
                        continue
                    wheel_info = archive.read(metadata.removesuffix('METADATA')+'WHEEL').decode('utf-8')
                    tag = next((tag for tag in re.findall(r'^Tag: (.+)$', wheel_info, re.MULTILINE) if tag.strip() in supported_tags), '')
                    if not tag:
                        continue
                    destination = cache / (name.replace('-','_')+f'-{version}-{tag.strip()}.whl')
                    shutil.copy2(body, destination)
                    found[name] = str(destination)
            except (OSError, zipfile.BadZipFile, KeyError, TypeError):
                continue
    return [found.get(package.split('==')[0].lower().replace('_','-'), package) for package in packages]


def firered_revision():
    """优先使用版本控制的固定提交；旧构建机缓存和远端 main 仅作兼容回退。"""
    pinned = ROOT / 'config/firered.json'
    if pinned.is_file():
        revision = str(json.loads(pinned.read_text(encoding='utf-8')).get('revision', ''))
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise RuntimeError('config/firered.json 的 revision 必须是完整的 40 位提交号')
        return revision
    cache = ROOT / 'data/cache/runtime-build'
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / 'firered-revision.json'
    if cached.exists():
        return json.loads(cached.read_text(encoding='utf-8'))['revision']
    metadata = cache / 'firered-revision.download.json'
    download('https://api.github.com/repos/FireRedTeam/FireRedASR2S/commits/main', metadata)
    revision = json.loads(metadata.read_text(encoding='utf-8'))['sha']
    metadata.unlink(missing_ok=True)
    cached.write_text(json.dumps({'revision': revision}), encoding='utf-8')
    return revision


def firered_source(target):
    """官方仓库没有安装元数据，固定提交后显式打包 SDK，不能 pip install 仓库 ZIP。"""
    cache = ROOT / 'data/cache/runtime-build'
    cache.mkdir(parents=True, exist_ok=True)
    revision = firered_revision()
    archive = cache / f'firered-{revision}.zip'
    if not archive.exists():
        download(f'https://codeload.github.com/FireRedTeam/FireRedASR2S/zip/{revision}', archive)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('SDK 包路径非法')
            parts = path.parts[1:]
            if member.is_dir() or not parts:
                continue
            if parts[0].upper().startswith('LICENSE'):
                license_file = target.parent / 'vendor/licenses/FireRedASR2S-LICENSE.txt'
                license_file.parent.mkdir(parents=True, exist_ok=True)
                license_file.write_bytes(bundle.read(member))
                continue
            if parts[0] != 'fireredasr2s':
                continue
            destination = target / 'Lib/site-packages' / Path(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(bundle.read(member))
    return {'revision': revision, 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}


def freeze(python):
    """记录实际装入的包，便于排查“锁文件相同但站包不同”的机器差异。"""
    result = subprocess.run([str(python), '-I', '-m', 'pip', 'freeze'], cwd=ROOT, env=clean_environment(),
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    return [line.strip() for line in (result.stdout or '').splitlines() if line.strip()] if result.returncode == 0 else []


def defer_japanese_tokenizer(python):
    """中文对齐不初始化日语 DyNet 模型，避免其原生加载器不支持中文安装路径。"""
    path=python.parent/'Lib/site-packages/qwen_asr/inference/qwen3_forced_aligner.py'
    source=path.read_text(encoding='utf-8')
    eager='\nimport nagisa\n'
    method='    def tokenize_japanese(self, text: str) -> List[str]:\n'
    if eager not in source:
        if method+'        import nagisa\n' not in source:
            raise RuntimeError('Qwen SDK 入口发生变化，无法验证日语分词器的延迟加载')
        return
    if method not in source:
        raise RuntimeError('Qwen SDK 分词方法发生变化，不能应用路径兼容修复')
    source=source.replace(eager,'\n',1).replace(method,method+'        import nagisa\n',1)
    path.write_text(source,encoding='utf-8',newline='\n')


def prepare(cuda=False):
    DESTINATION.mkdir(parents=True, exist_ok=True)
    marker = DESTINATION / 'manifest.json'
    marker.unlink(missing_ok=True)
    print('准备主程序的完整便携运行环境…', flush=True)
    core = clone_python(DESTINATION / 'python')
    packages_from(ROOT / '.venv/Lib/site-packages', core.parent)
    run(core, '-I', '-c', 'import av,numpy,faster_whisper,jieba,rapidfuzz,pypinyin,onnxruntime')
    print('准备 Qwen、FireRed 的共享推理环境…', flush=True)
    ml = clone_python(DESTINATION / 'ml')
    packages_from(ROOT / 'data/engines/qwen/Lib/site-packages', ml.parent)
    torch = ['torch==2.9.1', 'torchvision==0.24.1', 'torchaudio==2.9.1']
    if cuda:
        torch = [name.replace('2.9.1', '2.9.1+cu126').replace('0.24.1', '0.24.1+cu126') for name in torch]
    install(ml, torch, cuda=cuda)
    # Qwen 与 FireRed 共用的推理依赖；版本与 requirements-lock.txt 保持一致。
    install(ml, ['numpy==2.2.6', 'av==19.0.2', 'onnxruntime==1.31.1',
                 'opencv-python-headless==4.12.0.88', 'Pillow==12.0.0',
                 'scipy==1.16.3', 'imageio==2.37.0', 'einops==0.8.1',
                 'tqdm==4.67.1', 'timm==1.0.22', 'PyYAML==6.0.3',
                 'requests==2.32.5', 'addict==2.4.0', 'future==1.0.0',
                 'matplotlib==3.10.7', 'scikit-image==0.25.2', 'yapf==0.43.0',
                 'cn2an==0.5.23', 'kaldiio==2.18.1',
                 cached_wheel('kaldi-native-fbank', '1.23.0'),
                 cached_wheel('sentencepiece', '0.2.3'),
                 cached_wheel('textgrid', '1.5'),
                 cached_wheel('peft', '0.21.2'),
                 cached_wheel('flatbuffers', '25.12.19'),
                 cached_wheel('protobuf', '7.36.2'),
                 cached_wheel('contourpy', '1.4.0'),
                 cached_wheel('cycler', '0.12.1'),
                 cached_wheel('fonttools', '4.68.0'),
                 cached_wheel('kiwisolver', '1.5.1'),
                 cached_wheel('pyparsing', '3.3.3'),
                 cached_wheel('tifffile', '2026.11.8'),
                 cached_wheel('proces', '0.1.7')])
    firered = firered_source(ml.parent)
    defer_japanese_tokenizer(ml)
    print('验证全部引擎入口和依赖，不下载任何权重…', flush=True)
    run(ml, '-I', '-c', 'import torch,torchvision,torchaudio,av,cv2,onnxruntime,timm,scipy; '
        'from qwen_asr import Qwen3ASRModel,Qwen3ForcedAligner; '
        'from fireredasr2s.fireredasr2 import FireRedAsr2,FireRedAsr2Config; '
        'print(torch.__version__,torch.version.cuda)')
    check_dependencies(ml)
    info = {'format': 2, 'ready': True, 'core': 'python/python.exe', 'ml': 'ml/python.exe',
            'engines': ['qwen', 'firered'], 'torch_flavor': 'cuda' if cuda else 'cpu',
            'cuda': '12.6' if cuda else None, 'firered': firered,
            'recipe_version': RECIPE_VERSION, 'python': _python_identity(),
            'packages': {'core': freeze(core), 'ml': freeze(ml)},
            'dependency_sha256': runtime_key(cuda)}
    marker.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
    print('完整离线运行环境已就绪：' + str(DESTINATION), flush=True)
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cuda', action='store_true', help='可选：把 CUDA 一并打包；默认不打包 CUDA')
    args = parser.parse_args()
    prepare(cuda=args.cuda)


if __name__ == '__main__':
    main()
