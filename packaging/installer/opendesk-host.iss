; Inno Setup script for OpenDesk Host (Windows)
; Produces a per-user wizard installer with:
;   - Start Menu + Desktop shortcuts
;   - Auto-start at login (minimized in system tray)
;   - Pre-configured relay (gibisoft.net:8474)

#define MyAppName "OpenDesk Host"
; Version can be overridden from the build script: ISCC /DMyAppVersion=x.y.z
#ifndef MyAppVersion
#define MyAppVersion "1.6.0"
#endif
#define MyAppPublisher "OpenDesk"
#define MyAppExeName "opendesk-host.exe"

[Setup]
AppId={{6E0D3F5A-1B2C-4D3E-9F8A-7C6B5A4D3E2F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\OpenDeskHost
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=OpenDeskHostSetup-{#MyAppVersion}
SetupIconFile=..\opendesk-host.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\opendesk-host\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--minimized"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--minimized"; Tasks: desktopicon

[Registry]
; Auto-start at login (minimized in system tray)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"" --minimized"; Flags: uninsdeletevalue
; Pre-configured relay (QSettings org=OpenDesk app=OpenDesk -> HKCU\Software\OpenDesk\OpenDesk\network)
Root: HKCU; Subkey: "Software\OpenDesk\OpenDesk\network"; ValueType: string; ValueName: "relay_host"; ValueData: "gibisoft.net"
Root: HKCU; Subkey: "Software\OpenDesk\OpenDesk\network"; ValueType: dword; ValueName: "relay_port"; ValueData: "8474"
Root: HKCU; Subkey: "Software\OpenDesk\OpenDesk\network"; ValueType: string; ValueName: "enable_relay"; ValueData: "true"

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--minimized"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
