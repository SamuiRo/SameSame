; Inno Setup script for the SameSame Windows installer.
; Build with tools/build_windows.ps1, which passes /DAppVersion and /DSourceDir.
;
; Installs per user by default (no administrator prompt) into
; %LOCALAPPDATA%\Programs\SameSame. The uninstaller removes the program and
; offers to remove settings and cache; the quarantine folder is never touched,
; because it holds the user's own moved files.

#ifndef AppVersion
  #error AppVersion must be defined (/DAppVersion=x.y.z)
#endif
#ifndef SourceDir
  #error SourceDir must be defined (/DSourceDir=path\to\dist\SameSame)
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

#define AppName "SameSame"
#define AppExe "SameSame.exe"

[Setup]
; Never change AppId: upgrades and uninstall entries are keyed on it.
AppId={{6F1C2A3E-5B7D-4E8A-9C21-5A3E7D0B9F42}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=StarLith
AppPublisherURL=https://github.com/SamuiRo/SameSame
AppSupportURL=https://github.com/SamuiRo/SameSame/issues
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=SameSame-{#AppVersion}-win64-setup
SetupIconFile=..\..\dedupe\gui\assets\samesame.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
VersionInfoVersion={#AppVersion}
LicenseFile=..\..\LICENSE
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
ChangesEnvironment=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "addtopath"; Description: "Add samesame-cli to PATH (for the command line)"; GroupDescription: "Command line:"; Flags: unchecked

[InstallDelete]
; An upgrade must not leave the previous version's libraries next to the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The program writes nothing next to itself, so only remove the folder once it
; is empty: a user who installed into a shared folder must not lose other files.
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Code]
const
  UserEnvKey = 'Environment';
  SettingsKey = 'Software\SameSame';

function PathRoot(): Integer;
begin
  if IsAdminInstallMode() then
    Result := HKEY_LOCAL_MACHINE
  else
    Result := HKEY_CURRENT_USER;
end;

function PathKey(): String;
begin
  if IsAdminInstallMode() then
    Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
  else
    Result := UserEnvKey;
end;

function PathContains(const Paths, Dir: String): Boolean;
begin
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') > 0;
end;

procedure AddToPath(const Dir: String);
var
  Paths: String;
begin
  if not RegQueryStringValue(PathRoot(), PathKey(), 'Path', Paths) then
    Paths := '';
  if PathContains(Paths, Dir) then
    exit;
  if (Paths <> '') and (Copy(Paths, Length(Paths), 1) <> ';') then
    Paths := Paths + ';';
  RegWriteExpandStringValue(PathRoot(), PathKey(), 'Path', Paths + Dir);
end;

procedure RemoveFromPath(const Dir: String);
var
  Paths: String;
  Index: Integer;
begin
  if not RegQueryStringValue(PathRoot(), PathKey(), 'Path', Paths) then
    exit;
  Paths := ';' + Paths + ';';
  Index := Pos(';' + Uppercase(Dir) + ';', Uppercase(Paths));
  if Index = 0 then
    exit;
  Delete(Paths, Index, Length(Dir) + 1);
  Paths := Copy(Paths, 2, Length(Paths) - 2);
  RegWriteExpandStringValue(PathRoot(), PathKey(), 'Path', Paths);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddToPath(ExpandConstant('{app}'));
end;

procedure RemoveUserData();
var
  Kept: String;
begin
  DelTree(ExpandConstant('{localappdata}\SameSame'), True, True, True);
  DelTree(ExpandConstant('{userappdata}\SameSame'), True, True, True);
  RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, SettingsKey);
  // The quarantine holds the user's own moved originals: report it, never delete it.
  Kept := '';
  if DirExists(ExpandConstant('{userdocs}\SameSame Quarantine')) then
    Kept := Kept + ExpandConstant('{userdocs}\SameSame Quarantine') + #13#10;
  if DirExists(ExpandConstant('{%USERPROFILE}\SameSame Quarantine')) then
    Kept := Kept + ExpandConstant('{%USERPROFILE}\SameSame Quarantine') + #13#10;
  if Kept <> '' then
    MsgBox('Your quarantine folder was kept because it contains files SameSame moved there:' + #13#10 + #13#10 +
      Kept + #13#10 + 'Review it and delete it yourself when you no longer need those files.', mbInformation, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
  if CurUninstallStep = usPostUninstall then
  begin
    if UninstallSilent() then
      exit;
    if MsgBox('Also delete SameSame settings, scan cache, and the operations journal?' + #13#10 + #13#10 +
        'Choose No to keep them for a later reinstall.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      RemoveUserData();
  end;
end;
