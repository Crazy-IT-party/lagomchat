#!/bin/bash
# The Mac app of a release, signed with a Developer ID certificate and notarized by Apple, so that a download opens without
# the "unidentified developer" warning.
#
#     tools/sign-mac.sh <the .app> <the zip to make> <notary profile> [<signing identity>]
#
#   <notary profile>    the name the sign-in for Apple's notary service is stored under in the keychain, made once with
#                           xcrun notarytool store-credentials <name> --apple-id <Apple ID> --team-id <team>
#                       (it asks for an app-specific password and keeps it in the keychain, not in a file)
#   <signing identity>  the certificate to sign with, by its name or SHA-1, as `security find-identity -v -p codesigning`
#                       shows it; left out: the keychain's "Developer ID Application" certificate
#
# The certificate, its key and the sign-in stay in the keychain of the one who releases: this script and the repo hold none
# of them. Without a certificate a release is the app as built (signed ad hoc).
set -euo pipefail
if [ $# -lt 3 ] || [ $# -gt 4 ]; then
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
fi
APP=$1 ZIP=$2 NOTARY=$3 SIGN=${4:-Developer ID Application}
HERE=$(cd "$(dirname "$0")" && pwd)
[ -d "$APP" ] || { echo "no such app: $APP" >&2; exit 2; }

# the bundle holds one program: signing the bundle signs it. The hardened runtime and a timestamp are what the notary asks for
codesign --force --options runtime --timestamp --entitlements "$HERE/../desktop/src-tauri/Entitlements.plist" -s "$SIGN" "$APP"
codesign --verify --strict "$APP"

rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY" --wait
xcrun stapler staple "$APP"                      # the notary's ticket goes into the app: it opens with no network too
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"           # the zip again, with the ticket inside
spctl -a -vv "$APP" 2>&1 | grep -E "accepted|rejected|source="
