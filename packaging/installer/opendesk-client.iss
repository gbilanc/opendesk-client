; Inno Setup script for OpenDesk client (Windows)
; Produces a per-user wizard installer with:
;   - Start Menu + optional Desktop shortcut
;   - Pre-configured relay (gibisoft.net:8474)
; No auto-start (the client is launched by the user).

#define MyAppName "OpenDesk"
; Version can be overridden from the build script: ISCC /DMyAppVersion=x.y.z
#ifndef MyAppVersion
#define MyAppVersion "2.1.0"
#endif
#define MyAppPublisher "OpenDesk"
#define MyAppExeName "opendesk.exe"

[Setup]
AppId={{2B7C1E64-9A3D-4F8B-8C21-D5E4F6A7B901}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\OpenDesk
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=OpenDeskSetup-{#MyAppVersion}
SetupIconFile=..\opendesk.ico
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
Source: "..\..\dist\opendesk\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Pre-configured relay (QSettings org=OpenDesk app=OpenDesk -> HKCU\Software\OpenDesk\OpenDesk\network)
Root: HKCU; Subkey: "Software\OpenDesk\OpenDesk\network"; ValueType: string; ValueName: "relay_host"; ValueData: "gibisoft.net"
Root: HKCU; Subkey: "Software\OpenDesk\OpenDesk\network"; ValueType: dword; ValueName: "relay_port"; ValueData: "8474"
Root: HKCU; Subkey: "Software\OpenDesk\OpenDesk\network"; ValueType: string; ValueName: "enable_relay"; ValueData: "true"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
