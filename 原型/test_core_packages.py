"""Stdlib source isolation checks; real wheel builds are an explicit verifier."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import verify_core_packages


class StandaloneCorePackageTests(unittest.TestCase):
    def test_stale_mirror_refuses_before_build_or_subprocess(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temporary:
            root = Path(temporary)
            tool = root / '工具/sync_directory_identity.py'
            tool.parent.mkdir()
            shutil.copyfile(verify_core_packages.ROOT / '工具/sync_directory_identity.py', tool)
            for name, data in (('rabbit_runtime/directory_identity.py', b'CANONICAL = True\n'),
                               ('rabbit_content/_directory_identity.py', b'STALE = True\n')):
                target = root / '原型' / name
                target.parent.mkdir(parents=True)
                target.write_bytes(data)
            with patch.object(verify_core_packages.subprocess, 'run',
                              side_effect=AssertionError('Build should not start')):
                with self.assertRaisesRegex(ValueError, 'mirror differs'):
                    verify_core_packages.verify(root)

    def test_each_source_runs_without_the_other_package_or_build_tools(self):
        with patch.object(verify_core_packages, 'version',
                          side_effect=AssertionError('Default suite must not query build tools')):
            result = verify_core_packages.verify(source_only=True)
        self.assertTrue(result['passed'])
        self.assertEqual(result['mode'], 'source_only')
        self.assertTrue(result['source_unchanged'])
        self.assertTrue(result['temporary_data_removed'])
        for package in ('rabbit_content', 'rabbit_runtime'):
            with self.subTest(package=package):
                self.assertEqual(set(result['packages'][package]), {'version', 'source'})
                consumer = result['packages'][package]['source']
                self.assertTrue(consumer['passed'])
                self.assertTrue(consumer['checks']['all_public_imports'])
                self.assertEqual((consumer['native_processes'], consumer['network_requests']), (0, 0))


if __name__ == '__main__':
    unittest.main()
