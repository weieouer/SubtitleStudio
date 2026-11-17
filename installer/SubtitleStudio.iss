; ============================================================
;  Subtitle Studio installer (Inno Setup 6)
;  Build:  python installer\build_installer.py
; ============================================================

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "build\stage"
#endif
#ifndef SourceRoot
  #define SourceRoot ".."
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif
#ifndef Edition
  #define Edition "online"
#endif

#define AppName "Subtitle Studio"
#define AppPublisher "Subtitle Studio"
#define AppExe "run.bat"

[Setup]
AppId={{FFE1553D-606B-4F3E-8460-641574C7776D}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\Subtitle Studio
; Show the destination page on upgrades too; "auto" hides it for an existing install.
DisableDirPage=no
AlwaysShowDirOnReadyPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=SubtitleStudio-Setup-{#AppVersion}-x64-{#Edition}
SetupIconFile={#SourceRoot}\frontend\assets\studio-film.ico
UninstallDisplayIcon={app}\frontend\assets\studio-film.ico
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes
UsePreviousAppDir=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
chinesesimplified.SelectDirBrowseLabel=可输入路径或点击“浏览”。工程与模型也保存在此目录，请选择可写磁盘。
english.SelectDirBrowseLabel=Enter a path or click Browse. Projects and models are also stored here; choose a writable folder.

[CustomMessages]
chinesesimplified.InstallDirNotWritable=无法写入所选安装目录：%1%n%n字幕工作室需要在此保存工程、模型和日志。请选择当前用户有写入权限的目录。
english.InstallDirNotWritable=Cannot write to the selected installation folder: %1%n%nSubtitle Studio stores projects, models and logs here. Choose a folder writable by the current user.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; 用户配置只在首次安装写入，覆盖升级保留已有设置与模型路径。
Source: "{#StageDir}\config\*"; DestDir: "{app}\config"; Flags: onlyifdoesntexist recursesubdirs createallsubdirs uninsneveruninstall
Source: "{#StageDir}\*"; DestDir: "{app}"; Excludes: "config\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
#if Edition == "full"
Name: "{group}\{#AppName}"; Filename: "{app}\runtime\python\pythonw.exe"; Parameters: "-I ""{app}\backend\launch.py"""; WorkingDir: "{app}"; IconFilename: "{app}\frontend\assets\studio-film.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\runtime\python\pythonw.exe"; Parameters: "-I ""{app}\backend\launch.py"""; WorkingDir: "{app}"; IconFilename: "{app}\frontend\assets\studio-film.ico"; Tasks: desktopicon
#else
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\frontend\assets\studio-film.ico"
#endif
Name: "{group}\首次使用说明"; Filename: "{app}\首次使用说明.txt"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{app}\卸载 {#AppName}"; Filename: "{uninstallexe}"; Comment: "卸载 {#AppName} 并删除安装目录中的工程、模型、引擎、日志和缓存"
#if Edition != "full"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\frontend\assets\studio-film.ico"; Tasks: desktopicon
#endif

[Run]
#if Edition == "full"
Filename: "{app}\runtime\python\pythonw.exe"; Parameters: "-I ""{app}\backend\launch.py"""; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
#else
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
#endif

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\data"
Type: filesandordirs; Name: "{app}\config"
Type: filesandordirs; Name: "{app}\backend\__pycache__"
Type: dirifempty; Name: "{app}"

[Code]
function CheckInstallDirectory: String;
var
  Directory, ProbeDirectory, Probe: String;
  CreatedDirectory, CreatedProbeDirectory: Boolean;
begin
  Result := '';
  Directory := WizardDirValue;
  CreatedDirectory := not DirExists(Directory);
  CreatedProbeDirectory := False;
  Probe := '';
  try
    if not ForceDirectories(Directory) then
      Result := FmtMessage(CustomMessage('InstallDirNotWritable'), [Directory])
    else begin
      ProbeDirectory := AddBackslash(Directory) + '.subtitle-studio-write-' +
        GetMD5OfUnicodeString(ExpandConstant('{tmp}'));
      CreatedProbeDirectory := CreateDir(ProbeDirectory);
      if not CreatedProbeDirectory then
        Result := FmtMessage(CustomMessage('InstallDirNotWritable'), [Directory])
      else begin
        Probe := AddBackslash(ProbeDirectory) + 'write.tmp';
        if not SaveStringToFile(Probe, 'Subtitle Studio write check', False) then
          Result := FmtMessage(CustomMessage('InstallDirNotWritable'), [Directory]);
      end;
    end;
  except
    Result := FmtMessage(CustomMessage('InstallDirNotWritable'), [Directory]);
  end;
  if (Probe <> '') and FileExists(Probe) then begin
    if not DeleteFile(Probe) then
      Result := FmtMessage(CustomMessage('InstallDirNotWritable'), [Directory]);
  end;
  if CreatedProbeDirectory then begin
    if not RemoveDir(ProbeDirectory) then
      Result := FmtMessage(CustomMessage('InstallDirNotWritable'), [Directory]);
  end;
  { Remove only the empty final directory created by this check, never existing data. }
  if CreatedDirectory then
    RemoveDir(Directory);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Error: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then begin
    Error := CheckInstallDirectory;
    if Error <> '' then begin
      Log(Error);
      if not WizardSilent then
        MsgBox(Error, mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { Recheck immediately before extraction, including silent /DIR installs. }
  Result := CheckInstallDirectory;
end;
