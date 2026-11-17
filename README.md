# 字幕工作室

本地字幕提取、剧本参考校准与音轨对齐工具。当前版本 3.2.2，旧版本提交、标签和安装包保留。主要面向中文素材，支持 SRT / MKV 字幕导出。

## 开发与运行

版本号使用 `主.次.修复`；大改动加主版本，常规功能加次版本，小修复加末位。每次发布保留 Git 提交与版本标签，详见 [版本与 Git 规则](docs/需求与规范.md#版本与-git-发布规则)。

Windows x64，使用本机 Python 3.13。根目录只有一个入口 `main.py`：

```powershell
py main.py setup                         # 首次配置或更新项目依赖
py main.py                               # 日常启动，Ctrl+C 停止
py main.py build                         # 构建完整 EXE 安装包（依赖内置），默认输出 dist/full
```

默认虚拟环境：项目根目录的 `.venv/`，在文件树中正常显示。依赖只安装到项目环境；运行时自动使用该解释器，不重复安装。用 `--venv <目录>` 或 `SUBTITLESTUDIO_DEV_VENV` 指定其他环境。

```powershell
py main.py run --port 9000 --no-browser   # 指定端口，不打开浏览器
py main.py run --background              # 后台运行
$projectPython = py main.py python       # 查看项目解释器
& $projectPython -m unittest discover -s tests
```

`py main.py --help` 查看全部命令。VS Code 的解释器、F5 调试和终端自动激活统一使用 `.venv/`；配置变更后需要新建终端，已打开的旧终端不会回溯切换。主界面默认使用 `http://127.0.0.1:8765/`，端口占用时顺延，仅监听本机。

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
├─ pyproject.toml         源码工具配置
└─ requirements*.txt      依赖清单与锁文件
```

`.ruff_cache/`、`__pycache__/` 等由工具生成，正常显示即可；它们不是业务源码。

## 基本流程

1. 导入媒体，选择模型和设备，点击“提取字幕”；也可直接导入已有 SRT。
2. 需要参考时导入 TXT、MD 或 DOCX 剧本，推荐“开始校验 → 校准字幕 → 对齐字幕”。
3. 校验只标注内容差异；校准只复制已确认的参考文字（不拆分、不合并）；对齐只调整音轨时间与分段。
4. 在字幕列表或波形中编辑、试听，导出 SRT 或 MKV。

校验通过、校准通过、修改通过以及轻微问题、明显错误标签只作复核提示；文字确认不会遮住时间异常。默认预览与导出均使用编辑区时间；时间微调需显式预览或应用，应用可撤销，可能裁掉发声时停止。导出全部未标记“不导出”的条目，不受勾选、搜索或筛选影响。详细行为见 [需求与规范](docs/需求与规范.md)。

主模型三选一：FireRedASR2-AED、Whisper large-v3、Qwen3-ASR-1.7B。对齐使用 Qwen3-ForcedAligner-0.6B。模型在界面下载，GPU 环境也按需安装；无需先下载全部模型。GPU/真实对白效果仍需按素材验证。

工程保存在 `data/projects`，模型位于 `data/models`，引擎环境位于 `data/engines`。Ctrl+S 保存；空格播放/暂停，Ctrl+Z 撤销，Ctrl+Y 重做。素材移动后可能需要重新关联。

## 发布

`py main.py build` 默认构建完整 EXE 安装包，输出 `dist/full/SubtitleStudio-Setup-<版本>-x64-full.exe`。构建期由 `tools/prepare_runtime.py` 准备便携 Python 和 Qwen/FireRed 共用推理环境，安装后直接启动，不在用户电脑运行 pip；模型仍由界面按需下载。不含模型权重、缓存和日志，不需要预装 Python 或 VSCode。

- `py main.py build --output <目录>` 指定输出目录；不覆盖已有安装包。
- `py main.py build --stage-only` 只准备安装文件，不编译 EXE。
- 旧在线/离线版：`runtime\python\python.exe installer\build_installer.py` / `--offline`（可选，首次启动联网或使用 wheels 安装依赖）。
- 旧便携 ZIP 工具仍保留在 `tools/package_release.py`，不再是默认构建路径。

安装包按当前用户安装到 `%LOCALAPPDATA%\Programs\Subtitle Studio`，不需要管理员权限。安装后从桌面或开始菜单快捷方式启动；完整版直接使用内置运行库，GPU 与模型准备见包内《首次使用说明》。仍需 Windows x64 和 Chrome/Edge，GPU 驱动由目标电脑提供。构建细节见 [打包与首次启动](docs/打包与首次启动.md)。

## 排错与维护

- 开发启动报缺环境：运行 `py main.py setup`。
- 依赖安装失败：查看 `data/logs/pip.log`；运行错误查看 `data/logs/app.log`。
- 首次启动失败：查看安装目录 `data/logs` 下的 `bootstrap.log`、`pip.log`、`launcher.log`，或运行安装目录的 `run.bat --console`。
- 工程保存冲突：使用界面的“备份本页并载入最新工程”，不要直接覆盖另一页。

维护架构见 [技术总览](docs/技术总览.md)，未解决事项见 [问题与待办](docs/问题与待办.md)，重要变更及验证见 [开发记录](docs/开发记录.md)。[文档索引](docs/README.md) 说明各文档职责。
