"""Regression tests for the Nikki updater (no network or ImageBuilder needed)."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('nikki', Path(__file__).resolve().parents[1] / 'shell/update-nikki.py')
nikki = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nikki)


class NikkiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.tmp.name)
        Path('packages').mkdir()
        Path('.config').write_text('CONFIG_TARGET_ARCH_PACKAGES="aarch64_generic"\n')
        self.configure('apk', '25.12.2')
        self.versions = {'nikki': '2026.10.10-r1', 'luci-app-nikki': '1.26.2-r1',
                         'mihomo-meta': '1.19.32', 'mihomo-alpha': '2026.08.16',
                         'luci-i18n-nikki-zh-cn': '26.283~abc'}

    def tearDown(self):
        os.chdir(self.previous)
        self.tmp.cleanup()

    def configure(self, fmt, version):
        for file in ('repositories', 'repositories.conf'):
            Path(file).unlink(missing_ok=True)
        Path('repositories' if fmt == 'apk' else 'repositories.conf').write_text(
            f'https://downloads.immortalwrt.org/releases/{version}/packages/\n')

    def release(self, arch='aarch64_generic', series='openwrt-25.12'):
        name = f'nikki_{arch}-{series}.tar.gz'
        return {'tag_name': 'v1.26.2', 'prerelease': False, 'draft': False,
                'assets': [{'name': name, 'browser_download_url':
                    f'https://github.com/nikkinikki-org/OpenWrt-nikki/releases/download/v1.26.2/{name}'}]}

    def archive(self, fmt='apk', arch='aarch64_generic', missing=None, package_arch=None):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w:gz') as tar:
            contents = {'index.json': json.dumps({'architecture': arch, 'packages': self.versions}).encode()}
            for name, version in self.versions.items():
                if name == missing:
                    continue
                filename = f'{name}-{version}.apk' if fmt == 'apk' else f'{name}_{version}_{package_arch or arch}.ipk'
                contents[filename] = b'official-package'
            for name, content in contents.items():
                member = tarfile.TarInfo('./' + name)
                member.size = len(content)
                tar.addfile(member, io.BytesIO(content))
        return out.getvalue()

    def run_update(self, fmt='apk', release=None, archive=None, selection='luci-i18n-nikki-zh-cn unrelated'):
        def fetch(url, target):
            target.write_bytes(json.dumps(release or self.release(), separators=(',', ':')).encode()
                               if url == nikki.API else (archive or self.archive(fmt)))
        with patch.object(nikki, 'fetch', side_effect=fetch):
            nikki.update(selection)

    def test_compact_and_pretty_json(self):
        for indent in (None, 2):
            release = json.loads(json.dumps(self.release(), indent=indent))
            self.assertEqual(nikki.select_release(release, 'aarch64_generic', 'openwrt-25.12')[0], 'v1.26.2')

    def test_apk_exact_cleanup_and_pinning(self):
        for name in ('nikki-2025-r1.apk', 'luci-app-nikki-1.0.apk', 'mihomo-meta-1.0.apk',
                     'mihomo-alpha-2025.apk', 'luci-i18n-nikki-zh-cn-1.apk', 'nikki-helper-1.apk', 'other-1.apk'):
            Path('packages', name).write_bytes(b'old')
        self.run_update()
        selected = Path('.nikki-packages').read_text().split()
        self.assertIn('mihomo-meta=1.19.32', selected)
        self.assertIn('unrelated', selected)
        self.assertNotIn('luci-i18n-nikki-zh-cn', selected)
        self.assertEqual(Path('packages/nikki-helper-1.apk').read_bytes(), b'old')
        self.assertEqual(Path('packages/other-1.apk').read_bytes(), b'old')
        self.assertFalse(Path('packages/mihomo-alpha-2025.apk').exists())
        self.assertEqual(len(list(Path('packages').glob('nikki-[0-9]*.apk'))), 1)

    def test_ipk_local_files_and_all_architecture(self):
        self.configure('ipk', '24.10.4')
        self.run_update('ipk', self.release(series='openwrt-24.10'))
        selected = Path('.nikki-packages').read_text().split()
        self.assertTrue(any(t.endswith('mihomo-meta_1.19.32_aarch64_generic.ipk') and Path(t).is_file() for t in selected))
        dest = Path('all'); dest.mkdir()
        Path('all.tar.gz').write_bytes(self.archive('ipk', package_arch='all'))
        result = nikki.stage('all.tar.gz', dest, 'aarch64_generic', 'ipk')
        self.assertEqual(result['nikki'][1], 'nikki_2026.10.10-r1_all.ipk')

    def test_failure_does_not_touch_old_packages(self):
        old = Path('packages/nikki-2025.apk'); old.write_bytes(b'old')
        for archive in (self.archive(missing='mihomo-meta'), self.archive(arch='x86_64')):
            with self.assertRaises(ValueError):
                self.run_update(archive=archive)
            self.assertEqual(old.read_bytes(), b'old')
            self.assertFalse(Path('.nikki-packages').exists())

    def test_checksum_failure(self):
        release = self.release(); release['assets'][0]['digest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
            self.run_update(release=release)
        self.assertFalse(Path('.nikki-packages').exists())

    def test_unsupported_system_keeps_existing(self):
        self.configure('ipk', '23.05.6')
        with patch.object(nikki, 'fetch') as fetch:
            nikki.update('nikki')
            fetch.assert_not_called()
        self.assertFalse(Path('.nikki-packages').exists())

    @unittest.skipUnless(Path('/bin/sh').exists(), 'requires POSIX shell')
    def test_shell_uses_actual_build_selection_and_clears_stale_state(self):
        Path('shell').mkdir()
        Path('shell/apk-custom-packages.sh').write_text('CUSTOM_PACKAGES="nikki"\n')
        wrapper = Path(SPEC.origin).with_suffix('.sh')
        shutil.copyfile(wrapper, 'shell/update-nikki.sh')
        Path('bin').mkdir()
        stub = Path('bin/python3')
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$2" > .nikki-packages\n')
        stub.chmod(0o755)
        env = dict(os.environ, PATH=str(Path('bin').resolve()) + ':' + os.environ['PATH'])
        env['CUSTOM_PACKAGES'] = 'luci-i18n-nikki-zh-cn luci-app-amlogic'
        subprocess.run(['/bin/sh', 'shell/update-nikki.sh'], env=env, check=True)
        self.assertEqual(Path('.nikki-packages').read_text().strip(), env['CUSTOM_PACKAGES'])
        # An explicitly empty/disabled selection must not fall back to defaults.
        env['CUSTOM_PACKAGES'] = '-nikki -luci-i18n-nikki-zh-cn unrelated'
        subprocess.run(['/bin/sh', 'shell/update-nikki.sh'], env=env, check=True)
        self.assertFalse(Path('.nikki-packages').exists())
        env.pop('CUSTOM_PACKAGES')
        subprocess.run(['/bin/sh', 'shell/update-nikki.sh'], env=env, check=True)
        self.assertEqual(Path('.nikki-packages').read_text().strip(), 'nikki')

    def test_format_and_selection_conflicts(self):
        for selection in ('nikki mihomo-alpha', 'nikki -mihomo-meta', 'nikki -luci-app-nikki'):
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                self.run_update(selection=selection)
        self.configure('ipk', '25.12.2')
        with self.assertRaisesRegex(ValueError, 'format'):
            self.run_update()

    def test_release_validation_and_architectures(self):
        for arch in ('aarch64_generic', 'aarch64_cortex-a53', 'aarch64_cortex-a72', 'x86_64'):
            self.assertEqual(nikki.select_release(self.release(arch), arch, 'openwrt-25.12')[0], 'v1.26.2')
        with self.assertRaisesRegex(ValueError, 'No unique'):
            nikki.select_release(self.release(), 'unsupported', 'openwrt-25.12')
        release = self.release(); release['prerelease'] = True
        with self.assertRaisesRegex(ValueError, 'stable'):
            nikki.select_release(release, 'aarch64_generic', 'openwrt-25.12')


if __name__ == '__main__':
    unittest.main()
