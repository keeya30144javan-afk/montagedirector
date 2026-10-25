; Windows installer for Montage Director (Inno Setup 6).
; Built by .github/workflows/release.yml:  iscc /DAppVersion=1.2.3 installer\MontageDirector.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Montage Director"
#define AppExe "MontageDirector.exe"

[Setup]
AppId={{6C1E2F4A-9B3D-4E7A-8F21-5D0C3B7A9E14}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Montage Director
AppPublisherURL=https://github.com/keeya30144javan-afk/montagedirector
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=MontageDirector-Setup-{#AppVersion}
SetupIconFile=..\montage_director\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; Remove the previous version's files so updates never mix old and new libraries.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\MontageDirector\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent
