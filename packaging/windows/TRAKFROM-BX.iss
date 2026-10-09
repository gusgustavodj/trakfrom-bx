; Authorial native installer. Compile only against a verified, positive staging.
#ifndef StageDir
  #error StageDir must identify a verified staging directory
#endif
#ifndef OutputPath
  #error OutputPath is required
#endif
#ifndef PayloadInclude
  #error PayloadInclude is required
#endif
#define ProductId "{574E3A6C-90F3-489F-A34F-C1D1A3E8609F}"
#define ProductVersion "0.1.0.dev0"
#define BuildNumber 2026100901

[Setup]
AppId={{574E3A6C-90F3-489F-A34F-C1D1A3E8609F}
AppName=TRAKFROM-BX
AppVersion={#ProductVersion}
AppPublisher=TRAKFROM-BX contributors
VersionInfoVersion=0.1.0.0
DefaultDirName={commonpf64}\TRAKFROM-BX
DefaultGroupName=TRAKFROM-BX
DisableProgramGroupPage=yes
DisableDirPage=no
DisableWelcomePage=no
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
LicenseFile={#StageDir}\LICENSE.txt
InfoBeforeFile=components.txt
OutputDir={#OutputPath}
OutputBaseFilename=TRAKFROM-BX-Setup
Compression=lzma2
SolidCompression=yes
TimeStampsInUTC=yes
SetupMutex=Global\TRAKFROM_BX_SETUP_V1
AppMutex=Global\TRAKFROM_BX_ACTIVITY_V1
CloseApplications=no
RestartApplications=no
UsePreviousAppDir=yes
UsePreviousTasks=yes
Uninstallable=yes
UninstallDisplayName=TRAKFROM-BX
UninstallDisplayIcon={app}\runtime\pythonw.exe
ChangesAssociations=no
ChangesEnvironment=no
AllowRootDirectory=no
AllowUNCPath=no
AllowNetworkDrive=no

[Tasks]
Name: desktop; Description: "Create a Desktop shortcut"; Flags: unchecked
Name: startmenu; Description: "Create a Start Menu shortcut"
Name: startup; Description: "Run TRAKFROM-BX when Windows starts (requires UAC)"; Flags: unchecked

[Files]
#include PayloadInclude

[Icons]
Name: "{autodesktop}\TRAKFROM-BX"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -B -m rekordbox_usb.public_launcher service"; WorkingDir: "{app}"; Tasks: desktop
Name: "{group}\TRAKFROM-BX"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -B -m rekordbox_usb.public_launcher service"; WorkingDir: "{app}"; Tasks: startmenu
Name: "{group}\Uninstall TRAKFROM-BX"; Filename: "{uninstallexe}"; WorkingDir: "{app}"; Tasks: startmenu

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -B -m rekordbox_usb.public_launcher service"; WorkingDir: "{app}"; Description: "Launch TRAKFROM-BX"; Flags: postinstall nowait skipifsilent runasoriginaluser unchecked

[INI]
Filename: "{app}\install-marker.ini"; Section: "installation"; Key: "product"; String: "TRAKFROM-BX"
Filename: "{app}\install-marker.ini"; Section: "installation"; Key: "app_id"; String: "{{574E3A6C-90F3-489F-A34F-C1D1A3E8609F}"
Filename: "{app}\install-marker.ini"; Section: "installation"; Key: "schema"; String: "1"
Filename: "{app}\install-marker.ini"; Section: "installation"; Key: "version"; String: "{#ProductVersion}"
Filename: "{app}\install-marker.ini"; Section: "installation"; Key: "build"; String: "{#BuildNumber}"
Filename: "{app}\install-marker.ini"; Section: "installation"; Key: "path"; String: "{app}"
Filename: "{app}\install-marker.ini"; Section: "installation"; Key: "installation_id"; String: "{code:InstallationId}"

[UninstallDelete]
Type: files; Name: "{app}\install-marker.ini"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
var
  Existing: Boolean;
  SavedId: String;
  OriginalStartup: Boolean;
  StartupChoiceLoaded: Boolean;

function GetFileAttributesW(Name: String): Cardinal;
  external 'GetFileAttributesW@kernel32.dll stdcall';

function SafeAncestors(Path: String): Boolean;
var Attributes: Cardinal; Parent: String;
begin
  Result := False;
  while Length(Path) > 3 do begin
    Attributes := GetFileAttributesW(Path);
    if (Attributes <> $FFFFFFFF) and ((Attributes and $400) <> 0) then exit;
    Parent := ExtractFileDir(Path);
    if Parent = Path then exit;
    Path := Parent;
  end;
  Result := True;
end;

function SafeTree(Path: String): Boolean;
var Find: TFindRec;
begin
  Result := SafeAncestors(Path);
  if not Result then exit;
  if FindFirst(AddBackslash(Path) + '*', Find) then begin
    try
      repeat
        if (Find.Name <> '.') and (Find.Name <> '..') then begin
          if (Find.Attributes and $400) <> 0 then begin Result := False; exit; end;
          if (Find.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
            if not SafeTree(AddBackslash(Path) + Find.Name) then begin Result := False; exit; end;
        end;
      until not FindNext(Find);
    finally FindClose(Find); end;
  end;
end;

function OwnInstallation(Path: String): Boolean;
var Marker: String;
begin
  Marker := AddBackslash(Path) + 'install-marker.ini';
  Result := FileExists(Marker) and
    (GetIniString('installation', 'product', '', Marker) = 'TRAKFROM-BX') and
    (GetIniString('installation', 'app_id', '', Marker) = '{#ProductId}') and
    (GetIniString('installation', 'schema', '', Marker) = '1') and
    (CompareText(GetIniString('installation', 'path', '', Marker), Path) = 0) and
    (GetIniString('installation', 'installation_id', '', Marker) <> '');
end;

function DirectoryEmpty(Path: String): Boolean;
var Find: TFindRec;
begin
  Result := True;
  if FindFirst(AddBackslash(Path) + '*', Find) then begin
    try
      repeat
        if (Find.Name <> '.') and (Find.Name <> '..') then begin Result := False; exit; end;
      until not FindNext(Find);
    finally FindClose(Find); end;
  end;
end;

function InstallationId(Param: String): String;
begin
  if SavedId = '' then SavedId := GetSHA256OfString(ExpandConstant('{app}') + '|' + GetDateTimeString('yyyymmddhhnnss', '-', ':'));
  Result := SavedId;
end;

function StartupCommand: String;
begin
  Result := '"' + ExpandConstant('{app}\runtime\pythonw.exe') + '" -I -B -m rekordbox_usb.public_launcher service';
end;

function AppArguments: String;
begin
  Result := '-I -B -m rekordbox_usb.public_launcher service';
end;

function SafeLink(Path, Target, Arguments: String): Boolean;
var Shell, Link: Variant;
begin
  Result := SafeAncestors(Path);
  if not Result or not FileExists(Path) then exit;
  try
    Shell := CreateOleObject('WScript.Shell');
    Link := Shell.CreateShortcut(Path);
    Result := (CompareText(Link.TargetPath, Target) = 0) and
      (Link.Arguments = Arguments) and
      (CompareText(Link.WorkingDirectory, ExpandConstant('{app}')) = 0);
  except Result := False; end;
end;

function SafeLinks: Boolean;
var Uninstaller: String;
begin
  Uninstaller := ExpandConstant('{uninstallexe}');
  // During upgrade Inno may reserve another filename before merging its log.
  // Validate the existing link against this AppId's registered uninstaller.
  if RegQueryStringValue(HKA, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#ProductId}_is1', 'UninstallString', Uninstaller) then begin
    if (Length(Uninstaller) >= 2) and (Uninstaller[1] = '"') and (Uninstaller[Length(Uninstaller)] = '"') then
      Uninstaller := Copy(Uninstaller, 2, Length(Uninstaller) - 2);
    if (CompareText(ExtractFileDir(Uninstaller), ExpandConstant('{app}')) <> 0) or
       (CompareText(ExtractFileExt(Uninstaller), '.exe') <> 0) or
       (Pos('unins', Lowercase(ExtractFileName(Uninstaller))) <> 1) then begin Result := False; exit; end;
  end;
  Result := SafeLink(ExpandConstant('{autodesktop}\TRAKFROM-BX.lnk'),
    ExpandConstant('{app}\runtime\pythonw.exe'), AppArguments) and
    SafeLink(ExpandConstant('{group}\TRAKFROM-BX.lnk'),
    ExpandConstant('{app}\runtime\pythonw.exe'), AppArguments) and
    SafeLink(ExpandConstant('{group}\Uninstall TRAKFROM-BX.lnk'),
    Uninstaller, '');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Path, Marker, Previous, Value: String;
begin
  Result := '';
  Path := ExpandConstant('{app}');
  if not SafeTree(Path) then begin Result := 'Redirected installation paths are not allowed.'; exit; end;
  if FileExists(Path) then begin Result := 'The installation path is a file.'; exit; end;
  if not SafeLinks then begin Result := 'A shortcut name belongs to another installation or is redirected.'; exit; end;
  if (IsAdminInstallMode and RegKeyExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#ProductId}_is1')) or
     (not IsAdminInstallMode and RegKeyExists(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#ProductId}_is1')) then begin
    Result := 'This product is already registered in another installation mode. Use its existing mode or uninstall it before changing scope.'; exit;
  end;
  if RegQueryStringValue(HKA, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#ProductId}_is1', 'Inno Setup: App Path', Previous) then
    if CompareText(Previous, Path) <> 0 then begin Result := 'An existing installation must be updated at its current path. Uninstall it before moving.'; exit; end;
  if RegKeyExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\TRAKFROM-BX') then begin
    Result := 'Uninstall the previous PowerShell installation first. User catalogs will be preserved.'; exit;
  end;
  Existing := OwnInstallation(Path);
  if DirExists(Path) and not DirectoryEmpty(Path) and not Existing then begin
    Result := 'Choose an empty folder or an identified TRAKFROM-BX installation.'; exit;
  end;
  Marker := AddBackslash(Path) + 'install-marker.ini';
  if Existing then begin
    if StrToIntDef(GetIniString('installation', 'build', '', Marker), -1) < 0 then begin Result := 'Invalid installed build marker.'; exit; end;
    if StrToIntDef(GetIniString('installation', 'build', '', Marker), -1) > {#BuildNumber} then begin Result := 'A newer build is installed. Downgrade is blocked.'; exit; end;
    SavedId := GetIniString('installation', 'installation_id', '', Marker);
  end;
  if RegQueryStringValue(HKCU, RunKey, 'TRAKFROM-BX', Value) and (Value <> StartupCommand) then begin
    Result := 'The TRAKFROM-BX startup name belongs to another installation. Nothing was changed.'; exit;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
var Value: String;
begin
  if (CurPageID = wpSelectTasks) and not WizardSilent and not StartupChoiceLoaded and
     (Pos('/TASKS=', Uppercase(GetCmdTail)) = 0) and
     (Pos('/MERGETASKS=', Uppercase(GetCmdTail)) = 0) and
     OwnInstallation(ExpandConstant('{app}')) then begin
    StartupChoiceLoaded := True;
    OriginalStartup := RegQueryStringValue(HKCU, RunKey, 'TRAKFROM-BX', Value) and (Value = StartupCommand);
    WizardForm.TasksList.Checked[2] := OriginalStartup;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Code: Integer; Choice: String;
begin
  if CurStep = ssPostInstall then begin
    if not WizardIsTaskSelected('desktop') then
      if FileExists(ExpandConstant('{autodesktop}\TRAKFROM-BX.lnk')) then
        if not DeleteFile(ExpandConstant('{autodesktop}\TRAKFROM-BX.lnk')) then RaiseException('Could not remove owned Desktop shortcut.');
    if not WizardIsTaskSelected('startmenu') then begin
      if FileExists(ExpandConstant('{group}\TRAKFROM-BX.lnk')) then
        if not DeleteFile(ExpandConstant('{group}\TRAKFROM-BX.lnk')) then RaiseException('Could not remove owned Start Menu shortcut.');
      if FileExists(ExpandConstant('{group}\Uninstall TRAKFROM-BX.lnk')) then
        if not DeleteFile(ExpandConstant('{group}\Uninstall TRAKFROM-BX.lnk')) then RaiseException('Could not remove owned uninstall shortcut.');
    end;
    Choice := 'no';
    if WizardIsTaskSelected('startup') then Choice := 'yes';
    if Existing and (Pos('/TASKS=', Uppercase(GetCmdTail)) = 0) and
       (Pos('/MERGETASKS=', Uppercase(GetCmdTail)) = 0) and WizardSilent then Choice := 'preserve';
    if not ExecAsOriginalUser(ExpandConstant('{app}\runtime\pythonw.exe'),
      '-I -B -m rekordbox_usb.bundled_setup --startup ' + Choice,
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
      RaiseException('Could not save startup preference as the normal user. Start the installer normally, without Run as administrator, and retry.');
  end;
end;

function InitializeUninstall: Boolean;
begin
  Result := OwnInstallation(ExpandConstant('{app}')) and SafeTree(ExpandConstant('{app}')) and SafeLinks;
  if not Result then MsgBox('Installation identity or path safety check failed. No files were removed.', mbError, MB_OK);
end;

procedure RemoveOwnedStartup(Root: Integer; Key: String);
var Value: String;
begin
  if RegQueryStringValue(Root, Key, 'TRAKFROM-BX', Value) and (Value = StartupCommand) then
    if not RegDeleteValue(Root, Key, 'TRAKFROM-BX') then RaiseException('Could not remove startup registration.');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Users: TArrayOfString; Index: Integer;
begin
  if CurUninstallStep = usUninstall then begin
    if not OwnInstallation(ExpandConstant('{app}')) or not SafeTree(ExpandConstant('{app}')) then
      RaiseException('Installation changed after preflight; uninstall stopped.');
    RemoveOwnedStartup(HKCU, RunKey);
    // Machine-wide uninstall may run under a different administrator. Only exact
    // commands for this installation are removed from currently loaded profiles.
    if IsAdminInstallMode and RegGetSubkeyNames(HKU, '', Users) then
      for Index := 0 to GetArrayLength(Users) - 1 do
        if (Pos('S-1-5-21-', Users[Index]) = 1) and (Pos('_Classes', Users[Index]) = 0) then
          RemoveOwnedStartup(HKU, Users[Index] + '\' + RunKey);
  end;
end;
