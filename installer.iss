; YouTube Downloader Setup. Build with: python build_installer.py (it passes Version and Wheel).
;
; Setup itself is small. When Install is clicked it downloads uv, which then downloads Python,
; yt-dlp, ffmpeg and deno (about 150 MB) into the install folder. Nothing else on the machine
; is touched, so uninstalling just deletes those folders.
; uv and its download cache stay in the install folder: running a newer Setup downloads only what
; changed, and the app's "Update yt-dlp" button uses them too.

#define Name "YouTube Downloader"
#define WheelName ExtractFileName(Wheel)
#define Uv "0.11.25"
#define UvHash "15bfd1423b7eaa7aae949922d4712ebaac2bb44a81af64ab59bbe007090cb0d0"

[Setup]
AppId={{6E8CD4FB-36FC-48A8-8B1B-CCC6EC8CAB98}
AppName={#Name}
AppVersion={#Version}
DefaultDirName={autopf}\{#Name}
; per-user install into %LOCALAPPDATA%\Programs, no admin prompt
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; what uv installs (about 275 MB), uv itself (75 MB) and room for downloads. The cache costs
; little extra: uv hard-links the installed files to it.
ExtraDiskSpaceRequired=450000000
; the uv download is a .zip
ArchiveExtraction=full
; uv links each Python version folder with a junction, which RedirectionGuard blocks (error 448).
; The guard stops junction tricks against elevated installers; this one never runs elevated.
RedirectionGuard=no
; the "Add to PATH" task
ChangesEnvironment=yes
; created by the running window, so Setup and the uninstaller ask to close it first
AppMutex=YouTubeDownloader
SetupIconFile=ytd\icon.ico
UninstallDisplayIcon={app}\icon.ico
UninstallDisplayName={#Name}
WizardStyle=modern dynamic
OutputDir=dist
OutputBaseFilename=YouTubeDownloader-Setup

[Tasks]
Name: desktopicon; Description: "{cm:CreateDesktopIcon}"
Name: addtopath; Description: "Add the ytd command to PATH (to download from a terminal)"

[Files]
Source: "{#Wheel}"; Flags: dontcopy
Source: "ytd\icon.ico"; DestDir: "{app}"

[Icons]
Name: "{autoprograms}\{#Name}"; Filename: "{app}\bin\ytd-gui.exe"; IconFilename: "{app}\icon.ico"
Name: "{autodesktop}\{#Name}"; Filename: "{app}\bin\ytd-gui.exe"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Registry]
; no uninsdeletevalue flag: that would delete the whole PATH; CurUninstallStepChanged removes our part
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\bin"; \
  Tasks: addtopath; Check: NotOnPath
; ytd:<link> opens the link in the app: the app's browser button (a bookmark) makes these
Root: HKCU; Subkey: "Software\Classes\ytd"; ValueType: string; ValueData: "URL:{#Name}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\ytd"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\ytd\DefaultIcon"; ValueType: string; ValueData: "{app}\icon.ico"
Root: HKCU; Subkey: "Software\Classes\ytd\shell\open\command"; ValueType: string; \
  ValueData: """{app}\bin\ytd-gui.exe"" ""%1"""

[Run]
Filename: "{app}\bin\ytd-gui.exe"; Description: "{cm:LaunchProgram,{#Name}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Python, the app, uv and its cache. Only these: {app} may be a folder the user picked that holds other files.
Type: filesandordirs; Name: "{app}\bin"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\tool"
Type: filesandordirs; Name: "{app}\uv"
Type: filesandordirs; Name: "{app}\cache"
; uv created {app} before Setup did, so Setup doesn't know to remove it
Type: dirifempty; Name: "{app}"
; the window's settings
Type: filesandordirs; Name: "{userappdata}\ytd"

[Code]
function SetEnvironmentVariable(Name, Value: String): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

var
  DownloadPage: TDownloadWizardPage;
  LastLine: String;
  InstallTried: Boolean;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), nil);
end;

procedure ShowUvLine(const S: String; const Error, FirstLine: Boolean);
begin
  Log('uv: ' + S);
  if Trim(S) <> '' then begin
    LastLine := Trim(S);
    DownloadPage.Msg2Label.Caption := LastLine;
  end;
end;

// Python and the app's libraries, downloaded by uv into {app}. Returns an error message, '' if it worked.
function InstallApp: String;
var
  UvExe: String;
  ResultCode: Integer;
begin
  UvExe := ExpandConstant('{app}\uv\{#Uv}\uv.exe');
  if not FileExists(UvExe) then begin  // else kept from the last install
    DownloadPage.Clear;
    DownloadPage.Add('https://github.com/astral-sh/uv/releases/download/{#Uv}/uv-x86_64-pc-windows-msvc.zip', 'uv.zip', '{#UvHash}');
    DownloadPage.AbortButton.Show;
    DownloadPage.Download;  // raises if it fails or the user clicks Abort
    DownloadPage.AbortButton.Hide;  // can't stop uv midway
    DelTree(ExpandConstant('{app}\uv'), True, True, True);  // an older uv
    ExtractArchive(ExpandConstant('{tmp}\uv.zip'), ExtractFileDir(UvExe), '', False, nil);
  end;
  ExtractTemporaryFile('{#WheelName}');

  DownloadPage.SetText('Downloading Python and the app''s components (up to 150 MB)...', '');
  DownloadPage.ProgressBar.Style := npbstMarquee;
  // keep uv's files inside {app}: no registry entries, no stray python.exe
  SetEnvironmentVariable('UV_TOOL_DIR', ExpandConstant('{app}\tool'));
  SetEnvironmentVariable('UV_TOOL_BIN_DIR', ExpandConstant('{app}\bin'));
  SetEnvironmentVariable('UV_PYTHON_INSTALL_DIR', ExpandConstant('{app}\python'));
  SetEnvironmentVariable('UV_PYTHON_INSTALL_BIN', '0');
  SetEnvironmentVariable('UV_PYTHON_INSTALL_REGISTRY', '0');
  if GetEnv('UV_CACHE_DIR') = '' then  // tests set it to reuse downloads
    SetEnvironmentVariable('UV_CACHE_DIR', ExpandConstant('{app}\cache'));
  DelTree(ExpandConstant('{app}\tool'), True, True, True);  // a fresh environment, so running Setup again updates yt-dlp
  if ExecAndLogOutput(UvExe,
      'tool install --managed-python --force "' + ExpandConstant('{tmp}\{#WheelName}') + '"',
      '', SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode, @ShowUvLine) and (ResultCode = 0) then
    Result := ''
  else begin
    // Python, uv and the cache stay, so trying again only downloads what's missing
    DelTree(ExpandConstant('{app}\bin'), True, True, True);
    DelTree(ExpandConstant('{app}\tool'), True, True, True);
    Result := 'Could not download the app''s components: ' + LastLine;
  end;
end;

// Setup is closing without the app installed (failed, then gave up): remove what uv downloaded.
procedure DeinitializeSetup;
begin
  if InstallTried and not FileExists(ExpandConstant('{app}\bin\ytd-gui.exe')) then begin
    DelTree(ExpandConstant('{app}\python'), True, True, True);
    DelTree(ExpandConstant('{app}\uv'), True, True, True);
    DelTree(ExpandConstant('{app}\cache'), True, True, True);
    RemoveDir(ExpandConstant('{app}'));  // only if that left it empty
  end;
end;

// Runs when Install is clicked, before Setup copies anything. On failure Setup stays on the
// Ready page (a silent install exits), so nothing is left half-installed.
function NextButtonClick(CurPageID: Integer): Boolean;
var
  Error: String;
begin
  Result := True;
  if CurPageID <> wpReady then
    exit;
  InstallTried := True;
  DownloadPage.Show;
  try
    try
      Error := InstallApp;
    except
      if DownloadPage.AbortedByUser then
        Error := 'Download cancelled.'
      else
        Error := GetExceptionMessage;
    end;
  finally
    DownloadPage.ProgressBar.Style := npbstNormal;
    DownloadPage.Hide;
  end;
  if Error <> '' then begin
    SuppressibleMsgBox(AddPeriod(Error) + #13#10#13#10 + 'Check the internet connection and click Install to try again.',
      mbCriticalError, MB_OK, IDOK);
    Result := False;
  end;
end;

function NotOnPath: Boolean;
var
  Path: String;
begin
  RegQueryStringValue(HKCU, 'Environment', 'Path', Path);
  Result := Pos(';' + Uppercase(ExpandConstant('{app}\bin')) + ';', ';' + Uppercase(Path) + ';') = 0;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Path, Bin: String;
  P: Integer;
begin
  if CurUninstallStep = usUninstall then  // the app's Options: Start with Windows
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'YouTube Downloader');
  if (CurUninstallStep = usUninstall) and RegQueryStringValue(HKCU, 'Environment', 'Path', Path) then begin
    Bin := ';' + Uppercase(ExpandConstant('{app}\bin')) + ';';
    Path := Path + ';';  // so the last entry also ends in ;
    P := Pos(Bin, Uppercase(Path));
    if P > 0 then begin
      Delete(Path, P, Length(Bin) - 1);  // keeps the ; that followed
      RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Copy(Path, 1, Length(Path) - 1));
    end;
  end;
end;
