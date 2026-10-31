#!/bin/sh
# Run after collecting third-party packages, in the ImageBuilder directory.
set -eu
rm -f .nikki-packages
if [ -f repositories ]; then
    selection_file=shell/apk-custom-packages.sh
elif [ -f repositories.conf ]; then
    selection_file=shell/custom-packages.sh
else
    echo '[Nikki] ERROR: No ImageBuilder repository configuration' >&2
    exit 1
fi
CUSTOM_PACKAGES=
. "./$selection_file"
case " $CUSTOM_PACKAGES " in
    *' nikki '*|*' nikki='*|*' luci-app-nikki '*|*' luci-app-nikki='*|*' luci-i18n-nikki-'*) ;;
    *) echo '[Nikki] Not selected; no changes'; exit 0 ;;
esac
command -v python3 >/dev/null || {
    echo '[Nikki] ERROR: Python3 is required (included in official ImageBuilder images)' >&2
    exit 1
}
python3 shell/update-nikki.py "$CUSTOM_PACKAGES"
