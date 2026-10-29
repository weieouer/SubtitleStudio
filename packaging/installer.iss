; Inno Setup 6 脚本：把 dist\SubtitleStudio 打成一个可覆盖升级的安装包
;
; 用法：
;   1. 先跑 packaging\build.bat 生成 dist\SubtitleStudio
;   2. 装 Inno Setup 6（免费）：https://jrsoftware.org/isdl.php
;   3. 右键本文件 → Compile，产物在 packaging\installer_out\
;
; 升级逻辑（重点）：
;   - AppId 不变 + 同一安装目录 = 原地升级，不需要先卸载
;   - 安装前删掉整个 _internal（程序文件），避免旧版本残留的库混进来
;   - data\models 用 onlyifdoesntexist：如果内置了模型，升级时不重复拷
;   - data\uploads / data\outputs 完全不动：用户的素材和字幕不会丢

#define AppName "SubtitleStudio"
#define AppVersion "0.1.0"
#define AppExe "SubtitleStudio.exe"
#define AppPublisher "SubtitleStudio"

[Setup]
; AppId 一旦发布就不要再改，否则升级会变成"装了两个"
AppId={{8E4B1C2A-7D3F-4A6B-9C1E-5F2D8A0B7C31}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 装到用户目录，不需要管理员权限：
; 程序把 data\（模型/上传/导出）写在 exe 旁边，装到 Program Files 下普通用户写不进去
PrivilegesRequired=lowest
OutputDir=installer_out
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
; 有图标就取消下一行注释（图标放 packaging\app.ico）
; SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExe}

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[InstallDelete]
; 升级前清掉旧程序文件，防止上一版的 _internal 残留
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; 程序本体（不含 data）
Source: "..\dist\SubtitleStudio\*"; DestDir: "{app}"; Excludes: "data\*"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; 可选内置模型：默认没有模型，该条目自动跳过；有则已有就不覆盖，升级不重复拷
Source: "..\dist\SubtitleStudio\data\models\*"; DestDir: "{app}\data\models"; \
    Flags: ignoreversion recursesubdirs createallsubdirs onlyifdoesntexist uninsneveruninstall skipifsourcedoesntexist

[Dirs]
Name: "{app}\data\uploads"; Permissions: users-modify
Name: "{app}\data\outputs"; Permissions: users-modify
Name: "{app}\data\models"; Permissions: users-modify

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时只清缓存，data\ 留着：模型、用户上传和导出都不删
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
