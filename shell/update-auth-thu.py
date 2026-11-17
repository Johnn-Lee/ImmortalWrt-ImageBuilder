#!/usr/bin/env python3
"""Stage the official Auth THU LuCI package and GoAuthing core for ImageBuilder."""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LUCI_API = "https://api.github.com/repos/stevenjoezhang/luci-app-auth-thu/releases/latest"
CORE_API = "https://api.github.com/repos/z4yx/GoAuthing/releases/latest"
LUCI_REPOSITORY = "stevenjoezhang/luci-app-auth-thu"
CORE_REPOSITORY = "z4yx/GoAuthing"
PACKAGE = "luci-app-auth-thu"
SELECTION_FILE = Path(".nikki-packages")  # Existing ImageBuilder build-script handoff.

# GoAuthing publishes no i386, mips64, or big-endian PowerPC Linux binary.
ELF_TARGETS = {
    "x86_64": (2, 1, 62),
    "arm64": (2, 1, 183),
    "arm": (1, 1, 40),
    "armv5": (1, 1, 40),
    "armv6": (1, 1, 40),
    "mipsle": (1, 1, 8),
    "mipsbe": (1, 2, 8),
    "riscv64": (2, 1, 243),
    "loong64": (2, 1, 258),
    "ppc64le": (2, 1, 21),
}


def log(message):
    print("[Auth THU] " + message, file=sys.stderr)


def fetch(url, destination):
    subprocess.run([
        "curl", "-fsSL", "--retry", "3", "--retry-delay", "2",
        "--connect-timeout", "15", "--max-time", "300",
        "-H", "Accept: application/vnd.github+json",
        "-H", "User-Agent: ImmortalWrt-ImageBuilder",
        "-o", str(destination), url,
    ], check=True)


def target_arch(package_arch):
    if package_arch == "x86_64":
        return "x86_64"
    if package_arch.startswith("aarch64_"):
        return "arm64"
    if package_arch.startswith("arm_"):
        if re.search(r"(?:arm926|armv5)", package_arch):
            return "armv5"
        if re.search(r"(?:arm1176|armv6)", package_arch):
            return "armv6"
        return "arm"
    if package_arch.startswith("mipsel_"):
        return "mipsle"
    if package_arch.startswith("mips_"):
        return "mipsbe"
    if package_arch.startswith("riscv64_"):
        return "riscv64"
    if package_arch.startswith("loongarch64_"):
        return "loong64"
    if package_arch.startswith("powerpc64le_"):
        return "ppc64le"
    return None


def select_asset(release, repository, filename_pattern):
    tag = release.get("tag_name", "")
    if (release.get("draft") or release.get("prerelease")
            or not re.fullmatch(r"v?[0-9][A-Za-z0-9._-]*", tag)):
        raise ValueError(f"{repository} did not return a stable release")
    matching = [a for a in release.get("assets", [])
                if re.fullmatch(filename_pattern(tag.removeprefix("v")), a.get("name", ""))]
    if len(matching) != 1:
        raise ValueError(f"No unique official asset in {repository} {tag}")
    asset = matching[0]
    expected = (f"https://github.com/{repository}/releases/download/"
                f"{tag}/{asset['name']}")
    if asset.get("browser_download_url") != expected:
        raise ValueError(f"Unexpected download URL in {repository}")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""):
        raise ValueError(f"Missing or invalid SHA256 in {repository} release")
    return tag, asset


def verify_sha256(path, asset):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != asset["digest"].split(":", 1)[1]:
        raise ValueError("SHA256 mismatch for " + asset["name"])


def verify_elf(path, arch):
    with path.open("rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise ValueError("GoAuthing core is not an ELF binary")
    bits, endian, machine = ELF_TARGETS[arch]
    if (header[4], header[5]) != (bits, endian):
        raise ValueError(f"GoAuthing core ELF class/endianness does not match {arch}")
    actual_machine = int.from_bytes(header[18:20], "little" if endian == 1 else "big")
    if actual_machine != machine:
        raise ValueError(f"GoAuthing core ELF architecture does not match {arch}")


def sign_apk(work, package_path):
    """Sign the upstream APK with a per-build key, as update-nikki.py does."""
    apk = Path("staging_dir/host/bin/apk").resolve()
    if not apk.is_file():
        raise FileNotFoundError(f"ImageBuilder apk-tools missing: {apk}")
    key = work / "auth-thu-private.pem"
    trust = work / "trust"
    trust.mkdir()
    pub = trust / "auth-thu-build.pem"
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey",
                    "-noout", "-out", str(key)], check=True)
    subprocess.run(["openssl", "ec", "-in", str(key), "-pubout", "-out", str(pub)], check=True)
    subprocess.run([str(apk), "adbsign", "--allow-untrusted", "--sign-key",
                    str(key), str(package_path)], check=True)
    subprocess.run([str(apk), "--keys-dir", str(trust), "verify", str(package_path)], check=True)
    return pub


def update(selection):
    tokens = selection.split()
    requested = any(t.split("=", 1)[0] == PACKAGE for t in tokens)
    if not requested:
        log("Not selected; no changes")
        return

    resolved = [t for t in tokens if t.split("=", 1)[0] not in (PACKAGE, "-" + PACKAGE)]
    if "-" + PACKAGE in tokens:
        log("Explicitly disabled; omitting Auth THU")
        SELECTION_FILE.write_text(" ".join(resolved) + "\n")
        return

    repo = Path("repositories") if Path("repositories").is_file() else Path("repositories.conf")
    if not repo.is_file():
        raise ValueError("No ImageBuilder repositories/repositories.conf")
    fmt = "apk" if repo.name == "repositories" else "ipk"
    conf = Path(".config").read_text()
    match = re.search(r'^CONFIG_TARGET_ARCH_PACKAGES="([A-Za-z0-9_-]+)"$', conf, re.M)
    if not match:
        raise ValueError("Missing CONFIG_TARGET_ARCH_PACKAGES in ImageBuilder .config")
    package_arch = match[1]
    core_arch = target_arch(package_arch)
    if core_arch is None:
        log(f"SKIP: GoAuthing does not publish a Linux core for {package_arch}")
        SELECTION_FILE.write_text(" ".join(resolved) + "\n")
        return

    with tempfile.TemporaryDirectory(prefix="auth-thu-") as dirname:
        work = Path(dirname)
        fetch(LUCI_API, work / "luci.json")
        luci = json.loads((work / "luci.json").read_text())
        if fmt == "apk":
            luci_pattern = lambda v: rf"luci-app-auth-thu-{re.escape(v)}-r[0-9]+\.apk"
        else:
            luci_pattern = lambda v: rf"luci-app-auth-thu_{re.escape(v)}_all\.ipk"
        luci_tag, luci_asset = select_asset(luci, LUCI_REPOSITORY, luci_pattern)

        fetch(CORE_API, work / "core.json")
        core_release = json.loads((work / "core.json").read_text())
        core_tag, core_asset = select_asset(
            core_release, CORE_REPOSITORY,
            lambda _: rf"auth-thu\.linux\.{re.escape(core_arch)}")

        package_path = work / luci_asset["name"]
        core_path = work / core_asset["name"]
        log(f"Downloading LuCI {luci_tag} ({fmt}) and GoAuthing {core_tag} ({core_arch})")
        fetch(luci_asset["browser_download_url"], package_path)
        fetch(core_asset["browser_download_url"], core_path)
        verify_sha256(package_path, luci_asset)
        verify_sha256(core_path, core_asset)
        verify_elf(core_path, core_arch)
        public_key = sign_apk(work, package_path) if fmt == "apk" else None

        # Nothing in the build tree is touched until download, validation and
        # APK signing have all succeeded.
        packages = Path("packages")
        packages.mkdir(exist_ok=True)
        binary_dest = Path("files/usr/bin/goauthing")
        binary_dest.parent.mkdir(parents=True, exist_ok=True)
        if public_key:
            Path("keys").mkdir(exist_ok=True)
            shutil.copyfile(public_key, "keys/auth-thu-build.pem")
        for old in packages.iterdir():
            if old.is_file() and re.fullmatch(r"luci-app-auth-thu[-_].*\.(?:ipk|apk)", old.name):
                old.unlink()
        shutil.copyfile(package_path, packages / package_path.name)
        staged_core = binary_dest.with_name(".goauthing.tmp")
        shutil.copyfile(core_path, staged_core)
        staged_core.chmod(0o755)
        staged_core.replace(binary_dest)
        resolved.append(str((packages / package_path.name).resolve()))
        SELECTION_FILE.write_text(" ".join(resolved) + "\n")
        log(f"Prepared official LuCI {luci_tag} and GoAuthing {core_tag} for {package_arch}")


if __name__ == "__main__":
    try:
        update(sys.argv[1])
    except (OSError, ValueError, KeyError, TypeError,
            subprocess.CalledProcessError) as exc:
        log("ERROR: " + str(exc))
        sys.exit(1)
