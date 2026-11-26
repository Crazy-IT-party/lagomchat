#!/bin/bash
# LagomChat for iPhone and iPad: the Xcode project made from project.yml, then built.
#
#     ./build.sh                        compiles for a device, unsigned: does it build
#     ./build.sh testflight <team id>   an archive signed by Xcode for <team id>, sent to App Store Connect (TestFlight)
#
# Needs Xcode and xcodegen (brew install xcodegen). For testflight: the Apple ID of the team added in Xcode - Settings -
# Accounts, and the app made once in App Store Connect with the bundle ID game.lagom.chat. The version is the Mac app's
# (desktop/src-tauri/tauri.conf.json); the build number is the time, so a later build is always a higher one.
# The team's id is a parameter: nothing of the one who builds is kept in the repo.
set -euo pipefail
cd "$(dirname "$0")"
SELF=$(basename "$0")
MODE=${1:-check} TEAM=${2:-}
VER=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' ../desktop/src-tauri/tauri.conf.json | head -1)
[ -n "$VER" ] || { echo "no version in ../desktop/src-tauri/tauri.conf.json" >&2; exit 1; }
OUT=build
mkdir -p "$OUT"
xcodegen generate --quiet
# xcodebuild says a great deal: its words go to a file, a failure shows the errors of it and stops here
xb() { xcodebuild "$@" > "$OUT/xcodebuild.log" 2>&1 || { { grep -E "error[: ]" "$OUT/xcodebuild.log" | head -20 >&2 || true; }; echo "failed: see ios/$OUT/xcodebuild.log" >&2; exit 1; }; }

case "$MODE" in
  check)
    xb -project LagomChat.xcodeproj -scheme LagomChat -configuration Release -destination 'generic/platform=iOS' \
      -derivedDataPath "$OUT/dd" CODE_SIGNING_ALLOWED=NO MARKETING_VERSION="$VER" build
    ls -d "$OUT/dd/Build/Products/Release-iphoneos/LagomChat.app"
    ;;
  testflight)
    [ -n "$TEAM" ] || { sed -n '2,10p' "$SELF" | sed 's/^# \{0,1\}//' >&2; exit 2; }
    NUM=$(date -u +%y%m%d%H%M)
    rm -rf "$OUT/LagomChat.xcarchive" "$OUT/export"
    xb -project LagomChat.xcodeproj -scheme LagomChat -configuration Release -destination 'generic/platform=iOS' \
      -archivePath "$OUT/LagomChat.xcarchive" -derivedDataPath "$OUT/dd" -allowProvisioningUpdates \
      DEVELOPMENT_TEAM="$TEAM" MARKETING_VERSION="$VER" CURRENT_PROJECT_VERSION="$NUM" archive
    cat > "$OUT/export.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>app-store-connect</string>
  <key>destination</key><string>upload</string>
  <key>teamID</key><string>$TEAM</string>
  <key>signingStyle</key><string>automatic</string>
</dict></plist>
EOF
    xb -exportArchive -archivePath "$OUT/LagomChat.xcarchive" -exportOptionsPlist "$OUT/export.plist" \
      -exportPath "$OUT/export" -allowProvisioningUpdates
    echo "LagomChat $VER ($NUM) sent: it shows in App Store Connect - TestFlight after Apple has processed it"
    ;;
  *)
    sed -n '2,10p' "$SELF" | sed 's/^# \{0,1\}//' >&2; exit 2
    ;;
esac
