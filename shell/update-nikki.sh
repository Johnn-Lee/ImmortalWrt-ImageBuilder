#!/bin/sh
# Select the latest stable Nikki release for the ImageBuilder target architecture.
# Run after collecting third-party packages and before "make image".
set -eu

log() { printf '[Nikki] %s\n' "$*"; }
fail() { printf '[Nikki] ERROR: %s\n' "$*" >&2; exit 1; }

# Read the same selected-package list as the ImageBuilder entrypoint.
if [ -f repositories ]; then
    format=apk
    repo_file=repositories
    selection_file=shell/apk-custom-packages.sh
elif [ -f repositories.conf ]; then
    format=ipk
    repo_file=repositories.conf
    selection_file=shell/custom-packages.sh
else
    fail 'No ImageBuilder package repository configuration found'
fi

[ -f "$selection_file" ] || fail "Missing $selection_file"
CUSTOM_PACKAGES=
. "./$selection_file"
case " $CUSTOM_PACKAGES " in
    *nikki*) ;;
    *) log 'Nikki not selected; no changes'; exit 0 ;;
esac

# Releases are built for 24.10, 25.12 and SNAPSHOT; older releases remain untouched.
if grep -Eq '/24[.]10([.]|/|-)' "$repo_file"; then
    series=openwrt-24.10
elif grep -Eq '/25[.]12([.]|/|-)' "$repo_file"; then
    series=openwrt-25.12
elif grep -Eq '/snapshots?/' "$repo_file"; then
    series=SNAPSHOT
else
    log "No supported upstream release for this ImageBuilder ($repo_file); keep existing Nikki packages"
    exit 0
fi

[ -f .config ] || fail 'Missing ImageBuilder .config (cannot determine target architecture)'
arch=$(sed -n 's/^CONFIG_TARGET_ARCH_PACKAGES="\([^"]*\)".*/\1/p' .config | head -n 1)
[ -n "$arch" ] || fail 'Missing CONFIG_TARGET_ARCH_PACKAGES in .config'
case "$arch" in
    *[!a-zA-Z0-9_-]*) fail "Unexpected target architecture: $arch" ;;
esac

api='https://api.github.com/repos/nikkinikki-org/OpenWrt-nikki/releases/latest'
release=$(curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 60 \
    -H 'Accept: application/vnd.github+json' -H 'User-Agent: ImmortalWrt-ImageBuilder' "$api") || fail 'Unable to query upstream Nikki releases'
tag=$(printf '%s\n' "$release" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)
[ -n "$tag" ] || fail 'The GitHub release has no tag_name'
case "$tag" in *[!a-zA-Z0-9._-]*) fail "Unsafe release tag: $tag" ;; esac

asset="nikki_${arch}-${series}.tar.gz"
printf '%s\n' "$release" | grep -F '"name": "'"$asset"'"' >/dev/null || \
    fail "No official Nikki package for ${arch}/${series} in ${tag}"

# Prefer GitHub's published SHA256 checksum when available.
checksum=$(printf '%s\n' "$release" | awk -v asset="$asset" '
    index($0, "\"name\": \"" asset "\"") { found = 1 }
    found && /"digest": "sha256:/ {
        sub(/.*"digest": "sha256:/, ""); sub(/".*/, ""); print; exit
    }
    found && /"browser_download_url":/ { exit }
')

work=$(mktemp -d) || fail 'Unable to create temporary directory'
trap 'rm -rf "$work"' EXIT
trap 'exit 1' HUP INT TERM
mkdir -p "$work/extracted" packages
url="https://github.com/nikkinikki-org/OpenWrt-nikki/releases/download/${tag}/${asset}"
log "Downloading ${tag}, arch=${arch}, firmware=${series}, packages=${format}"
curl -fL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 300 \
    -o "$work/nikki.tar.gz" "$url" || fail "Unable to download $url"
if [ -n "$checksum" ]; then
    printf '%s  %s\n' "$checksum" "$work/nikki.tar.gz" | sha256sum -c - >/dev/null || \
        fail 'Upstream release SHA256 mismatch'
fi
tar -xzf "$work/nikki.tar.gz" -C "$work/extracted" || fail 'Unable to extract Nikki packages'

# Validate all required packages before touching the old ones.
for package in nikki luci-app-nikki mihomo-meta; do
    found=false
    for file in "$work/extracted/$package"-*."$format" "$work/extracted/$package"_*.$format; do
        if [ -f "$file" ]; then found=true; break; fi
    done
    [ "$found" = true ] || fail "Missing ${package} ${format} package in ${asset}"
done

# Do not modify other plugins or package archives.
for package in nikki luci-app-nikki mihomo-meta luci-i18n-nikki-zh-cn; do
    has_new=false
    for file in "$work/extracted/$package"-*."$format" "$work/extracted/$package"_*.$format; do
        if [ -f "$file" ]; then has_new=true; break; fi
    done
    if [ "$has_new" = true ]; then
        rm -f "packages/$package"-*."$format" "packages/$package"_*.$format
        for file in "$work/extracted/$package"-*."$format" "$work/extracted/$package"_*.$format; do
            [ -f "$file" ] || continue
            cp "$file" packages/ || fail "Unable to copy $(basename "$file")"
            log "Installed $(basename "$file")"
        done
    fi
done
log "Prepared ${tag} for ${arch} (${series}); ready for ImageBuilder"
