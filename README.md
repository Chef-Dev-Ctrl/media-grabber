# Media Grabber

Desktop app for downloading YouTube and Instagram videos, audio and photos (yt-dlp + gallery-dl + ffmpeg,
Flask backend, pywebview window). Windows and macOS. Other devices can use it through a shared PC
(Settings > Office network).

## Downloads

Every push to `main` builds all platforms on GitHub Actions; grab them from the run's **Artifacts**.
Tag a version to publish a release:

```bash
git tag v1.0.1 && git push origin v1.0.1
```

| File | For |
|---|---|
| `MediaGrabber-Setup-<ver>.exe` | Windows 10/11 |
| `MediaGrabber-<ver>-mac-arm64.dmg` | Macs with Apple M-series chips |
| `MediaGrabber-<ver>-mac-x86_64.dmg` | Intel Macs |

Install and first-launch steps for users are in [`installer/READ ME FIRST.txt`](installer/READ%20ME%20FIRST.txt).
The apps are not code-signed/notarized, so Windows SmartScreen and macOS Gatekeeper ask once.

## Develop

```bash
pip install -r requirements.txt
python app.py            # browser UI at http://127.0.0.1:5177
python app.py --window   # native window, like the packaged app
```

Needs ffmpeg on PATH (Windows: `winget install Gyan.FFmpeg.Essentials`). Test runs can use a throwaway
data folder: `MG_DATA_DIR=/tmp/mg python app.py`.

## Build locally

- Windows: `powershell -ExecutionPolicy Bypass -File build.ps1` (needs Inno Setup)
- macOS: `bash build-mac.sh`

Bump `VERSION` in `app.py` before a release.

## Where data lives

Per user, never in the app folder: logins (`cookies.txt`), library history, settings, the updated
download engine, and a log.

- Windows: `%APPDATA%\MediaGrabber`
- macOS: `~/Library/Application Support/MediaGrabber`
