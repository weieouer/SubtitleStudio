# 字幕工作室

本地字幕提取、剧本参考校准与音轨对齐工具。当前版本 4.0.2，旧版本提交、标签和安装包保留。主要面向中文素材，支持 SRT / MKV 字幕导出。

## 开发与运行

版本号使用 `主.次.修复`；大改动加主版本，常规功能加次版本，小修复加末位。每次发布保留 Git 提交与版本标签，详见 [版本与 Git 规则](docs/需求与规范.md#版本与-git-发布规则)。

源码开发使用 Windows x64 和 uv。`.python-version` 指定 Python 3.13，uv 会选用兼容的解释器，缺失时自动下载。

```powershell
winget install --id astral-sh.uv -e       # 首次安装 uv，安装后重开终端
uv sync --locked                        # 按锁文件同步项目环境
uv run --locked python main.py           # 日常启动，Ctrl+C 停止
```

uv 默认创建项目根目录下的 `.venv/`。需要自定义环境时，在运行 uv 前设置 `UV_PROJECT_ENVIRONMENT`。通过 `py main.py` 直接调用旧入口时，仍可用 `--venv <目录>` 或 `SUBTITLESTUDIO_DEV_VENV` 指定环境。

```powershell
uv run --locked python main.py --port 9000 --no-browser
uv run --locked python main.py --background
uv run --locked python main.py python    # 查看项目解释器
uv run --locked python -m unittest discover -s tests
```

`uv run --locked python main.py --help` 查看全部命令。VS Code 可选择 `.venv/Scripts/python.exe` 作为解释器。主界面默认使用 `http://127.0.0.1:8765/`，端口占用时顺延，仅监听本机。

源码、环境和缓存目录都不在文件树隐藏。`.gitignore` 只控制 Git 提交范围；`.ruff_cache/.gitignore` 是 Ruff 自动生成的缓存规则，不是第二个仓库。

## 目录结构

```text
SubtitleStudio/
├─ .venv/                 项目虚拟环境
├─ .vscode/               编辑器和调试配置
├─ backend/               Python 后端
├─ frontend/              HTML/CSS/JavaScript 前端
├─ config/                应用配置
├─ data/                  工程、模型、引擎、缓存和日志
├─ docs/                  文档与验证记录
├─ tests/                 测试
├─ tools/                 环境、维护和打包工具
├─ runtime/               随安装包发布的便携 Python，不参与日常源码运行
├─ wheels/                离线依赖（旧便携包或离线安装器使用）
├─ installer/             Inno Setup 安装器与构建脚本
├─ dist/                  构建产物
├─ main.py                唯一命令入口
├─ .python-version        开发解释器版本
├─ pyproject.toml         直接依赖与源码工具配置
└─ uv.lock                锁定直接和传递依赖
```

`.ruff_cache/`、`__pycache__/` 等由工具生成，正常显示即可；它们不是业务源码。

## 基本流程

1. 导入媒体，选择模型和设备，点击“提取字幕”；也可直接导入已有 SRT。
2. 需要参考时导入 TXT、MD 或 DOCX 剧本，推荐“开始校验 → 校准字幕 → 对齐字幕”。
3. 校验只标注内容差异；校准只复制已确认的参考文字（不拆分、不合并）；对齐只调整音轨时间与分段。
4. 在字幕列表或波形中编辑、试听，导出 SRT 或 MKV。

匹配到跨角色或隔着动作说明的多段对白时，展开匹配详情可查看各段原文。对齐会按已定位的剧本句界和词时间拆条，保留句间停顿。已有工程可再次点击“开始校验”更新参考。列表中的“时间待复核”和“时间异常”对应音轨，悬停可查看具体原因。首字或尾字的人声提示只显示在对应子条。

校验通过、校准通过、修改通过以及轻微问题、明显错误标签只作复核提示；文字确认不会遮住时间异常。默认预览与导出均使用编辑区时间；时间微调需显式预览或应用，应用可撤销，可能裁掉发声时停止。导出全部未标记“不导出”的条目，不受勾选、搜索或筛选影响。详细行为见 [需求与规范](docs/需求与规范.md)。

主模型三选一：FireRedASR2-AED、Whisper large-v3、Qwen3-ASR-1.7B。对齐使用 Qwen3-ForcedAligner-0.6B。模型在界面下载，GPU 环境也按需安装；无需先下载全部模型。GPU/真实对白效果仍需按素材验证。

音效影响句首尾时，可在“辅模”下载“人声分离（对齐辅助）”（约 320 MB）。下载后，对齐会自动分离疑难句的人声并重新核对逐字时间；只有多个词的时间稳定、首尾音效被明显抑制时才采用。普通句子不做分离，不确定的结果继续使用原有复核标签。辅模未下载或分离失败时沿用原声对齐；不会在对齐途中自动下载。权重保存在 `data/models/hdemucs-vocals`，升级安装时随其他模型保留。

工程保存在 `data/projects`，模型位于 `data/models`，引擎环境位于 `data/engines`。Ctrl+S 保存；空格播放/暂停，Ctrl+Z 撤销，Ctrl+Y 重做。素材移动后可能需要重新关联。

## 发布

`uv run --locked --exact python main.py build` 默认构建完整 EXE 安装包，输出 `dist/full/SubtitleStudio-Setup-<版本>-x64-full.exe`。构建期由 `tools/prepare_runtime.py` 准备便携 Python 和 Qwen/FireRed 共用推理环境，安装后直接启动，不在用户电脑运行 pip；模型仍由界面按需下载。不含模型权重、缓存和日志，不需要预装 Python、uv 或 VS Code。

- `build --output <目录>` 指定输出目录，已有安装包会保留。
- `build --stage-only` 只准备安装文件。
- 旧在线和离线版用 `uv run --locked python installer/build_installer.py` 构建，离线版加 `--offline`。首次启动使用联网下载或包内 wheels。
- 旧便携 ZIP 工具仍保留在 `tools/package_release.py`，不再是默认构建路径。

安装包按当前用户安装到 `%LOCALAPPDATA%\Programs\Subtitle Studio`，不需要管理员权限。安装后从桌面或开始菜单快捷方式启动；完整版直接使用内置运行库，GPU 与模型准备见包内《首次使用说明》。仍需 Windows x64 和 Chrome/Edge，GPU 驱动由目标电脑提供。构建细节见 [打包与首次启动](docs/打包与首次启动.md)。

## 排错与维护

- 开发启动报缺环境时运行 `uv sync --locked`。同步失败时查看 uv 的终端输出。
- 应用运行错误查看 `data/logs/app.log`，引擎依赖安装失败查看 `data/logs/pip.log`。
- 首次启动失败：查看安装目录 `data/logs` 下的 `bootstrap.log`、`pip.log`、`launcher.log`，或运行安装目录的 `run.bat --console`。
- 工程保存冲突：使用界面的“备份本页并载入最新工程”，不要直接覆盖另一页。

维护架构见 [技术总览](docs/技术总览.md)，未解决事项见 [问题与待办](docs/问题与待办.md)，重要变更及验证见 [开发记录](docs/开发记录.md)。[文档索引](docs/README.md) 说明各文档职责。
