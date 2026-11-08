"""Source-only checks of the controlled mirror; no application imports."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    'rabbit_identity_sync_under_test', Path(__file__).with_name('sync_directory_identity.py'))
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class DirectoryIdentityMirrorTests(unittest.TestCase):
    def test_repository_mirror_is_exact(self):
        self.assertTrue(sync.check()['equal'])

    def test_missing_stale_and_partial_exports_are_rejected_then_sync_uses_source_bytes(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temporary:
            root = Path(temporary)
            source, target = root / sync.CANONICAL, root / sync.MIRROR
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            source.write_bytes(b'CANONICAL = "invented"\n')
            with self.assertRaises(ValueError):
                sync.check(root)
            target.write_bytes(b'STALE = True\n')
            before = target.read_bytes()
            with self.assertRaises(ValueError):
                sync.check(root)
            self.assertEqual(target.read_bytes(), before)
            for payloads in ({sync.CANONICAL: source.read_bytes()},
                             {sync.MIRROR: before},
                             {sync.CANONICAL: source.read_bytes(), sync.MIRROR: before}):
                with self.assertRaises(ValueError):
                    sync.check_payloads(payloads)
            self.assertTrue(sync.sync(root)['equal'])
            self.assertEqual(source.read_bytes(), target.read_bytes())
            sync.check_payloads({})  # Old archives did not carry this primitive.

    def test_symlink_is_never_followed_or_replaced(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temporary:
            root = Path(temporary)
            source, target = root / sync.CANONICAL, root / sync.MIRROR
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            source.write_bytes(b'CANONICAL = True\n')
            target.symlink_to(source)
            for operation in (sync.check, sync.sync):
                with self.assertRaises(ValueError):
                    operation(root)
            self.assertTrue(target.is_symlink())


if __name__ == '__main__':
    unittest.main()
