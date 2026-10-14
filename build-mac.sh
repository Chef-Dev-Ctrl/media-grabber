#!/usr/bin/env bash
# Builds "dist/Media Grabber.app" and installer/MediaGrabber-<version>-mac-<arch>.dmg.
# Runs on macOS only (GitHub Actions does this for every push; see .github/workflows/build.yml).
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' app.py)
ARCH=$(uname -m)
echo "Building Media Grabber $VERSION for macOS ($ARCH)"

python3 -m pip install --quiet --upgrade pyinstaller pywebview flask yt-dlp gallery-dl qrcode imageio-ffmpeg

# ffmpeg: imageio-ffmpeg ships a static macOS build for this machine's architecture
mkdir -p vendor installer
if [ ! -x vendor/ffmpeg ]; then
    cp "$(python3 -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')" vendor/ffmpeg
    chmod +x vendor/ffmpeg
fi
vendor/ffmpeg -hide_banner -version | head -1
if vendor/ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264; then
    echo "ffmpeg has libx264 (Instagram compatibility conversion will work)"
else
    echo "::warning::bundled ffmpeg has no libx264; Instagram videos will be kept in their original format"
fi

rm -rf build "dist/Media Grabber.app"
python3 -m PyInstaller --noconfirm --clean --windowed \
    --name "Media Grabber" \
    --icon icon.icns \
    --osx-bundle-identifier com.pulselabs.mediagrabber \
    --add-data "static:static" \
    --add-binary "vendor/ffmpeg:bin" \
    --collect-submodules gallery_dl \
    --collect-submodules yt_dlp \
    --exclude-module tkinter \
    app.py

APP="dist/Media Grabber.app"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist"

# never ship personal data
if find "$APP" \( -name cookies.txt -o -name history.json -o -name settings.json \) | grep -q .; then
    echo "Personal data found in build" >&2; exit 1
fi

# ad-hoc signature so Apple Silicon will run it (not notarized: see README for first-launch steps)
codesign --force --deep --sign - "$APP"

# drag-to-Applications disk image
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="installer/MediaGrabber-$VERSION-mac-$ARCH.dmg"
rm -f "$DMG"
hdiutil create -volname "Media Grabber" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
ls -lh "$DMG"
