# Installs or updates Retrace for this Windows user. Run it through Install.bat.
#
# It needs no administrator rights and leaves any other Python alone. Everything goes in one folder under
# AppData, except your rounds, which stay in <home>\Retrace and survive updates and uninstalling. Your settings
# survive updates. It fetches a pinned, checksum-verified copy of uv, which downloads a private Python.
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Retrace"),
    [string]$DesktopDir = [Environment]::GetFolderPath("Desktop"),
    [switch]$NoOpen,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$UvVersion = "0.13.1"
$UvSha256 = "3b2661a463a5baea7f5076ea2b25ab71d77f36a6c0d809e5350651792f80a672"
$PythonVersion = "3.12"
$AppFiles = @("retrace.py", "make_index.py", "make_viewer.py", "build_round.py", "course_data.py", "align_lidar.py",
              "fetch_osm.py", "lidar_trees.py", "osm_surfaces.py", "viewer.template.html", "requirements.lock",
              "README.md", "install.ps1", "Uninstall.bat")

$Source = $PSScriptRoot
$Venv = Join-Path $InstallDir ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$PythonW = Join-Path $Venv "Scripts\pythonw.exe"
$Retrace = Join-Path $InstallDir "retrace.py"
$Shortcut = Join-Path $DesktopDir "Retrace Rounds.lnk"

function Step($text) { Write-Host "  $text" }

function Run($exe, [string[]]$arguments, $doing) {
    $ErrorActionPreference = "Continue"   # a warning on stderr isn't a failure; the exit code decides
    $output = & $exe @arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host ($output | Out-String)
        throw "Couldn't $doing."
    }
}

# Stops a running watcher, so its files can be replaced or removed.
function Stop-Watcher {
    $ErrorActionPreference = "Continue"
    if (Test-Path $Python) {
        if (Test-Path $Retrace) { & $Python $Retrace stop *> $null }
        $global:LASTEXITCODE = 0
    }
}

if ($Uninstall) {
    Write-Host ""
    Write-Host "Removing Retrace..."
    Stop-Watcher
    $ErrorActionPreference = "Continue"
    if (Test-Path $Python) { & $Python $Retrace startup off *> $null }
    if (Test-Path $Shortcut) { Remove-Item -LiteralPath $Shortcut -Force }
    Write-Host ""
    Write-Host "Retrace is uninstalled. Your rounds are still in $(Join-Path $HOME 'Retrace');"
    Write-Host "delete that folder too if you don't want them."
    # this script is inside the folder being removed, so that happens just after it exits
    Start-Process cmd.exe -WindowStyle Hidden -ArgumentList "/c timeout /t 3 /nobreak >nul & rd /s /q `"$InstallDir`""
    exit 0
}

try {
    Write-Host ""
    Write-Host "Setting up Retrace. The first time takes a few minutes."
    Write-Host ""
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Stop-Watcher

    Step "Copying Retrace..."
    if ($Source -ne (Resolve-Path $InstallDir).Path) {
        foreach ($f in $AppFiles) { Copy-Item -LiteralPath (Join-Path $Source $f) -Destination $InstallDir -Force }
    }

    $Tools = Join-Path $InstallDir "tools"
    $Uv = Join-Path $Tools "uv.exe"
    if (-not (Test-Path $Uv) -or ((& $Uv --version) -notmatch [regex]::Escape($UvVersion))) {
        Step "Downloading the setup tool..."
        New-Item -ItemType Directory -Force -Path $Tools | Out-Null
        $zip = Join-Path $Tools "uv.zip"
        Invoke-WebRequest -UseBasicParsing -OutFile $zip -Uri "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
        if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $UvSha256.ToUpper()) {
            Remove-Item -LiteralPath $zip -Force
            throw "The setup tool didn't match its expected checksum, so it wasn't used. Please try again."
        }
        Expand-Archive -LiteralPath $zip -DestinationPath $Tools -Force
        Remove-Item -LiteralPath $zip -Force
    }

    # uv keeps everything inside the install folder, so nothing is left behind and other Pythons are untouched
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $InstallDir "python"
    $env:UV_CACHE_DIR = Join-Path $InstallDir "cache"
    $env:UV_NO_CONFIG = "1"

    Step "Installing Python and what Retrace needs (a few hundred MB)..."
    if (-not (Test-Path $Python)) {
        Run $Uv @("venv", "--quiet", "--python", $PythonVersion, "--python-preference", "only-managed", $Venv) "install Python"
    }
    Run $Uv @("pip", "sync", "--quiet", "--python", $Python, (Join-Path $InstallDir "requirements.lock")) "install Retrace's Python packages"
    # only tidying up, so it can't fail the install; uv copes with the long paths in there
    $ErrorActionPreference = "Continue"
    & $Uv cache clean --quiet *> $null
    $global:LASTEXITCODE = 0
    $ErrorActionPreference = "Stop"

    Step "Looking for your GSPro courses (this can take up to a minute)..."
    $ErrorActionPreference = "Continue"
    & $Python $Retrace find-courses 2>&1 | ForEach-Object { Step "  $_" }
    if ($LASTEXITCODE -eq 3) {
        # retrace.py's exit code for a settings file it can't use; it has just said what's wrong
        $SettingsProblem = $true
        throw "Your settings file, $(Join-Path $InstallDir 'config.json'), has a mistake in it (see above). Fix it in Notepad, or delete it, then run Install.bat again."
    }
    while ($LASTEXITCODE -ne 0) {
        # the only question setup ever asks; pressing Enter skips it, and with no one there to answer it's skipped too
        Write-Host ""
        Write-Host "  GSPro shows your Courses folder under Settings > Game > Course Folder."
        $answer = $null
        try { $answer = Read-Host "  Paste that folder here, or press Enter to set it later" } catch { }
        if (-not $answer -or -not $answer.Trim().Trim('"')) { break }
        & $Python $Retrace find-courses $answer.Trim().Trim('"') 2>&1 | ForEach-Object { Step "  $_" }
    }
    $global:LASTEXITCODE = 0
    $ErrorActionPreference = "Stop"

    Step "Starting Retrace, now and whenever you sign in..."
    Run $Python @($Retrace, "startup", "on") "set Retrace to start at sign-in"
    Run $Python @($Retrace, "index") "create your rounds page"
    Start-Process -FilePath $PythonW -ArgumentList "`"$Retrace`" watch" -WorkingDirectory $InstallDir

    Step "Adding the Retrace Rounds shortcut to your desktop..."
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Shortcut)
    $link.TargetPath = $PythonW
    $link.Arguments = "`"$Retrace`" open"
    $link.WorkingDirectory = $InstallDir
    $link.Description = "See your Retrace rounds"
    $link.Save()

    Write-Host ""
    Write-Host "All done. Retrace is now running in the background."
    Write-Host ""
    & $Python $Retrace check
    $global:LASTEXITCODE = 0
    Write-Host ""
    Write-Host "Play a round in GSPro, then double-click Retrace Rounds on your desktop to replay it."
    Write-Host "The first round on each course also downloads that course's terrain, which can take several minutes."
    if (-not $NoOpen) { Start-Process -FilePath $PythonW -ArgumentList "`"$Retrace`" open" -WorkingDirectory $InstallDir }
    exit 0
}
catch {
    Write-Host ""
    Write-Host "Setup didn't finish: $($_.Exception.Message)" -ForegroundColor Red
    if (-not $SettingsProblem) { Write-Host "Check your internet connection and run Install.bat again. It's safe to run more than once." }
    exit 1
}
