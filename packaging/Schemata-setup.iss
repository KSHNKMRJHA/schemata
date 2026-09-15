; Schemata — Component Intelligence Platform installer (Inno Setup 6)
; Build: ISCC.exe Schemata-setup.iss

#define AppName       "Schemata"
#define AppVersion    "2.0.0"
#define AppPublisher  "Kishan J."
#define AppExeName    "Schemata.exe"
; Paths are relative to this script's directory (packaging\) so the build is
; portable. SourcePath resolves to the folder containing this .iss file.
#define SourceDir     SourcePath + "..\dist\Schemata.dist"
#define OutputDir     SourcePath + "..\dist\installer"
#define IconPath      SourcePath + "icon.ico"
#define VcRedist      SourcePath + "vc_redist.x64.exe"

[Setup]
AppId={{8F2C1A3E-5D04-4E7B-9C6A-3B1F0C2E9D14}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/KSHNKMRJHA/schemata
AppSupportURL=https://github.com/KSHNKMRJHA/schemata
AppUpdatesURL=https://github.com/KSHNKMRJHA/schemata
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=Setup_{#AppName}_v{#AppVersion}
SetupIconFile={#IconPath}
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
VersionInfoVersion=2.0.0.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=Schemata - Component Intelligence Platform
VersionInfoProductName={#AppName}
VersionInfoProductVersion=2.0.0
VersionInfoTextVersion=2.0.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#VcRedist}"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon; IconFilename: "{app}\{#AppExeName}"
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: checkedonce

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing Visual C++ runtime..."; Flags: waituntilterminated
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\*"