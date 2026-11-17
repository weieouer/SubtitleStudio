"""生成带离线依赖 wheels 的 Windows 便携包；只读取本机项目，不搬动模型和引擎。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP = 'SubtitleStudio'
TREES = ('backend', 'frontend', 'docs', 'tests', 'tools', 'runtime', 'wheels')
FILES = ('main.py', 'pyproject.toml', '.gitignore', 'README.md', 'requirements.txt',
         'requirements-lock.txt', 'config/engines.json', 'config/ui.json')
WORK = ('data/projects', 'data/results', 'data/uploads', 'data/outputs')
REQUIRED = ('run.bat', 'backend/app.py', 'backend/launch.py', 'frontend/index.html',
            'backend/core/progress_console.py',
            'tools/bootstrap.py', 'tools/pip_progress.py',
            'requirements.txt', 'config/engines.json', 'runtime/python/python.exe',
            'runtime/python/pythonw.exe', 'runtime/python/Lib/venv/__init__.py',
            'runtime/python/Lib/ensurepip/__init__.py')
PRUNE = {'__pycache__', '.venv', '.git', 'site-packages'}
FORBIDDEN = ('data/models/', 'data/engines/', 'data/cache/', 'data/run/', 'data/logs/')


def app_version() -> str:
    match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)',
                      (ROOT / 'backend' / 'app.py').read_text(encoding='utf-8'), re.MULTILINE)
    if not match:
        raise SystemExit('无法从 backend/app.py 读取 VERSION')
    return match.group(1)


def selected_files(root: Path, include_work: bool):
    selected = {}
    for tree in TREES + (WORK if include_work else ()):
        base = root / tree
        if not base.is_dir():
            continue
        for current, dirs, files in os.walk(base, followlinks=False):
            current = Path(current)
            dirs[:] = sorted(d for d in dirs if d not in PRUNE and not (current / d).is_symlink())
            for name in sorted(files):
                path = current / name
                if path.is_symlink() or path.suffix in ('.pyc', '.pyo', '.lock', '.tmp', '.part'):
                    continue
                selected[path.relative_to(root).as_posix()] = path
    for name in FILES:
        path = root / name
        if path.is_file():
            selected[name] = path
    # 开发目录没有 BAT 入口；发布时把模板放到 ZIP 根目录。
    launcher = root / 'tools/portable/run.bat'
    if launcher.is_file():
        selected['run.bat'] = launcher
    guide = root / 'tools/portable/首次使用说明.txt'
    if guide.is_file():
        selected['首次使用说明.txt'] = guide
    missing = [name for name in REQUIRED if name not in selected]
    if missing:
        raise ValueError('缺少必需文件：' + ', '.join(missing))
    return dict(sorted(selected.items()))


def verify_archive(path: Path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('压缩包有重复路径')
        for name in names:
            parts = name.split('/')
            if parts[0] != TOP or '..' in parts or '\\' in name or any(p in PRUNE for p in parts):
                raise ValueError('压缩包路径不允许：' + name)
            relative = '/'.join(parts[1:])
            if any(relative.startswith(p) for p in FORBIDDEN):
                raise ValueError('压缩包含应排除的运行数据：' + name)
        for name in REQUIRED + ('PACKAGE_MANIFEST.json',):
            if f'{TOP}/{name}' not in names:
                raise ValueError('压缩包缺少：' + name)
        manifest = json.loads(archive.read(f'{TOP}/PACKAGE_MANIFEST.json'))
        expected = {f'{TOP}/{name}' for name in manifest['files']} | {f'{TOP}/PACKAGE_MANIFEST.json'}
        if set(names) != expected:
            raise ValueError('压缩包内容与清单不一致')
        for name, info in manifest['files'].items():
            with archive.open(f'{TOP}/{name}') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != info['sha256'] or archive.getinfo(f'{TOP}/{name}').file_size != info['size']:
                raise ValueError('文件校验失败：' + name)
        corrupt = archive.testzip()
        if corrupt:
            raise ValueError('ZIP CRC 校验失败：' + corrupt)
    return {'archive': str(path), 'bytes': path.stat().st_size, 'files': len(names),
            'include_work': manifest['include_work'], 'all_passed': True}


def package(output: Path, include_work=False, allow_online=False):
    output = output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError('压缩包请输出到项目目录之外，避免把发布包再次收入项目')
    if output.exists():
        raise FileExistsError('目标文件已存在，保留原包，请换一个名称：' + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    selected = selected_files(ROOT, include_work)
    wheels = {name: path for name, path in selected.items()
              if name.startswith('wheels/') and name.endswith('.whl')}
    if not wheels and not allow_online:
        raise ValueError('缺少离线依赖 wheels/；请先运行 tools/prepare_wheels.py，'
                         '或加 --allow-online 生成联网安装包')
    if wheels:
        restore = ('解压整个目录后双击 run.bat；首次启动优先用包内 wheels 离线安装依赖，'
                   '缺失时才联网；在模型菜单下载所需模型。')
    else:
        restore = '解压整个目录后双击 run.bat；首次启动需要联网安装依赖，在模型菜单下载所需模型。'
    manifest = {'format': 1, 'application_version': app_version(), 'platform': 'Windows x64',
                'include_work': include_work, 'entrypoint': 'run.bat',
                'offline_install': bool(wheels),
                'offline_wheels': {'count': len(wheels),
                                   'bytes': sum(path.stat().st_size for path in wheels.values())},
                'excluded': list(FORBIDDEN) + ['.venv/', 'config/dialog_dirs.json', '**/__pycache__/'],
                'restore': restore,
                'work_note': '工程保留原路径；带工程时，换设备仍需重新关联旧绝对路径和项目外素材。',
                'files': {}}
    fd, temporary = tempfile.mkstemp(prefix=output.stem + '_', suffix='.partial', dir=output.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, path in selected.items():
                before = path.stat()
                with path.open('rb') as source, archive.open(f'{TOP}/{name}', 'w', force_zip64=True) as target:
                    digest = hashlib.sha256()
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        target.write(chunk)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise RuntimeError('打包时文件发生变化，请稍后重试：' + name)
                manifest['files'][name] = {'size': after.st_size, 'sha256': digest.hexdigest()}
            archive.writestr(f'{TOP}/PACKAGE_MANIFEST.json',
                             json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
        verify_archive(temporary)
        # 不覆盖任何现有压缩包。Windows rename 在目标存在时会报错。
        temporary.rename(output)
    finally:
        temporary.unlink(missing_ok=True)
    return verify_archive(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output', type=Path, help='新 ZIP 路径（必须在项目目录之外）')
    group.add_argument('--verify', type=Path, help='只校验已有 ZIP')
    parser.add_argument('--include-work', action='store_true', help='额外带上工程、结果、项目内素材和导出；仍排除模型依赖')
    parser.add_argument('--allow-online', action='store_true', help='没有 wheels 时仍生成需要联网安装的包')
    args = parser.parse_args()
    result = (verify_archive(args.verify) if args.verify
              else package(args.output, args.include_work, args.allow_online))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
