"""Offline regression tests for official Auth THU ImageBuilder integration."""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("auth_thu", ROOT / "shell/update-auth-thu.py")
auth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auth)


def elf(arch):
    bits, endian, machine = auth.ELF_TARGETS[arch]
    hdr = bytearray(64)
    hdr[:4] = b"\x7fELF"
    hdr[4:6] = bytes([bits, endian])
    hdr[18:20] = machine.to_bytes(2, "little" if endian == 1 else "big")
    return bytes(hdr) + b"test core payload"


class AuthTHUTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.tmp.name)
        Path("repositories").write_text("https://downloads.immortalwrt.org/releases/25.12.2/packages/\n")
        Path(".config").write_text('CONFIG_TARGET_ARCH_PACKAGES="aarch64_generic"\n')
        Path("packages").mkdir()
        self.luci = b"official LuCI archive payload"
        self.core = elf("arm64")

    def tearDown(self):
        os.chdir(self.previous)
        self.tmp.cleanup()

    def release(self, repo, tag, filename, data):
        return {"tag_name": tag, "draft": False, "prerelease": False,
                "assets": [{"name": filename,
                            "browser_download_url": f"https://github.com/{repo}/releases/download/{tag}/{filename}",
                            "digest": "sha256:" + hashlib.sha256(data).hexdigest()}]}

    def run_update(self, selection="mihomo-meta luci-app-auth-thu other", fmt="apk", arch="aarch64_generic",
                   corrupt=None, invalid_core=False):
        Path(".config").write_text(f'CONFIG_TARGET_ARCH_PACKAGES="{arch}"\n')
        for f in ("repositories", "repositories.conf"):
            Path(f).unlink(missing_ok=True)
        Path("repositories" if fmt == "apk" else "repositories.conf").write_text(
            "https://downloads.immortalwrt.org/releases/25.12.2/packages/\n" if fmt == "apk"
            else "https://downloads.immortalwrt.org/releases/24.10.6/packages/\n")
        lu_name = "luci-app-auth-thu-1.0.1-r1.apk" if fmt == "apk" else "luci-app-auth-thu_1.0.1_all.ipk"
        arch_name = auth.target_arch(arch)
        core_name = f"auth-thu.linux.{arch_name}"
        core_bytes = elf(arch_name) if arch_name else self.core
        if invalid_core:
            core_bytes = b"not an ELF binary"
        luci_release = self.release(auth.LUCI_REPOSITORY, "v1.0.1", lu_name, self.luci)
        core_release = self.release(auth.CORE_REPOSITORY, "v2.4.0", core_name, core_bytes)
        blobs = {luci_release["assets"][0]["browser_download_url"]: self.luci,
                 core_release["assets"][0]["browser_download_url"]: core_bytes}
        def fake_fetch(url, dest):
            if url == auth.LUCI_API:
                dest.write_text(json.dumps(luci_release))
            elif url == auth.CORE_API:
                dest.write_text(json.dumps(core_release))
            else:
                dest.write_bytes(blobs[url] + (b"corruption" if corrupt == url else b""))
        key = Path("test-key.pem"); key.write_text("test public key")
        with patch.object(auth, "fetch", side_effect=fake_fetch) as fetch, \
                patch.object(auth, "sign_apk", return_value=key) as sign:
            auth.update(selection)
        return fetch, sign, lu_name, core_name

    def test_apk_complete_and_preserves_nikki_selection(self):
        Path("packages/luci-app-auth-thu-old.apk").write_bytes(b"old")
        Path("packages/mihomo-meta-1.0.apk").write_bytes(b"mihomo")
        fetch, sign, pkg, _ = self.run_update(
            selection="/some/path/nikki.apk luci-app-auth-thu luci-app-argon-config")
        self.assertEqual(fetch.call_count, 4)
        sign.assert_called_once()
        self.assertFalse(Path("packages/luci-app-auth-thu-old.apk").exists())
        self.assertEqual(Path("packages", pkg).read_bytes(), self.luci)
        self.assertEqual(Path("packages/mihomo-meta-1.0.apk").read_bytes(), b"mihomo")
        tokens = Path(".nikki-packages").read_text().split()
        self.assertIn("/some/path/nikki.apk", tokens)
        self.assertIn("luci-app-argon-config", tokens)
        self.assertNotIn("luci-app-auth-thu", tokens)
        self.assertTrue(any(t.endswith("/packages/" + pkg) for t in tokens))
        self.assertEqual(Path("files/usr/bin/goauthing").read_bytes(), self.core)
        self.assertEqual(Path("files/usr/bin/goauthing").stat().st_mode & 0o111, 0o111)
        self.assertEqual(Path("keys/auth-thu-build.pem").read_text(), "test public key")

    def test_ipk_needs_no_apk_signing(self):
        fetch, sign, pkg, _ = self.run_update(fmt="ipk", arch="x86_64")
        self.assertEqual(fetch.call_count, 4)
        sign.assert_not_called()
        self.assertEqual(Path("packages", pkg).read_bytes(), self.luci)
        self.assertEqual(Path("files/usr/bin/goauthing").read_bytes(), elf("x86_64"))
        self.assertFalse(Path("keys").exists())

    def test_unsupported_arch_skips_entire_pair(self):
        with patch.object(auth, "fetch") as fetch:
            Path(".config").write_text('CONFIG_TARGET_ARCH_PACKAGES="i386_pentium4"\n')
            auth.update("/some/path/nikki.apk luci-app-auth-thu unrelated")
            fetch.assert_not_called()
        self.assertEqual(Path(".nikki-packages").read_text().strip(), "/some/path/nikki.apk unrelated")
        self.assertFalse(Path("files/usr/bin/goauthing").exists())

    def test_explicit_disable(self):
        with patch.object(auth, "fetch") as fetch:
            auth.update("luci-app-auth-thu unrelated -luci-app-auth-thu")
            fetch.assert_not_called()
        self.assertEqual(Path(".nikki-packages").read_text().strip(), "unrelated")

    def test_not_requested_is_no_op(self):
        with patch.object(auth, "fetch") as fetch:
            auth.update("nikki other")
            fetch.assert_not_called()
        self.assertFalse(Path(".nikki-packages").exists())

    def test_validation_failures_leave_existing_files_intact(self):
        old_core = Path("files/usr/bin/goauthing")
        old_core.parent.mkdir(parents=True)
        old_core.write_bytes(b"existing user core")
        Path("packages/luci-app-auth-thu-old.apk").write_bytes(b"existing package")
        self.assertEqual(len(list(Path("packages").iterdir())), 1)
        with self.assertRaisesRegex(ValueError, "not an ELF"):
            self.run_update(invalid_core=True)
        self.assertEqual(old_core.read_bytes(), b"existing user core")
        self.assertEqual(Path("packages/luci-app-auth-thu-old.apk").read_bytes(), b"existing package")
        self.assertFalse(Path(".nikki-packages").exists())

    def test_sha256_failure_stops_before_mutation(self):
        with patch.object(auth, "verify_sha256", side_effect=ValueError("SHA256 mismatch")):
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                self.run_update()
        self.assertFalse(Path("files/usr/bin/goauthing").exists())
        self.assertFalse(Path(".nikki-packages").exists())

    def test_select_asset_checks_source_digest_and_release(self):
        asset = self.release(auth.LUCI_REPOSITORY, "v1.0.1", "luci-app-auth-thu_1.0.1_all.ipk", self.luci)
        patt = lambda v: rf"luci-app-auth-thu_{v}_all\.ipk"
        self.assertEqual(auth.select_asset(asset, auth.LUCI_REPOSITORY, patt)[0], "v1.0.1")
        asset["assets"][0]["digest"] = None
        with self.assertRaisesRegex(ValueError, "SHA256"):
            auth.select_asset(asset, auth.LUCI_REPOSITORY, patt)
        asset["draft"] = True
        with self.assertRaisesRegex(ValueError, "stable"):
            auth.select_asset(asset, auth.LUCI_REPOSITORY, patt)

    def test_architecture_mapping(self):
        cases = {"aarch64_generic": "arm64", "aarch64_cortex-a53": "arm64",
                 "x86_64": "x86_64", "arm_cortex-a7_neon-vfpv4": "arm",
                 "arm_arm926ej-s": "armv5", "arm_arm1176jzf-s": "armv6",
                 "mipsel_24kc": "mipsle", "mips_24kc": "mipsbe",
                 "riscv64_generic": "riscv64", "loongarch64_generic": "loong64",
                 "mips64_octeonplus": None, "i386_pentium4": None,
                 "powerpc64": None}
        for arch, expected in cases.items():
            self.assertEqual(auth.target_arch(arch), expected, arch)

    def test_signing_process_uses_strict_verify(self):
        Path("staging_dir/host/bin").mkdir(parents=True)
        Path("staging_dir/host/bin/apk").write_text("mock apk")
        path = Path("package.apk"); path.write_bytes(b"fake apk")
        work = Path("work"); work.mkdir()
        with patch.object(auth.subprocess, "run") as run:
            pub = auth.sign_apk(work, path)
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertIn("adbsign", cmds[2])
        self.assertIn("--allow-untrusted", cmds[2])
        self.assertIn("verify", cmds[3])
        self.assertNotIn("--allow-untrusted", cmds[3])
        self.assertEqual(pub.name, "auth-thu-build.pem")

    def test_shell_wrapper_uses_previous_nikki_resolutions(self):
        Path("shell").mkdir()
        shutil.copyfile(ROOT / "shell/update-auth-thu.sh", "shell/update-auth-thu.sh")
        Path(".nikki-packages").write_text("nikki.apk luci-app-auth-thu unrelated\n")
        Path("bin").mkdir()
        stub = Path("bin/python3")
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$2" > selection.txt\n')
        stub.chmod(0o755)
        env = dict(os.environ, PATH=str(Path("bin").resolve()) + ":" + os.environ["PATH"],
                   CUSTOM_PACKAGES="should-not-use-this")
        subprocess.run(["/bin/sh", "shell/update-auth-thu.sh"], env=env, check=True)
        self.assertEqual(Path("selection.txt").read_text().strip(), "nikki.apk luci-app-auth-thu unrelated")


if __name__ == "__main__":
    unittest.main()
