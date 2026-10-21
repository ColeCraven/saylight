<#
.SYNOPSIS
  Installer lifecycle test for the TEST-named Saylight installer ("ISCC /DTEST"), built from a test copy.

.DESCRIPTION
  Runs, all silently, in a short sandbox folder:
    1. fresh install with "Start with Windows" ticked
    2. first start of the installed copy
    3. upgrade while it runs, with "Start with Windows" unticked (it must quit, then start again)
       and the in-app update path: Setup /RELAUNCH=1 after Saylight has quit (it must start again), and
       Setup started from Saylight's download folder without the flag (the same)
    4. upgrade over a simulated 1.0 source install (Python files go; the app moves the settings file into the
       data folder)
    5. uninstall while it runs
    6. with -Legacy: upgrade over a 1.1 source install laid out like a real one (saylight_app.py and the
       saylight package in the install folder, started with pythonw by its own --install, its settings next
       to it), while it runs; then uninstall again
  and checks the files, the Run value, the Start menu shortcut and its app ID, the Settings > Apps entry, and
  that nothing is left afterwards.

  Only the TEST names from identity.iss are ever used: the test AppId, the test install folder, the test Run
  value, shortcut and quit event. The script refuses an installer that isn't the test one, never signals the
  real Saylight, and only stops processes that run from the sandbox. It records the real Saylight's state
  before and after and fails if anything about it changed. It starts the installed app only when its exe is
  stamped as a test-copy build, and it uses a sandbox folder only when that is empty or an earlier run made it.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File packaging\lifecycle_test.ps1 -Setup dist\SaylightTest-Setup.exe

.PARAMETER Legacy
  A test copy of the 1.1 source (make_test_copy.py on "git archive v1.1.0"), for step 6. -Python names the
  python.exe that runs it.
#>
param(
  [Parameter(Mandatory = $true)][string]$Setup,
  [string]$Sandbox = (Join-Path $env:TEMP 'sltest'),
  [string]$Report = '',
  [string]$Legacy = '',
  [string]$Python = '',
  [switch]$KeepSandbox
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

# --- Identity -----------------------------------------------------------------------------------------

function Read-Identity([string]$Path) {
  $sets = @{ test = @{}; real = @{} }
  $current = $null
  foreach ($line in Get-Content -LiteralPath $Path) {
    $t = $line.Trim()
    if ($t.StartsWith('#ifdef TEST')) { $current = 'test' }
    elseif ($t.StartsWith('#else')) { $current = 'real' }
    elseif ($t.StartsWith('#endif')) { $current = $null }
    elseif ($current -and ($line -match '^\s*#define\s+(\w+)\s+"([^"]*)"\s*$')) {
      $sets[$current][$Matches[1]] = $Matches[2].Replace('{{', '{')
    }
  }
  return $sets
}

$ident = Read-Identity (Join-Path $PSScriptRoot 'identity.iss')
$T = $ident.test
$Real = $ident.real
foreach ($k in 'AppName', 'AppIdGuid', 'RunValue', 'QuitEvent', 'AumId', 'ShortcutName', 'CachePrefix') {
  if (-not $T.ContainsKey($k) -or $T[$k] -eq $Real[$k]) { throw "identity.iss: TEST $k is missing or equals the real one" }
}

# saylight.spec writes this SpecialBuild string into the exe of a test copy (make_test_copy.BUILD_STAMP).
$TestBuildStamp = 'Test copy'

if ($Sandbox.Length -gt 120) { throw 'Use a short sandbox path: Inno Setup fails above 260 characters' }
$sb = [IO.Path]::GetFullPath($Sandbox)
# The sandbox is emptied at the start and deleted at the end, so only an empty folder or one this script made
# (it leaves a marker file) is ever used.
$marker = Join-Path $sb '.saylight-lifecycle-sandbox'
if ((Test-Path -LiteralPath $sb) -and @(Get-ChildItem -LiteralPath $sb -Force).Count -gt 0 -and
    -not (Test-Path -LiteralPath $marker -PathType Leaf)) {
  throw "Refusing to use $sb as the sandbox: it isn't empty and wasn't made by this script"
}

$Setup = (Resolve-Path -LiteralPath $Setup).Path
$product = "$((Get-Item -LiteralPath $Setup).VersionInfo.ProductName)".Trim().Trim([char]0)  # Inno pads it
if ($product -ne $T.AppName) { throw "Refusing to run: $Setup is '$product', not the '$($T.AppName)' test installer" }

$app = Join-Path $sb $T.AppName
$data = Join-Path $sb 'data'
$tmp = Join-Path $sb 'tmp'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$unKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$($T.AppIdGuid)_is1"
$realUnKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$($Real.AppIdGuid)_is1"
$programs = [Environment]::GetFolderPath('Programs')
$lnk = Join-Path $programs "$($T.ShortcutName).lnk"

# Named events through .NET rather than Add-Type: compiling C# fails on some PCs (csc.exe can't be started).
function Open-Event([string]$Name, [Security.AccessControl.EventWaitHandleRights]$Rights) {
  $h = $null
  if ([Threading.EventWaitHandle]::TryOpenExisting($Name, $Rights, [ref]$h)) { return $h }
  return $null
}

function Test-Event([string]$Name) {
  $h = Open-Event $Name ([Security.AccessControl.EventWaitHandleRights]::Synchronize)
  if ($null -eq $h) { return $false }
  $h.Dispose(); return $true
}

function Stop-TestCopy {
  # Only ever the TEST quit event.
  $h = Open-Event $T.QuitEvent ([Security.AccessControl.EventWaitHandleRights]::Modify)
  if ($null -eq $h) { return $false }
  [void]$h.Set(); $h.Dispose(); return $true
}

function Get-TestProcs { @(Get-Process -Name Saylight -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($sb, [StringComparison]::OrdinalIgnoreCase) }) }
# The 1.1 source copy runs under pythonw; only those started on a script in the sandbox count.
function Get-LegacyProcs { @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($sb, [StringComparison]::OrdinalIgnoreCase) -ge 0 }) }
function Get-RunValue([string]$Name) { $v = Get-ItemProperty -Path $runKey -Name $Name -ErrorAction SilentlyContinue; if ($v) { $v.$Name } else { $null } }

function Get-RealState {
  $folder = Join-Path $env:LOCALAPPDATA 'Programs\Saylight'
  $files = @(Get-ChildItem -LiteralPath $folder -Recurse -File -Force -ErrorAction SilentlyContinue)
  $size = 0; foreach ($f in $files) { $size += $f.Length }
  $realLnk = Join-Path $programs "$($Real.ShortcutName).lnk"
  [ordered]@{
    running = (Test-Event $Real.QuitEvent)
    run_value = (Get-RunValue $Real.RunValue)
    uninstall_key = (Test-Path -LiteralPath $realUnKey)
    shortcut = $(if (Test-Path -LiteralPath $realLnk) { (Get-Item -LiteralPath $realLnk).LastWriteTimeUtc.Ticks } else { $null })
    install_folder = "$($files.Count) files, $size bytes"
  }
}

# --- Steps --------------------------------------------------------------------------------------------

$checks = New-Object System.Collections.Generic.List[object]
function Check([string]$Name, [bool]$Ok, $Detail = $null) {
  $checks.Add([ordered]@{ check = $Name; ok = $Ok; detail = $Detail })
}

function Invoke-Setup([string]$Log, [string[]]$Extra, [string]$From = $Setup) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $args_ = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$app`"", "/LOG=`"$sb\$Log`"") + $Extra
  # Process.WaitForExit on Setup's own PID: Start-Process -Wait would also wait for the Saylight it restarts.
  $p = Start-Process -FilePath $From -ArgumentList $args_ -WorkingDirectory $sb -PassThru
  $p.WaitForExit()
  [ordered]@{ exit = $p.ExitCode; seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1) }
}

function Wait-Started([datetime]$Since, [int]$TimeoutS = 45) {
  $log = Join-Path $data 'saylight.log'
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $TimeoutS) {
    if ((Test-Event $T.QuitEvent) -and (Test-Path -LiteralPath $log)) {
      $lines = @(Get-Content -LiteralPath $log | Where-Object { $_ -match 'Saylight (\S+ )?started' })
      foreach ($l in $lines) {
        $stamp = [datetime]::MinValue
        $ok = [datetime]::TryParseExact($l.Substring(0, [Math]::Min(19, $l.Length)), 'yyyy-MM-dd HH:mm:ss',
          [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$stamp)
        if ($ok -and $stamp -ge $Since.AddSeconds(-1)) {
          return [math]::Round($sw.Elapsed.TotalSeconds, 2)
        }
      }
    }
    Start-Sleep -Milliseconds 50
  }
  return $null
}

function Start-TestCopy {
  $since = Get-Date
  $p = Start-Process -FilePath (Join-Path $app 'Saylight.exe') -WorkingDirectory $app -PassThru
  $s = Wait-Started $since
  return @{ process = $p; started_s = $s }
}

function Stop-AndWait($Proc) {
  [void](Stop-TestCopy)
  if ($Proc) { [void]$Proc.WaitForExit(10000) }
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ((Test-Event $T.QuitEvent) -and $sw.Elapsed.TotalSeconds -lt 10) { Start-Sleep -Milliseconds 50 }
  return -not (Test-Event $T.QuitEvent)
}

function Get-Shortcut {
  if (-not (Test-Path -LiteralPath $lnk)) { return $null }
  $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
  $f = (New-Object -ComObject Shell.Application).NameSpace($programs).ParseName((Split-Path $lnk -Leaf))
  [ordered]@{ target = $s.TargetPath; workdir = $s.WorkingDirectory; aumid = $f.ExtendedProperty('System.AppUserModel.ID') }
}

function Remove-TestLeftovers {
  # After a failure: remove only what carries a TEST name or lives in the sandbox.
  foreach ($p in Get-TestProcs) { [void](Stop-TestCopy); if (-not $p.WaitForExit(5000)) { Stop-Process -Id $p.Id -Force } }
  foreach ($p in Get-LegacyProcs) { [void](Stop-TestCopy); Start-Sleep -Seconds 2; Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
  $unins = Join-Path $app 'unins000.exe'
  if (Test-Path -LiteralPath $unins) {
    $u = Start-Process -FilePath $unins -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -PassThru
    $u.WaitForExit()
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ((Test-Path -LiteralPath $unKey) -and $sw.Elapsed.TotalSeconds -lt 30) { Start-Sleep -Milliseconds 100 }
  }
  if (Get-RunValue $T.RunValue) { Remove-ItemProperty -Path $runKey -Name $T.RunValue }
  if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk }
  if (Test-Path -LiteralPath $unKey) { Remove-Item -LiteralPath $unKey -Recurse }
}

# --- Run ----------------------------------------------------------------------------------------------

if (Test-Event $T.QuitEvent) { throw 'A test copy is already running; quit it first' }
if ($Legacy) {
  if (-not (Test-Path -LiteralPath (Join-Path $Legacy 'test_identity.json'))) { throw '-Legacy must be a finished test copy' }
  if (-not $Python -or -not (Test-Path -LiteralPath $Python)) { throw '-Legacy needs -Python <python.exe>' }
}
if (Test-Path -LiteralPath $unKey) { throw "A '$($T.AppName)' install already exists; uninstall it first" }
if (Test-Path -LiteralPath $sb) { Remove-Item -LiteralPath $sb -Recurse -Force }
New-Item -ItemType Directory -Force -Path $sb, $data, $tmp | Out-Null
New-Item -ItemType File -Force -Path $marker | Out-Null

$realBefore = Get-RealState
$savedEnv = @{ TEMP = $env:TEMP; TMP = $env:TMP; SAYLIGHT_DATA_DIR = $env:SAYLIGHT_DATA_DIR }
# Setup, the uninstaller and every copy they start inherit these.
$env:TEMP = $tmp; $env:TMP = $tmp; $env:SAYLIGHT_DATA_DIR = $data
$R = [ordered]@{ setup = (Split-Path $Setup -Leaf); sandbox = $sb }
$failed = $true
try {
  # 1. Fresh install, "Start with Windows" ticked
  $R.install = Invoke-Setup 'install1.log' @('/TASKS="startup"')
  Check 'install: exit code 0' ($R.install.exit -eq 0) $R.install
  $files = @(Get-ChildItem -LiteralPath $app -Recurse -File)
  $R.installed = "$($files.Count) files"
  foreach ($rel in 'Saylight.exe', '_internal\saylight.ico', 'THIRD_PARTY_NOTICES.txt', 'LICENSE.txt', 'licenses', 'unins000.exe') {
    Check "install: $rel present" (Test-Path -LiteralPath (Join-Path $app $rel))
  }
  $exe = Join-Path $app 'Saylight.exe'
  # Never start a release build: it answers to the real quit event and registers the real shortcut.
  $stamp = "$((Get-Item -LiteralPath $exe).VersionInfo.SpecialBuild)".Trim().Trim([char]0)
  Check 'install: the app is a test-copy build' ($stamp -eq $TestBuildStamp) $stamp
  if ($stamp -ne $TestBuildStamp) { throw "Refusing to start $exe, which isn't a build of a test copy" }
  Check 'install: Run value points at the exe' ((Get-RunValue $T.RunValue) -eq "`"$exe`"") (Get-RunValue $T.RunValue)
  $sc = Get-Shortcut
  Check 'install: Start menu shortcut' ($sc -and $sc.target -eq $exe -and $sc.workdir -eq $app -and $sc.aumid -eq $T.AumId) $sc
  $u = Get-ItemProperty -LiteralPath $unKey -ErrorAction SilentlyContinue
  $version = (Get-Item -LiteralPath $exe).VersionInfo.ProductVersion
  Check 'install: Settings > Apps entry' ($u -and $u.DisplayName -eq $T.AppName -and $u.DisplayVersion -eq $version -and
    $u.Publisher -eq 'Cole Craven' -and $u.QuietUninstallString -and $u.InstallLocation.TrimEnd('\') -eq $app) $(if ($u) {
    [ordered]@{ name = $u.DisplayName; version = $u.DisplayVersion; publisher = $u.Publisher; icon = $u.DisplayIcon; size_kb = $u.EstimatedSize } })

  # 2. First start of the installed copy
  $first = Start-TestCopy
  $R.first_start_s = $first.started_s
  Check 'first start: running with the TEST quit event' ($null -ne $first.started_s) $first.started_s

  # 3. Upgrade while running, "Start with Windows" unticked
  $R.upgrade_running = Invoke-Setup 'install2.log' @('/MERGETASKS="!startup"')
  Check 'upgrade while running: exit code 0' ($R.upgrade_running.exit -eq 0) $R.upgrade_running
  Check 'upgrade while running: the old copy quit' ($first.process.WaitForExit(5000))
  $log2 = Get-Content -LiteralPath "$sb\install2.log" -Raw
  Check 'upgrade while running: closed through the quit event' ($log2 -match 'Running copy gone: 1')
  Check 'upgrade while running: Run value removed' ($null -eq (Get-RunValue $T.RunValue))
  $s = Wait-Started $first.process.ExitTime
  $procs = @(Get-TestProcs)
  Check 'upgrade while running: started again after the silent upgrade' ($null -ne $s -and $procs.Count -eq 1 -and $procs[0].Id -ne $first.process.Id) $s
  if ($procs.Count -gt 0) { Check 'upgrade while running: the restarted copy quits' (Stop-AndWait $procs[0]) }

  # 3b. The in-app update: Saylight has already quit, and asks Setup to start it again
  $since = Get-Date
  $R.upgrade_relaunch = Invoke-Setup 'install2b.log' @('/MERGETASKS="!startup"', '/RELAUNCH=1')
  Check 'update with /RELAUNCH=1: exit code 0' ($R.upgrade_relaunch.exit -eq 0) $R.upgrade_relaunch
  $s = Wait-Started $since
  $procs = @(Get-TestProcs)
  Check 'update with /RELAUNCH=1: Saylight started again' ($null -ne $s -and $procs.Count -eq 1) $s
  if ($procs.Count -gt 0) { Check 'update with /RELAUNCH=1: it quits' (Stop-AndWait $procs[0]) }

  # 3c. The in-app update without the flag: Setup runs from Saylight's download folder after Saylight quit,
  #     with only the arguments the app passes. The remembered task is "not startup", and Settings > General has
  #     since turned "Start with Windows" on; the update must leave that alone.
  $dl = Join-Path $tmp "$($T.CachePrefix)update_lifecycle"
  New-Item -ItemType Directory -Force -Path $dl | Out-Null
  $dlSetup = Join-Path $dl (Split-Path $Setup -Leaf)
  Copy-Item -LiteralPath $Setup -Destination $dlSetup
  New-ItemProperty -Path $runKey -Name $T.RunValue -Value "`"$exe`"" -PropertyType String -Force | Out-Null
  $since = Get-Date
  $R.upgrade_from_download = Invoke-Setup 'install2c.log' @() $dlSetup
  Check 'update from the download folder: exit code 0' ($R.upgrade_from_download.exit -eq 0) $R.upgrade_from_download
  Check 'update from the download folder: Start with Windows kept as the app set it' ((Get-RunValue $T.RunValue) -eq "`"$exe`"") (Get-RunValue $T.RunValue)
  Remove-ItemProperty -Path $runKey -Name $T.RunValue -ErrorAction SilentlyContinue
  $s = Wait-Started $since
  $procs = @(Get-TestProcs)
  Check 'update from the download folder: Saylight started again' ($null -ne $s -and $procs.Count -eq 1) $s
  # The restarted copy deletes Setup's download folder (in the background, just after it starts).
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ((Test-Path -LiteralPath $dl) -and $sw.Elapsed.TotalSeconds -lt 10) { Start-Sleep -Milliseconds 100 }
  Check 'update from the download folder: Saylight deleted it at start' (-not (Test-Path -LiteralPath $dl))
  if ($procs.Count -gt 0) { Check 'update from the download folder: it quits' (Stop-AndWait $procs[0]) }
  if (Test-Path -LiteralPath $dl) { Remove-Item -LiteralPath $dl -Recurse -Force }

  # 4. Upgrade over a simulated 1.0 source install
  New-Item -ItemType Directory -Force -Path "$app\saylight\ui", "$app\__pycache__" | Out-Null
  'print("1.0")' | Set-Content -Encoding ascii "$app\saylight_app.py"
  'edge-tts' | Set-Content -Encoding ascii "$app\requirements.txt"
  '' | Set-Content -Encoding ascii "$app\saylight\engine.py"
  '' | Set-Content -Encoding ascii "$app\saylight\ui\panel.py"
  'log' | Set-Content -Encoding ascii "$app\saylight.log"
  '{"version": 1, "speed": 1.25, "hotkeys": {"read": ""}, "media_keys": false, "panel": {"visible": false}}' |
    Set-Content -Encoding ascii "$app\saylight_settings.json"
  $R.upgrade_from_10 = Invoke-Setup 'install3.log' @('/TASKS="startup"')
  Check 'upgrade from 1.0: exit code 0' ($R.upgrade_from_10.exit -eq 0) $R.upgrade_from_10
  Check 'upgrade from 1.0: Setup started nothing (it was not running)' (@(Get-TestProcs).Count -eq 0)
  $gone = -not ((Test-Path "$app\saylight_app.py") -or (Test-Path "$app\requirements.txt") -or (Test-Path "$app\saylight") -or
    (Test-Path "$app\__pycache__") -or (Test-Path "$app\saylight.log"))
  Check 'upgrade from 1.0: Python files removed' $gone
  Check 'upgrade from 1.0: settings file kept' (Test-Path "$app\saylight_settings.json")
  Check 'upgrade from 1.0: Run value back' ((Get-RunValue $T.RunValue) -eq "`"$exe`"")
  $third = Start-TestCopy
  Check 'upgrade from 1.0: starts' ($null -ne $third.started_s) $third.started_s
  # The app moves 1.0's settings into the data folder on its first start.
  $moved = Join-Path $data 'saylight_settings.json'
  $kept = $(if (Test-Path -LiteralPath $moved) { (Get-Content -LiteralPath $moved -Raw | ConvertFrom-Json).speed })
  Check 'upgrade from 1.0: settings moved into the data folder' ((-not (Test-Path "$app\saylight_settings.json")) -and $kept -eq 1.25) $kept

  # 5. Uninstall while running
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $un = Start-Process -FilePath (Join-Path $app 'unins000.exe') -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$sb\uninstall.log`"" -PassThru
  $un.WaitForExit()
  # The uninstaller hands over to a copy of itself in TEMP and returns at once; wait for its work to finish.
  while (((Test-Path -LiteralPath $exe) -or (Test-Path -LiteralPath $unKey)) -and $sw.Elapsed.TotalSeconds -lt 30) { Start-Sleep -Milliseconds 100 }
  Start-Sleep -Seconds 1
  $R.uninstall_s = [math]::Round($sw.Elapsed.TotalSeconds, 1)
  Check 'uninstall: the running copy quit' ($third.process.HasExited -and -not (Test-Event $T.QuitEvent))
  $left = @(if (Test-Path -LiteralPath $app) { Get-ChildItem -LiteralPath $app -Recurse -Force | ForEach-Object { $_.FullName.Substring($app.Length + 1) } })
  Check 'uninstall: install folder emptied' ($left.Count -eq 0) $left
  Check 'uninstall: a silent uninstall keeps the settings' (Test-Path -LiteralPath $moved)
  Check 'uninstall: Run value removed' ($null -eq (Get-RunValue $T.RunValue))
  Check 'uninstall: shortcut removed' (-not (Test-Path -LiteralPath $lnk))
  Check 'uninstall: Settings > Apps entry removed' (-not (Test-Path -LiteralPath $unKey))
  Check 'uninstall: no test copy running' (@(Get-TestProcs).Count -eq 0)
  $caches = @(Get-ChildItem -LiteralPath $tmp -Directory -Filter "$($T.CachePrefix)*" -ErrorAction SilentlyContinue)
  Check 'uninstall: no audio cache folders left' ($caches.Count -eq 0) $caches.Count

  if ($Legacy) {
    # 6. Upgrade over a 1.1 source install, the way 1.1 was set up: its files in the install folder, its own
    #    --install for the Run value, the Start menu entry and the first start, and its settings next to it.
    Remove-Item -Path (Join-Path $data '*') -Recurse -Force
    New-Item -ItemType Directory -Force -Path $app | Out-Null
    Copy-Item -LiteralPath (Join-Path $Legacy 'saylight_app.py') -Destination $app
    Copy-Item -LiteralPath (Join-Path $Legacy 'saylight') -Destination $app -Recurse
    "edge-tts>=7.3.1,<8`r`nPySide6-Essentials>=6.12,<7" | Set-Content -Encoding ascii "$app\requirements.txt"
    '{"version": 1, "voice": "en-GB-SoniaNeural", "speed": 1.25, "volume": 0, "media_keys": false, "panel": {"visible": false}, "hotkeys": {"read": "", "toggle_panel": "", "pause_resume": "", "compose": ""}}' |
      Set-Content -Encoding ascii "$app\saylight_settings.json"
    $pythonw = Join-Path (Split-Path $Python) 'pythonw.exe'
    # 1.1 keeps its data next to saylight_app.py; a test copy's prologue takes that folder from SAYLIGHT_DATA_DIR.
    $env:SAYLIGHT_DATA_DIR = $app
    & $Python "$app\saylight_app.py" --install | Out-Null
    $env:SAYLIGHT_DATA_DIR = $data
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Event $T.QuitEvent) -and $sw.Elapsed.TotalSeconds -lt 30) { Start-Sleep -Milliseconds 100 }
    $legacyProcs = @(Get-LegacyProcs)
    Check '1.1 source install: running under pythonw' ((Test-Event $T.QuitEvent) -and $legacyProcs.Count -eq 1) $legacyProcs.Count
    Check '1.1 source install: Run value starts pythonw' ((Get-RunValue $T.RunValue) -eq "`"$pythonw`" `"$app\saylight_app.py`"") (Get-RunValue $T.RunValue)
    $sc = Get-Shortcut
    Check '1.1 source install: Start menu shortcut starts pythonw' ($sc -and $sc.target -eq $pythonw) $sc
    $since = Get-Date
    $R.upgrade_from_11 = Invoke-Setup 'install4.log' @('/TASKS="startup"')
    Check 'upgrade from 1.1: exit code 0' ($R.upgrade_from_11.exit -eq 0) $R.upgrade_from_11
    $log4 = Get-Content -LiteralPath "$sb\install4.log" -Raw
    Check 'upgrade from 1.1: closed through the quit event' ($log4 -match 'Running copy gone: 1')
    Check 'upgrade from 1.1: the pythonw copy quit' (@(Get-LegacyProcs).Count -eq 0)
    $gone = -not ((Test-Path "$app\saylight_app.py") -or (Test-Path "$app\requirements.txt") -or (Test-Path "$app\saylight") -or
      (Test-Path "$app\__pycache__") -or (Test-Path "$app\saylight.log") -or (Test-Path "$app\saylight.ico"))
    Check 'upgrade from 1.1: Python files removed' $gone
    Check 'upgrade from 1.1: Run value starts the exe' ((Get-RunValue $T.RunValue) -eq "`"$exe`"") (Get-RunValue $T.RunValue)
    $sc = Get-Shortcut
    Check 'upgrade from 1.1: Start menu shortcut starts the exe' ($sc -and $sc.target -eq $exe -and $sc.workdir -eq $app -and $sc.aumid -eq $T.AumId) $sc
    $s = Wait-Started $since
    $procs = @(Get-TestProcs)
    Check 'upgrade from 1.1: started again' ($null -ne $s -and $procs.Count -eq 1) $s
    $migrated = $(if (Test-Path -LiteralPath $moved) { Get-Content -LiteralPath $moved -Raw | ConvertFrom-Json })
    Check 'upgrade from 1.1: settings moved into the data folder' ((-not (Test-Path "$app\saylight_settings.json")) -and
      $migrated -and $migrated.speed -eq 1.25 -and "$($migrated.voice)".EndsWith('en-GB-SoniaNeural')) $(if ($migrated) { [ordered]@{ speed = $migrated.speed; voice = $migrated.voice } })
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $un = Start-Process -FilePath (Join-Path $app 'unins000.exe') -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$sb\uninstall2.log`"" -PassThru
    $un.WaitForExit()
    while (((Test-Path -LiteralPath $exe) -or (Test-Path -LiteralPath $unKey)) -and $sw.Elapsed.TotalSeconds -lt 30) { Start-Sleep -Milliseconds 100 }
    Start-Sleep -Seconds 1
    $left = @(if (Test-Path -LiteralPath $app) { Get-ChildItem -LiteralPath $app -Recurse -Force | ForEach-Object { $_.FullName.Substring($app.Length + 1) } })
    Check 'uninstall after 1.1: install folder emptied' ($left.Count -eq 0) $left
    Check 'uninstall after 1.1: Run value, shortcut and Apps entry removed' (($null -eq (Get-RunValue $T.RunValue)) -and
      -not (Test-Path -LiteralPath $lnk) -and -not (Test-Path -LiteralPath $unKey))
    Check 'uninstall after 1.1: nothing running' ((@(Get-TestProcs).Count + @(Get-LegacyProcs).Count) -eq 0)
  }
  $failed = $false
}
catch {
  Check 'script ran to the end' $false ($_.Exception.Message)
}
finally {
  $env:TEMP = $savedEnv.TEMP; $env:TMP = $savedEnv.TMP; $env:SAYLIGHT_DATA_DIR = $savedEnv.SAYLIGHT_DATA_DIR
  Remove-TestLeftovers
  $realAfter = Get-RealState
  $same = ($realBefore | ConvertTo-Json -Compress) -eq ($realAfter | ConvertTo-Json -Compress)
  Check 'real Saylight untouched' $same ([ordered]@{ before = $realBefore; after = $realAfter })
  Check 'no test data in the real data folders' (-not (Test-Path (Join-Path $env:LOCALAPPDATA $T.DataDirName)))
  if (-not $KeepSandbox) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ((Test-Path -LiteralPath $sb) -and $sw.Elapsed.TotalSeconds -lt 15) {
      Remove-Item -LiteralPath $sb -Recurse -Force -ErrorAction SilentlyContinue
      if (Test-Path -LiteralPath $sb) { Start-Sleep -Milliseconds 300 }
    }
    Check 'sandbox removed' (-not (Test-Path -LiteralPath $sb))
  }
}

$R.checks = $checks
$bad = @($checks | Where-Object { -not $_.ok })
$R.passed = ($bad.Count -eq 0 -and -not $failed)
$json = $R | ConvertTo-Json -Depth 6
if ($Report) { $json | Out-File -LiteralPath $Report -Encoding utf8 }
foreach ($c in $checks) { if ($c.ok) { Write-Output "  ok  $($c.check)" } else { Write-Output "  !!  $($c.check)  $($c.detail | ConvertTo-Json -Compress -Depth 4)" } }
Write-Output ("lifecycle: {0} of {1} checks passed" -f ($checks.Count - $bad.Count), $checks.Count)
if ($R.passed) { exit 0 } else { exit 1 }
