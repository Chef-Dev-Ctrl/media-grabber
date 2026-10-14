; Inno Setup script for Media Grabber. Built by build.ps1 (pass /DAppVersion=x.y.z).
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6F0B7C2E-3D4A-4E8B-9C1F-5A2D8E7B4C11}
AppName=Media Grabber
AppVersion={#AppVersion}
AppPublisher=Pulse Labs
DefaultDirName={localappdata}\Programs\Media Grabber
DefaultGroupName=Media Grabber
DisableProgramGroupPage=yes
; per-user install: no admin prompt
PrivilegesRequired=lowest
OutputDir=installer
OutputBaseFilename=MediaGrabber-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\MediaGrabber.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\MediaGrabber\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Media Grabber"; Filename: "{app}\MediaGrabber.exe"
Name: "{autodesktop}\Media Grabber"; Filename: "{app}\MediaGrabber.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MediaGrabber.exe"; Description: "Launch Media Grabber"; Flags: nowait postinstall skipifsilent

; Downloads, history and logins (%APPDATA%\MediaGrabber) are kept on uninstall on purpose.
