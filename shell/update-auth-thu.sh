#!/bin/sh
# Run after update-nikki.sh, preserving its resolved local-package selections.
set -eu
if [ -f .nikki-packages ]; then
    selection=$(cat .nikki-packages)
else
    selection=${CUSTOM_PACKAGES-}
fi
case " $selection " in
    *' luci-app-auth-thu '*|*' luci-app-auth-thu='*) ;;
    *) echo '[Auth THU] Not selected; no changes'; exit 0 ;;
esac
command -v python3 >/dev/null || {
    echo '[Auth THU] ERROR: Python3 is required inside ImageBuilder' >&2
    exit 1
}
python3 shell/update-auth-thu.py "$selection"
