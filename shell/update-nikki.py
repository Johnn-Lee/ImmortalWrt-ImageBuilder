#!/usr/bin/env python3
"""Stage one official stable Nikki package set for the current ImageBuilder."""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

API = "https://api.github.com/repos/nikkinikki-org/OpenWrt-nikki/releases/latest"
REQUIRED = {"nikki", "luci-app-nikki", "mihomo-meta"}


def log(message):
    print("[Nikki] " + message, file=sys.stderr)


def related(name):
    return name in REQUIRED | {"mihomo", "mihomo-alpha"} or name.startswith("luci-i18n-nikki-")


def fetch(url, destination):
    subprocess.run([
        "curl", "-fsSL", "--retry", "3", "--retry-delay", "2",
        "--connect-timeout", "15", "--max-time", "300",
        "-H", "Accept: application/vnd.github+json",
        "-H", "User-Agent: ImmortalWrt-ImageBuilder", "-o", str(destination), url,
    ], check=True)


def select_release(release, arch, series):
    tag = release.get("tag_name", "")
    if release.get("prerelease") or release.get("draft") or not re.fullmatch(r"v?[0-9][A-Za-z0-9._-]*", tag):
        raise ValueError("Upstream did not return a stable release")
    name = f"nikki_{arch}-{series}.tar.gz"
    matches = [a for a in release["assets"] if a["name"] == name]
    if len(matches) != 1:
        raise ValueError(f"No unique official package for {arch}/{series} in {tag}")
    asset = matches[0]
    expected = f"https://github.com/nikkinikki-org/OpenWrt-nikki/releases/download/{tag}/{name}"
    if asset["browser_download_url"] != expected:
        raise ValueError("Unexpected upstream download URL")
    digest = asset.get("digest")
    if digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Invalid upstream SHA256 digest")
    return tag, asset


def stage(archive, destination, arch, fmt):
    """Extract only exact files named by the upstream index; never extract paths."""
    with tarfile.open(archive, "r:gz") as tar:
        members = {}
        for member in tar.getmembers():
            name = member.name.removeprefix("./")
            if name in members:
                raise ValueError(f"Duplicate archive member: {name}")
            members[name] = member
        index_member = members.get("index.json")
        if not index_member or not index_member.isfile():
            raise ValueError("Missing official index.json")
        index = json.load(tar.extractfile(index_member))
        if index["architecture"] != arch:
            raise ValueError("Official package index architecture mismatch")
        versions = index["packages"]
        names = REQUIRED | {n for n in versions if n.startswith("luci-i18n-nikki-")}
        if not REQUIRED <= versions.keys():
            raise ValueError("Incomplete Nikki/LuCI/stable Mihomo package set")
        selected = {}
        for name in sorted(names):
            version = versions[name]
            if not re.fullmatch(r"[a-z0-9+.-]+", name) or not re.fullmatch(r"[A-Za-z0-9.+~_-]+", version):
                raise ValueError("Invalid package name/version in official index")
            if fmt == "apk":
                candidates = [f"{name}-{version}.apk"]
            else:
                candidates = [f"{name}_{version}_{a}.ipk" for a in (arch, "all")]
            found = [n for n in candidates if n in members and members[n].isfile()]
            if len(found) != 1:
                raise ValueError(f"Missing or ambiguous {name} {fmt} package")
            filename = found[0]
            with tar.extractfile(members[filename]) as src, (destination / filename).open("wb") as dst:
                shutil.copyfileobj(src, dst)
            selected[name] = (version, filename)
        return selected


def old_package_name(filename):
    if filename.endswith(".ipk"):
        return filename.split("_", 1)[0]
    if filename.endswith(".apk"):
        # APK names use a hyphen before a version beginning with a digit.
        return re.split(r"-(?=[0-9])", filename[:-4], maxsplit=1)[0]
    return ""


def update(selection):
    repo = Path("repositories") if Path("repositories").is_file() else Path("repositories.conf")
    fmt = "apk" if repo.name == "repositories" else "ipk"
    repositories = repo.read_text()
    match = re.search(r"/releases/(24\.10|25\.12)(?:[./-])", repositories)
    if match:
        series = "openwrt-" + match[1]
    elif re.search(r"/snapshots?/", repositories):
        series = "SNAPSHOT"
    else:
        log("SKIP: official Nikki does not support this system; retaining existing packages")
        return
    if (series == "openwrt-24.10" and fmt != "ipk") or (series == "openwrt-25.12" and fmt != "apk"):
        raise ValueError(f"Unsupported package format: {series}/{fmt}")
    config = Path(".config").read_text()
    match = re.search(r'^CONFIG_TARGET_ARCH_PACKAGES="([A-Za-z0-9_-]+)"$', config, re.M)
    if not match:
        raise ValueError("Missing CONFIG_TARGET_ARCH_PACKAGES in ImageBuilder .config")
    arch = match[1]
    tokens = selection.split()
    for token in tokens:
        name = token.split("=", 1)[0]
        if name == "mihomo-alpha" or (name.startswith("-") and name[1:] in REQUIRED | {"mihomo"}):
            raise ValueError(f"Selection conflicts with stable Nikki: {token}")
    with tempfile.TemporaryDirectory(prefix="nikki-") as directory:
        work = Path(directory)
        fetch(API, work / "release.json")
        tag, asset = select_release(json.loads((work / "release.json").read_text()), arch, series)
        log(f"Downloading {tag}, arch={arch}, firmware={series}, packages={fmt}")
        fetch(asset["browser_download_url"], work / "nikki.tar.gz")
        digest = asset.get("digest")
        if digest:
            with (work / "nikki.tar.gz").open("rb") as src:
                actual = hashlib.sha256(src.read()).hexdigest()
            if actual != digest.split(":", 1)[1]:
                raise ValueError("Upstream release SHA256 mismatch")
        else:
            log("Upstream has no SHA256 digest; using official HTTPS asset")
        staged = work / "packages"
        staged.mkdir()
        selected = stage(work / "nikki.tar.gz", staged, arch, fmt)
        for token in tokens:
            name = token.split("=", 1)[0]
            if related(name) and name not in selected and name != "mihomo":
                raise ValueError(f"Selected package is absent from official release: {name}")
        packages = Path("packages")
        packages.mkdir(exist_ok=True)
        # All downloads/validation finish before any old package is touched.
        for file in packages.iterdir():
            if file.is_file() and related(old_package_name(file.name)):
                file.unlink()
        for _, filename in selected.values():
            shutil.copyfile(staged / filename, packages / filename)
            log("Prepared " + filename)
        # Both package managers accept local archive paths. Avoid name=version:
        # 25.12 ImageBuilder's FormatPackages leaks that version to later packages.
        # Explicit files also prevent a feed version replacing this release set.
        resolved = [t for t in tokens if not related(t.split("=", 1)[0])]
        requested = {t.split("=", 1)[0] for t in tokens}
        for name, (version, filename) in sorted(selected.items()):
            if name in REQUIRED or name in requested:
                resolved.append(str((packages / filename).resolve()))
        Path(".nikki-packages").write_text(" ".join(resolved) + "\n")
        log(f"Prepared official {tag}; normal dependencies are resolved by ImageBuilder")


if __name__ == "__main__":
    try:
        update(sys.argv[1])
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError, tarfile.TarError) as error:
        log("ERROR: " + str(error))
        sys.exit(1)
