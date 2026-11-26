; Instalador Windows (Inno Setup 6): ISCC /DAppVersion=1.0.0 packaging\installer.iss
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6C1B0E4A-7D52-4E1B-9B8E-4D3A2F1C9A11}
AppName=Mega Sinc
AppVersion={#AppVersion}
AppPublisher=Mega Sinc
DefaultDirName={autopf}\Mega Sinc
DefaultGroupName=Mega Sinc
OutputDir=..\dist
OutputBaseFilename=MegaSinc-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\MegaSinc.exe
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
ChangesAssociations=yes

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\MegaSinc\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Mega Sinc"; Filename: "{app}\MegaSinc.exe"
Name: "{autodesktop}\Mega Sinc"; Filename: "{app}\MegaSinc.exe"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\Classes\.megasinc"; ValueType: string; ValueName: ""; ValueData: "MegaSinc.Project"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\MegaSinc.Project"; ValueType: string; ValueName: ""; ValueData: "Projeto Mega Sinc"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\MegaSinc.Project\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\MegaSinc.exe"" ""%1"""

[Run]
Filename: "{app}\MegaSinc.exe"; Description: "{cm:LaunchProgram,Mega Sinc}"; Flags: nowait postinstall skipifsilent
