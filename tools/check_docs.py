"""检查当前维护文档的本地链接和过期命令；历史档案不参与当前规范检查。"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ('README.md', 'docs/README.md', 'docs/技术总览.md', 'docs/打包与首次启动.md',
             'docs/需求与规范.md', 'docs/问题与待办.md', 'docs/开发记录.md',
             'docs/设计检查与改进建议.md', 'docs/3.1.0需求与验收.md')


def check() -> dict:
    errors = []
    links = 0
    for name in DOCUMENTS:
        document = ROOT / name
        text = document.read_text(encoding='utf-8')
        if re.search(r'\b(?:py|python)\s+(?:-3\.13\s+)?(?:dev|build)\.py\b', text):
            errors.append(f'{name}: 仍引用已移除的根目录命令')
        prose = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
        for match in re.finditer(r'!?\[[^\]\n]*\]\(([^)\n]+)\)', prose):
            target = match.group(1).strip().strip('<>')
            if re.match(r'^[a-zA-Z][\w+.-]*:', target) or target.startswith('#'):
                continue
            target = re.sub(r':\d+$', '', unquote(target.split('#', 1)[0]))
            links += 1
            if target and not (document.parent / target).exists():
                errors.append(f'{name}: 本地链接不存在：{target}')
    return {'documents': len(DOCUMENTS), 'local_links': links, 'errors': errors, 'all_passed': not errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = check()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
