; argus.iss - Inno Setup script for the ARGUS deployer.
; Build:  ISCC.exe installer\argus.iss   (run from deployer\, or use build_installer.ps1)
; Produces dist\installer\ARGUS-Setup-<version>.exe
;
; What the installed app gets:
;   - Program Files\ARGUS\ARGUS.exe  (the PyInstaller onefile build, with icon)
;   - Desktop shortcut (default ON) + Start Menu entry + uninstaller
;   - WebView2 evergreen runtime auto-installed IF missing (bundled bootstrapper)
;   - License page = Terms of Use (legal\terms_of_use.md, shown as plain text)

#define MyAppName "ARGUS"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "ARGUS Project"
#define MyAppExeName "ARGUS.exe"

[Setup]
; NEVER change AppId after first release - it is how upgrades find the install.
AppId={{182111db-ae1d-4e37-937c-8fe96352ccad}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\legal\terms_of_use.md
OutputDir=..\dist\installer
OutputBaseFilename=ARGUS-Setup-{#MyAppVersion}
SetupIconFile=..\assets\argus_B.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; no 'unchecked' flag -> checked by default (Runzhe's spec: desktop shortcut on)
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\ARGUS.exe"; DestDir: "{app}"; Flags: ignoreversion
; WebView2 evergreen bootstrapper (~2 MB) - runs only when the runtime is missing
Source: "redist\MicrosoftEdgeWebView2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: not WebView2Installed

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; install WebView2 runtime first if the machine lacks it (silent)
Filename: "{tmp}\MicrosoftEdgeWebView2Setup.exe"; Parameters: "/silent /install"; \
  StatusMsg: "Installing Microsoft Edge WebView2 runtime (required)..."; \
  Check: not WebView2Installed; Flags: waituntilterminated
; offer to launch at the end
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; the app's own working data (state files, WebView2 profile) lives under
; %LOCALAPPDATA%\ARGUS - remove it on uninstall so nothing is left behind.
Type: filesandordirs; Name: "{localappdata}\ARGUS"

[Code]
function WebView2Installed: Boolean;
var
  pv: string;
begin
  { evergreen runtime per-machine key (x64 systems use the WOW6432Node path) }
  Result := (RegQueryStringValue(HKLM,
      'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', pv) and (pv <> '') and (pv <> '0.0.0.0'));
  if not Result then
    Result := (RegQueryStringValue(HKCU,
        'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
        'pv', pv) and (pv <> '') and (pv <> '0.0.0.0'));
end;
