# Builds dist\MediaGrabber\ (PyInstaller) and installer\MediaGrabber-Setup-<version>.exe (Inno Setup).
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$version = (Select-String -Path app.py -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Building Media Grabber $version" -ForegroundColor Cyan

# ffmpeg: the smaller "essentials" build is enough (ffprobe is optional for yt-dlp)
New-Item -ItemType Directory -Force vendor | Out-Null
if (-not (Test-Path vendor\ffmpeg.exe)) {
    $ff = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg.Essentials*\*\bin\ffmpeg.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ff) { throw "ffmpeg essentials not found. Run: winget install Gyan.FFmpeg.Essentials" }
    Copy-Item $ff.FullName vendor\ffmpeg.exe
}

python -m pip install --quiet --upgrade pyinstaller pywebview flask yt-dlp gallery-dl qrcode
if ($LASTEXITCODE) { throw "pip install failed" }

python -m PyInstaller --noconfirm --clean --windowed `
    --name MediaGrabber `
    --icon icon.ico `
    --add-data "static;static" `
    --add-binary "vendor\ffmpeg.exe;bin" `
    --collect-submodules gallery_dl `
    --collect-submodules yt_dlp `
    --exclude-module tkinter `
    app.py
if ($LASTEXITCODE) { throw "PyInstaller failed" }

# never ship personal data
$leaks = Get-ChildItem dist\MediaGrabber -Recurse -Include cookies.txt, history.json, settings.json
if ($leaks) { throw "Personal data found in build: $($leaks.FullName -join ', ')" }

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup not found. Run: winget install JRSoftware.InnoSetup" }
& $iscc /Q "/DAppVersion=$version" installer.iss
if ($LASTEXITCODE) { throw "Inno Setup failed" }

Get-Item "installer\MediaGrabber-Setup-$version.exe" | Select-Object Name, @{n = 'MB'; e = { [math]::Round($_.Length / 1MB, 1) } }
