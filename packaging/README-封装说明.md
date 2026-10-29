# 封装成 exe 说明

已经整套准备好并**实测构建通过**：`dist\SubtitleStudio\SubtitleStudio.exe` 能启动、界面静态资源正常、`data\` 正确落在 exe 旁边。
等你代码写完，跑一次 `packaging\build.bat` 就能出新的。

## 一句话原理

PyInstaller 把 `.venv` 里的 Python、依赖、`app\static` 前端资源打成一个**文件夹**（onedir），
旁边留 `data\` 放模型和用户文件。代码里三行路径判断让打包后仍能找到 `data\`。
Inno Setup 再把这个文件夹做成安装包。

## 为什么是 onedir 不是 onefile

| | onedir（已选） | onefile |
|---|---|---|
| 冷启动 | 秒开 | 每次把 300MB+ 解压到临时目录，十几秒 |
| 杀毒误报 | 少 | 多（每次自解压行为可疑） |
| 体积 | 296MB（不含模型） | 差不多，但多一份临时解压开销 |

带 ctranslate2 / onnxruntime / PyAV 三个大块头，onefile 体验会很差。

## 三个必须处理的坑（已处理并验证）

1. **`data\` 会被建到 `_internal` 里**
   原来 `BASE_DIR = Path(__file__).parent.parent`，打包后 `__file__` 在 `_internal\` 里，
   模型和导出全进 `_internal\data`，升级一覆盖就丢。
   → `make_frozen_safe.py` 改成：打包后取 exe 目录，源码运行行为不变。
   实测结果：启动后 `data\{models,uploads,outputs}` 出现在 exe 旁边 ✅

2. **前端静态资源必须随包**
   `StaticFiles(directory=STATIC_DIR)`，目录丢了界面 404。
   → spec 的 `datas` 打包 `app/static`，解到 `_internal/app/static`，正好对上 `__file__`。实测 `style.css`/`app.js`/`favicon.svg` 全部 200 ✅

3. **uvicorn 是动态导入**
   只 `import uvicorn` 会缺 `uvicorn.protocols.http.h11_impl` 之类，启动即 `ModuleNotFoundError`。
   → spec 的 `hiddenimports` 里显式列出。

## 模型：默认都不内置

代码里 `HF_REPOS` 定义了 small / medium / large-v3。体积：

| 模型 | 大小 | 是否内置 |
|---|---|---|
| small | 约 500MB | ❌ 首次使用时在界面下载 |
| medium | 约 1.5GB | ❌ 用户按需下载 |
| large-v3 | 约 3GB | ❌ 用户按需下载 |

`build.bat` 里的 `BUNDLED_MODEL` 默认留空，发布目录不含任何模型，安装包最小。
想内置某个本机已经下载完整的模型：把它改成 `small`（或对应名字）再打包；
也可以打包后手动拷：

```bat
.venv\Scripts\python.exe packaging\bundle_models.py --dist dist\SubtitleStudio --only small
```

想一次内置本机所有已下载模型：去掉 `--only`。本机没下载过的模型会自动跳过，不会报错。

模型放在 `data\models` 而不是打进 exe，是为了内置时**升级不重复拷**（见下）。

## 打包步骤

```bat
packaging\build.bat
```

七步：装 pyinstaller → 打路径补丁 → 清旧产物 → PyInstaller 构建 → 建 data 目录 → 可选内置模型 → 拷使用说明。

手动等价命令：

```bat
.venv\Scripts\python.exe packaging\make_frozen_safe.py
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean ^
    --distpath dist --workpath build packaging\app.spec
.venv\Scripts\python.exe packaging\bundle_models.py --dist dist\SubtitleStudio --only small
```

## 哪些进安装包，哪些不进

| 内容 | 进包 | 说明 |
|---|---|---|
| `app\`、`run.py` | ✅ | 程序本体；`app\static` 由 spec 显式带上 |
| Python 与依赖（`_internal\`） | ✅ | PyInstaller 自动收集 |
| `data\models\small`、`medium`、`large-v3` | ❌（默认） | `BUNDLED_MODEL` 留空；用户在界面里按需下载 |
| `data\models\*`（内置时） | 可选 | 把 `BUNDLED_MODEL` 设为对应模型名，从本机拷入 |
| `data\uploads`、`data\outputs`、`data\results` | ❌ | 用户数据；安装时只创建空目录 |
| `samples\`、`tests\` | ❌ | 示例和测试，运行不需要 |
| `packaging\` | ❌ | 打包工具本身，不随安装包发布 |
| `.venv\` | ❌ | 只在打包时使用 |
| `README.md`、`requirements.txt`、`启动.bat` | ❌ | 源码运行用，安装包不带 |
| `packaging\README-usage.txt` | ✅ | `build.bat` 拷到发布目录，随安装包给用户 |

## 安装包与升级覆盖

`packaging\installer.iss`（Inno Setup 6，免费）已经按"可覆盖升级"配好：

- **AppId 固定**、安装到 `%LOCALAPPDATA%\Programs\SubtitleStudio` → **直接装新版就完成升级**，不用先卸载
- 安装前 `[InstallDelete]` 清掉整个 `_internal` → 旧版本残留的库不会混进新版
- `data\models` 用 `onlyifdoesntexist` + `skipifsourcedoesntexist` → 没内置模型时自动跳过，内置了升级也不重复拷
- `data\uploads`、`data\outputs` → 完全不碰，用户素材和字幕不会丢；安装包里也不含这些文件
  （`[Files]` 用 `Excludes: "data\*"` 排除了整个 `data\`，只单独拷 `data\models`）
- 装到用户目录（`PrivilegesRequired=lowest`）→ **普通员工账号就能装，不需要 IT 给管理员权限**

发新版本的流程：

1. 改 `packaging\version_info.txt` 里的 `filevers` / `FileVersion`
2. 改 `packaging\installer.iss` 的 `#define AppVersion`
3. 跑 `build.bat`，再 Compile `installer.iss`
4. 把新 `SubtitleStudio-Setup-x.y.z.exe` 发出去，覆盖安装即可

安装包体积预估：默认不内置模型，程序约 296MB，lzma2 压缩后 **约 120–150MB**；
如果内置 small，程序 + 模型约 762MB，压缩后 **约 550–650MB**。

## 给同事/公司环境用之前要知道的

| 项目 | 结论 |
|---|---|
| 管理员权限 | 不需要，装用户目录 ✅ |
| 首次启动要联网 | 默认要下载 small（约 500MB，已指 ModelScope / hf-mirror.com）；下载一次后离线可用。只有内置模型时才能开箱离线 |
| 杀毒/EDR 拦截 | **最大风险点**。PyInstaller 未签名 exe 容易被拦，建议内部报白名单，或买代码签名证书 |
| Windows 版本 | Win10/11 x64 ✅（`ArchitecturesAllowed=x64compatible`）|
| 缺 VCRUNTIME140.dll | 装 VC++ 2015-2022 x64 运行库，README-usage 里写了 |
| 多人同时用同一台机器 | 端口自动顺延 8765→8766，互不干扰 ✅ |
| 别人乱改文件 | 界面只读程序目录，写的是 `data\`，不会破坏安装 ✅ |

## 以后改了代码要注意

- **加了新依赖** → 判断它有没有数据文件/二进制，有的话加到 spec 的 `_COLLECT`
  （现在有 faster_whisper、ctranslate2、onnxruntime、av、tokenizers、huggingface_hub、jieba、pypinyin、srt）
- **加了新的静态目录/模板目录** → 加到 spec 的 `datas`
- **版本号** 三处：`version_info.txt`、`installer.iss` 的 `#define AppVersion`、`app/main.py` 里 FastAPI 的 version
- **新增 `app/` 下的 .py 模块** 不用管，自动收进去

## 常见报错对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: uvicorn.protocols...` | hiddenimports 漏了 | 加到 spec |
| 启动即退，`DLL load failed` | onnxruntime / ctranslate2 的 DLL 没进去或被压坏 | spec 已设 `upx=False`；确认 `_COLLECT` 含这两个包 |
| 界面 404、样式全丢 | `app/static` 没打进去 | 查 `_internal\app\static` 是否存在 |
| 每次启动都下载模型 | `data\` 指到了临时目录 | 跑 `make_frozen_safe.py --check` |
| 双击闪退看不到原因 | 控制台一闪而过 | 地址栏输 `cmd` 回车，再敲 `SubtitleStudio.exe` |

## 目录说明

```
packaging\
  app.spec             PyInstaller 配置（核心）
  build.bat            一键打包，默认不内置模型（BUNDLED_MODEL 可设）
  make_frozen_safe.py  路径适配补丁（幂等，--check 只看状态）
  bundle_models.py     把本地已下载的模型拷进发布目录（--list 查看）
  installer.iss        Inno Setup 安装包脚本（支持覆盖升级）
  version_info.txt     exe 属性里的版本信息
  README-usage.txt     随发布目录给用户看的使用说明
```

`build\`、`dist\`、`*.py.bak` 已在 `.gitignore` 里。
