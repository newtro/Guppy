#!/usr/bin/env bash
# Build GuppyPet.app and install it to ~/Applications (a stable path keeps the macOS mic permission).
#   pet/build.sh [--login]   --login also starts it at login (LaunchAgent ai.guppy.pet)
set -euo pipefail
cd "$(dirname "$0")"
APP="build/GuppyPet.app"
rm -rf build && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
xcrun swiftc -O -parse-as-library -o "$APP/Contents/MacOS/GuppyPet" GuppyPet.swift -framework AppKit -framework WebKit 2>&1 \
  | grep -v "warning: 'main' attribute" || true
[ -x "$APP/Contents/MacOS/GuppyPet" ] || { echo "build failed" >&2; exit 1; }
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>ai.guppy.pet</string>
  <key>CFBundleName</key><string>GuppyPet</string>
  <key>CFBundleExecutable</key><string>GuppyPet</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Guppy listens to the Admiral through the microphone.</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
codesign --force --sign - "$APP" >/dev/null
mkdir -p ~/Applications && rm -rf ~/Applications/GuppyPet.app && cp -R "$APP" ~/Applications/
echo "Installed ~/Applications/GuppyPet.app"

if [ "${1:-}" = "--login" ]; then
  PLIST="$HOME/Library/LaunchAgents/ai.guppy.pet.plist"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.guppy.pet</string>
  <key>ProgramArguments</key><array><string>$HOME/Applications/GuppyPet.app/Contents/MacOS/GuppyPet</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
PLIST
  launchctl bootout "gui/$(id -u)/ai.guppy.pet" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "GuppyPet starts at login (quit from its menu to stop until next login)."
fi
