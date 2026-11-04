; CTI Camera Follow - installer (Inno Setup 6)
; Build: ISCC.exe /DAppVer=1.0.0 /DSrcDir=..\dist\CTI Camera Follow installer\setup.iss

#ifndef AppVer
  #define AppVer "0.0.0"
#endif
#ifndef SrcDir
  #define SrcDir "..\dist\CTI Camera Follow"
#endif
#define AppName "CTI Camera Follow"
#define AppExe "CTI Camera Follow.exe"

[Setup]
AppId={{99D3DE31-C04C-4ABF-976B-C0519600056B}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName} {#AppVer}
AppPublisher=DESA / CTI
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=..\dist
OutputBaseFilename=CTI-CameraFollow-Setup-{#AppVer}
SetupIconFile=..\icons\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=force
RestartApplications=no
WizardStyle=modern
SetupLogging=yes
VersionInfoVersion={#AppVer}

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Scurtatura pe Desktop"; GroupDescription: "Scurtaturi:"
Name: "autostart"; Description: "Porneste automat la logarea in Windows (langa ceas)"; GroupDescription: "Pornire:"
Name: "dep_obs"; Description: "Instaleaza OBS Studio (lipseste)"; GroupDescription: "Componente necesare (se descarca de pe internet):"; Check: NeedOBS
Name: "dep_vbcable"; Description: "Instaleaza VB-Cable (audio spre Zoom/Teams, cere repornire)"; GroupDescription: "Componente necesare (se descarca de pe internet):"; Check: NeedVBCable
Name: "dep_audiomon"; Description: "Instaleaza plugin-ul OBS Audio Monitor"; GroupDescription: "Componente necesare (se descarca de pe internet):"; Check: NeedAudioMon

[Dirs]
Name: "{commonappdata}\{#AppName}"; Permissions: users-modify

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "tools\*.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\Dezinstaleaza {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{commonstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--tray"; WorkingDir: "{app}"; Tasks: autostart

[Run]
; firewall: interfata din retea + preview WebRTC
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Flags: runhidden; StatusMsg: "Configurez firewall-ul..."
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\{#AppExe}"" enable=yes"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\go2rtc\go2rtc.exe"" enable=yes"; Flags: runhidden
; componente
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\install_deps.ps1"" -What obs"; Flags: runhidden waituntilterminated; StatusMsg: "Instalez OBS Studio (descarcare)..."; Tasks: dep_obs
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\install_plugins.ps1"""; Flags: runhidden waituntilterminated; StatusMsg: "Instalez plugin-ul Audio Monitor..."; Tasks: dep_audiomon
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\install_deps.ps1"" -What vbcable"; Flags: waituntilterminated; StatusMsg: "Instalez VB-Cable (confirma fereastra Windows)..."; Tasks: dep_vbcable
; migrare de la versiunea cu scripturi .bat (ruleaza ca utilizatorul logat)
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\migrate.ps1"""; Flags: runhidden waituntilterminated runasoriginaluser; StatusMsg: "Import configuratia veche..."
; pornire
Filename: "{app}\{#AppExe}"; Description: "Porneste {#AppName}"; Flags: nowait postinstall runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM ""{#AppExe}"""; Flags: runhidden; RunOnceId: "KillApp"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM go2rtc.exe"; Flags: runhidden; RunOnceId: "KillGo2rtc"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Flags: runhidden; RunOnceId: "DelFw"

[Code]
function NeedOBS: Boolean;
begin
  Result := not FileExists(ExpandConstant('{commonpf64}\obs-studio\bin\64bit\obs64.exe'));
end;

function NeedVBCable: Boolean;
begin
  Result := not FileExists(ExpandConstant('{sys}\drivers\vbaudio_cable64_win10.sys'))
        and not FileExists(ExpandConstant('{sys}\drivers\vbaudio_cable64_win7.sys'));
end;

function NeedAudioMon: Boolean;
begin
  Result := not DirExists(ExpandConstant('{commonappdata}\obs-studio\plugins\audio-monitor'));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  { opresc aplicatia (inclusiv versiunea veche cu Python) inainte de copiere }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM "{#AppExe}"', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec('powershell.exe',
    '-NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -match ''^pythonw?\.exe$'' -and $_.CommandLine -like ''*app.py*'') -or $_.Name -eq ''go2rtc.exe'' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
    '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(1000);
  Result := '';
end;
