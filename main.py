"""字幕工作室唯一入口：运行、配置项目环境、构建完整安装包。"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from io import TextIOWrapper
from pathlib import Path

from tools.development import (
    environment_path,
    process_environment,
    python_path,
    setup_environment,
    validate_environment,
)

ROOT = Path(__file__).resolve().parent

#wyj
def run_application(arguments: list[str]) -> int:
    # 后端保留现有 core 模块组织；从任何工作目录启动都使用本项目的源码。
    sys.path.insert(0, str(ROOT / 'backend'))
    background = '--background' in arguments
    arguments = [argument for argument in arguments if argument not in ('--background', '--console')]
    sys.argv = [str(ROOT / 'main.py'), *arguments]
    if background:
        import launch
        return launch.main() or 0
    import app
    app.main()
    return 0


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('端口必须是整数') from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError('端口必须在 0 到 65535 之间')
    return port


def command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='main.py', description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--venv', help='项目虚拟环境目录，默认项目内 .venv')
    run = commands.add_parser('run', parents=[common], help='运行程序（不写子命令时默认执行）')
    run.add_argument('--port', type=port_number, default=8765, help='起始端口，默认 8765')
    run.add_argument('--no-browser', action='store_true', help='不打开浏览器')
    run.add_argument('--background', action='store_true', help='在后台运行')
    run.add_argument('--console', action='store_true', help=argparse.SUPPRESS)
    commands.add_parser('setup', parents=[common], help='创建项目虚拟环境并安装/检查依赖')
    build = commands.add_parser('build', parents=[common], help='构建完整 EXE 安装包，依赖内置，仅模型按需下载')
    build.add_argument('--output', type=Path, default=ROOT / 'dist/full', help='EXE 输出目录，默认 dist/full')
    build.add_argument('--iscc', type=Path, help='Inno Setup 编译器路径')
    build.add_argument('--stage-only', action='store_true', help='只准备安装文件，交给已有打包工具')
    commands.add_parser('python', parents=[common], help='输出项目解释器路径')
    return parser


def main(arguments=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding='utf-8')
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if not arguments or arguments[0].startswith('-') and arguments[0] not in ('-h', '--help'):
        arguments.insert(0, 'run')
    args = command_parser().parse_args(arguments)
    environment = environment_path(args.venv)
    try:
        if args.command == 'python':
            print(python_path(environment))
            return 0
        if args.command == 'setup':
            setup_environment(environment)
            return 0
        if args.command == 'build':
            from tools.release import build_installer
            result = build_installer(args.output, environment, iscc=args.iscc, stage_only=args.stage_only)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        python = validate_environment(environment)
        if Path(sys.prefix).resolve() != environment or sys.prefix == sys.base_prefix:
            return subprocess.call([str(python), str(ROOT / 'main.py'), *arguments],
                                   cwd=ROOT, env=process_environment(environment))
        application_arguments = ['--port', str(args.port)]
        if args.no_browser:
            application_arguments.append('--no-browser')
        if args.background:
            application_arguments.append('--background')
        return run_application(application_arguments)
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f'操作失败：{error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
