; Saylight per-user installer (Inno Setup 6.7). No admin rights and no UAC prompt.
;
; Build, after collect_notices.py has run on the folder (from a path under 260 characters):
;   ISCC.exe [/DTEST] [/DSourceDir=<dist\Saylight>] [/O<output folder>] packaging\saylight.iss
; Silent install or upgrade:
;   Saylight-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART [/TASKS="startup"]
; An update Saylight starts itself: Saylight-Setup.exe /SILENT /RELAUNCH=1 (Setup starts Saylight again
; afterwards, even if it had already quit). Started silently from Saylight's download folder
; (%TEMP%\saylight_update_*), Setup does the same without the flag.
;
; /DTEST builds "Saylight Test" with the TEST names from identity.iss (its own AppId, folder, quit event,
; Run value and shortcut), for lifecycle_test.ps1 and a build made by make_test_copy.py.
;
; Releases are not code-signed, so the finish page says what to do if Windows blocks Saylight.

#include "identity.iss"

#ifndef SourceDir
  #define SourceDir AddBackslash(SourcePath) + "..\dist\Saylight"
#endif
#ifndef OutputDir
  #define OutputDir AddBackslash(SourcePath) + "..\dist"
#endif
; Wizard images drawn from the app icon by make_icon.py (saylight.spec writes them next to the build folder).
#ifndef ImagesDir
  #define ImagesDir AddBackslash(SourceDir) + "..\Saylight-wizard"
#endif
#define WizardImages(Kind) \
  ImagesDir + "\wizard_" + Kind + "_100.png," + ImagesDir + "\wizard_" + Kind + "_125.png," + \
  ImagesDir + "\wizard_" + Kind + "_150.png," + ImagesDir + "\wizard_" + Kind + "_200.png," + \
  ImagesDir + "\wizard_" + Kind + "_250.png"
#define AppExe "Saylight.exe"
#define AppVersion GetStringFileInfo(AddBackslash(SourceDir) + AppExe, "ProductVersion")
#if AppVersion == ""
  #error Saylight.exe has no version resource; build it with packaging\saylight.spec first
#endif
; VersionInfoVersion takes numbers only, so a version such as 1.3.0-rc.1 uses the exe's numeric version.
#define AppVersionNumbers GetVersionNumbersString(AddBackslash(SourceDir) + AppExe)
; saylight.spec stamps a test copy's exe (SpecialBuild "Test copy"). Only the TEST installer may wrap one, and
; it may wrap nothing else: a release must never ship the test names with the shortcut and updates off, and a
; test install must never start a copy that answers to the real quit event and shortcut.
#define BuildStamp GetStringFileInfo(AddBackslash(SourceDir) + AppExe, "SpecialBuild")
#ifdef TEST
  #if BuildStamp != "Test copy"
    #error /DTEST needs a build of a test copy (make_test_copy.py, then saylight.spec); this Saylight.exe isn't one
  #endif
#else
  #if BuildStamp != ""
    #error This Saylight.exe was built from a test copy; build the release installer from the repository
  #endif
#endif
#if !FileExists(AddBackslash(SourceDir) + "THIRD_PARTY_NOTICES.txt")
  #error THIRD_PARTY_NOTICES.txt is missing; run packaging\collect_notices.py on the build first
#endif
#ifdef TEST
  #define SetupName "SaylightTest-Setup"
#else
  #define SetupName "Saylight-Setup"
#endif

[Setup]
AppId={#AppIdGuid}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Cole Craven
AppPublisherURL=https://github.com/ColeCraven/saylight
AppSupportURL=https://github.com/ColeCraven/saylight/issues
AppUpdatesURL=https://github.com/ColeCraven/saylight/releases
AppCopyright=(c) 2026 Cole Craven. MIT License.
VersionInfoVersion={#AppVersionNumbers}
VersionInfoTextVersion={#AppVersion}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
; Per user: installs without admin rights and registers the uninstaller under HKCU, so it shows in
; Settings > Apps for this user. The folder is the one Saylight 1.0 used.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableWelcomePage=yes
UsePreviousTasks=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
SetupIconFile={#SourceDir}\_internal\saylight.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern dynamic
WizardSizePercent=110
#if FileExists(ImagesDir + "\wizard_small_100.png")
WizardSmallImageFile={#WizardImages("small")}
WizardImageFile={#WizardImages("large")}
WizardSmallImageFileDynamicDark={#WizardImages("small")}
WizardImageFileDynamicDark={#WizardImages("large")}
#endif
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
OutputDir={#OutputDir}
OutputBaseFilename={#SetupName}
; Saylight is closed through its own quit event first (see [Code]). The Restart Manager stays as a fallback
; for a copy that doesn't answer, but Setup never restarts anything on its own.
CloseApplications=yes
RestartApplications=no
; Open with is registered below; Explorer is told, so it lists Saylight straight away.
ChangesAssociations=yes
; Code signing, if a certificate is ever available (signs Setup and the uninstaller):
; SignTool=signtool sign /fd sha256 /tr http://timestamp.acs.microsoft.com /td sha256 $f
; SignedUninstaller=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

; Plain, short lines: many people who use Saylight are dyslexic. Releases aren't code-signed, so the finish page
; also says what to do if Windows blocks Saylight.
#define FinishText \
  "Highlight text in any app, then press Ctrl+` to hear it.%n(The ` key is to the left of 1.)%n%n" + \
  "If Windows blocks Saylight%n" + \
  '• If you see "Windows protected your PC", choose More info, then Run anyway.%n' + \
  "• Smart App Control can't make an exception for one app. If it stops Saylight, " + \
  'see "If Windows blocks Saylight" at github.com/ColeCraven/saylight.'

[Messages]
en.FinishedHeadingLabel=[name] is ready
en.FinishedLabel={#FinishText}
en.FinishedLabelNoIcons={#FinishText}
en.WizardSelectTasks=Options
en.SelectTasksDesc=Choose how [name] starts.
en.SelectTasksLabel2=[name] can start when you sign in, so it's ready whenever you highlight text.

[CustomMessages]
en.StartupTask=Start {#AppName} when I sign in
en.LaunchNow=Start {#AppName} now
en.RemoveSettings=Also remove your {#AppName} settings?%n%nChoose No to keep them for next time.
en.StillRunning={#AppName} is still running. Quit it from its icon in the taskbar corner, then try again.

[Tasks]
Name: "startup"; Description: "{cm:StartupTask}"

[InstallDelete]
; 1.0 was installed from source into this same folder. Remove its Python files so only the new build is left.
; Its settings file stays: Saylight moves it to %LOCALAPPDATA%\{#DataDirName} on its first start.
Type: files; Name: "{app}\saylight_app.py"
Type: files; Name: "{app}\requirements.txt"
Type: files; Name: "{app}\saylight.ico"
Type: files; Name: "{app}\read_aloud.ico"
Type: filesandordirs; Name: "{app}\saylight"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files; Name: "{app}\saylight.log*"
Type: files; Name: "{app}\saylight_crash.log"
; A previous build's runtime folder and texts, so nothing an older build shipped lingers.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\licenses"
; The earlier name's Start menu entry.
Type: files; Name: "{userprograms}\{#OldShortcut}.lnk"

[Files]
Source: "{#SourceDir}\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Directly in Start > All apps (no folder), at the same path 1.0 used, so an upgrade replaces 1.0's entry.
Name: "{userprograms}\{#ShortcutName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; \
  Comment: "Read highlighted text aloud"; AppUserModelID: "{#AumId}"

[Registry]
; An update Saylight started keeps the sign-in entry as Settings > General left it.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#RunValue}"; \
  ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: startup; Check: not StartedByUpdate
; Unticked on an upgrade: remove the sign-in entry (this also replaces 1.0's pythonw entry of the same name).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#RunValue}"; \
  Flags: deletevalue; Tasks: not startup; Check: not StartedByUpdate
; The sign-in entry of the app's earlier name, if one is still there.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#OldRunValue}"; \
  Flags: deletevalue
; Open with: Saylight is listed for the file kinds it reads, through its own ProgID. It never sets a default:
; no file kind's default value or UserChoice is touched, and uninstalling removes all of it. .markdown is left
; out: it rarely has a default, and Windows then asks "Pick an app" for it after install.
Root: HKCU; Subkey: "Software\Classes\{#ProgId}"; ValueType: string; ValueName: ""; \
  ValueData: "{#AppName} document"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\{#ProgId}\DefaultIcon"; ValueType: string; ValueName: ""; \
  ValueData: """{app}\{#AppExe}"",0"
Root: HKCU; Subkey: "Software\Classes\{#ProgId}\shell\open"; ValueType: string; \
  ValueName: "FriendlyAppName"; ValueData: "{#AppName}"
Root: HKCU; Subkey: "Software\Classes\{#ProgId}\shell\open\command"; ValueType: string; ValueName: ""; \
  ValueData: """{app}\{#AppExe}"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; \
  ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.docx\OpenWithProgids"; ValueType: string; \
  ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.txt\OpenWithProgids"; ValueType: string; \
  ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.md\OpenWithProgids"; ValueType: string; \
  ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent
; A silent upgrade (for example one Saylight started itself) brings Saylight back if it was running.
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Flags: nowait; Check: RelaunchAfterSilentUpgrade

[UninstallDelete]
#ifdef TEST
; A test copy keeps its data next to the exe unless SAYLIGHT_DATA_DIR says otherwise.
Type: filesandordirs; Name: "{app}\test_data"
#endif
Type: dirifempty; Name: "{app}"

[Code]
const
  EVENT_MODIFY_STATE = $0002;
  SYNCHRONIZE = $00100000;

var
  WasRunning: Boolean;

function OpenEvent(dwDesiredAccess: DWORD; bInheritHandle: BOOL; lpName: string): THandle;
  external 'OpenEventW@kernel32.dll stdcall';
function SetEvent(hEvent: THandle): BOOL;
  external 'SetEvent@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

{ The running copy owns its named quit event, so the event disappears when that copy exits. }
function EventExists(const Name: string): Boolean;
var
  H: THandle;
begin
  H := OpenEvent(SYNCHRONIZE, False, Name);
  Result := H <> 0;
  if Result then
    CloseHandle(H);
end;

{ Ask a running Saylight to quit (what "Saylight.exe --quit" does) and wait up to TimeoutMs for it to save its
  settings and exit. True when no copy is running any more. }
function QuitRunningCopy(const Name: string; TimeoutMs: Integer): Boolean;
var
  H: THandle;
  Waited: Integer;
begin
  H := OpenEvent(EVENT_MODIFY_STATE, False, Name);
  if H = 0 then begin
    Result := True;
    Exit;
  end;
  SetEvent(H);
  CloseHandle(H);
  Log('Asked the running copy to quit');
  Waited := 0;
  while EventExists(Name) and (Waited < TimeoutMs) do begin
    Sleep(100);
    Waited := Waited + 100;
  end;
  Result := not EventExists(Name);
  Log(Format('Running copy gone: %d after %d ms', [Ord(Result), Waited]));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  WasRunning := EventExists('{#QuitEvent}');
  if not QuitRunningCopy('{#QuitEvent}', 5000) then
    Log('Still running after 5 s; the Restart Manager will offer to close it');
  QuitRunningCopy('{#OldQuitEvent}', 3000);
end;

{ Saylight downloads an update into %TEMP%\<prefix>update_<random>\ and starts it from there, then quits. }
function StartedByUpdate: Boolean;
var
  Folder: String;
begin
  Folder := ExtractFileName(RemoveBackslash(ExtractFileDir(ExpandConstant('{srcexe}'))));
  Result := Pos(Lowercase('{#CachePrefix}update_'), Lowercase(Folder)) = 1;
end;

{ After a silent upgrade, start Saylight again if Setup closed it, or if Saylight started this update and quit
  before Setup looked: Setup runs from Saylight's download folder, or the command line has /RELAUNCH=1. }
function RelaunchAfterSilentUpgrade: Boolean;
begin
  Result := WizardSilent and (WasRunning or StartedByUpdate or (ExpandConstant('{param:RELAUNCH|0}') = '1'));
end;

{ The ready page is off, so the options page is the last one before installing: say so on its button. }
procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpSelectTasks then
    WizardForm.NextButton.Caption := SetupMessage(msgButtonInstall);
end;

function InitializeUninstall: Boolean;
begin
  Result := True;
  if not QuitRunningCopy('{#QuitEvent}', 5000) then begin
    if not UninstallSilent then
      MsgBox(CustomMessage('StillRunning'), mbError, MB_OK);
    Result := False;
  end;
end;

{ Playback audio only ever lives in %TEMP%\<prefix><random> folders. A running copy deletes its own when it
  quits; this removes any a crash left behind. }
procedure DeleteCacheFolders(const Prefix: string);
var
  Root: string;
  F: TFindRec;
begin
  Root := ExpandConstant('{%TEMP}');
  if (Root = '') or (Prefix = '') then
    Exit;
  if FindFirst(AddBackslash(Root) + Prefix + '*', F) then begin
    try
      repeat
        if (F.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          DelTree(AddBackslash(Root) + F.Name, True, True, True);
      until not FindNext(F);
    finally
      FindClose(F);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then begin
    { The sign-in entry, also when Settings > General added it rather than Setup. }
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#RunValue}');
    DeleteCacheFolders('{#CachePrefix}');
    DeleteCacheFolders('{#OldCachePrefix}');
    DataDir := ExpandConstant('{localappdata}\{#DataDirName}');
    { Settings are kept unless the person asks; a silent uninstall always keeps them. }
    if not UninstallSilent and (DirExists(DataDir) or FileExists(ExpandConstant('{app}\saylight_settings.json'))) then
      if MsgBox(CustomMessage('RemoveSettings'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then begin
        DelTree(DataDir, True, True, True);
        DeleteFile(ExpandConstant('{app}\saylight_settings.json'));
        RemoveDir(ExpandConstant('{app}'));
      end;
  end;
end;
