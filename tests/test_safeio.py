from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_history.safeio import (
    PathSafetyError,
    SourceChangedError,
    stable_copy,
)


class StableCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "allowed"
        self.root.mkdir()
        self.output = Path(self.temp.name) / "staging" / "copy.bin"

    def _source(self, data: bytes = b"source bytes") -> Path:
        source = self.root / "source.bin"
        source.write_bytes(data)
        return source

    def test_stable_copy_is_byte_identical_and_hashes_multiple_chunks(self) -> None:
        data = (b"0123456789abcdef" * 131_073) + b"tail"
        source = self._source(data)

        size, digest = stable_copy(source, self.output, self.root)

        self.assertEqual(size, len(data))
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(self.output.read_bytes(), data)

    def test_stable_copy_detects_source_growth_and_removes_destination(self) -> None:
        source = self._source()
        current = source.stat()
        before = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        after_growth = (current.st_dev, current.st_ino, current.st_size + 1, current.st_mtime_ns + 1)

        with mock.patch(
            "codex_history.safeio._path_identity",
            side_effect=(before, after_growth),
        ):
            with self.assertRaisesRegex(SourceChangedError, "changed while copying"):
                stable_copy(source, self.output, self.root)

        self.assertFalse(self.output.exists())

    def test_stable_copy_detects_inode_or_mtime_replacement(self) -> None:
        source = self._source()
        current = source.stat()
        before = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        changed_identities = (
            (current.st_dev, current.st_ino + 1, current.st_size, current.st_mtime_ns),
            (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns + 1),
        )
        for changed in changed_identities:
            with self.subTest(changed=changed), mock.patch(
                "codex_history.safeio._path_identity",
                side_effect=(before, changed),
            ):
                with self.assertRaises(SourceChangedError):
                    stable_copy(source, self.output, self.root)
                self.assertFalse(self.output.exists())

    def test_stable_copy_rejects_symlink_even_when_target_is_allowed(self) -> None:
        real_file = self._source()
        source = self.root / "linked.bin"
        source.symlink_to(real_file)

        with self.assertRaises(PathSafetyError):
            stable_copy(source, self.output, self.root)

        self.assertFalse(self.output.exists())

    def test_stable_copy_rejects_source_outside_root(self) -> None:
        source = Path(self.temp.name) / "outside.bin"
        source.write_bytes(b"outside")

        with self.assertRaisesRegex(PathSafetyError, "outside allowed root"):
            stable_copy(source, self.output, self.root)

    def test_stable_copy_rejects_special_file(self) -> None:
        source = self.root / "pipe"
        os.mkfifo(source)

        with self.assertRaisesRegex(PathSafetyError, "regular file"):
            stable_copy(source, self.output, self.root)

    def test_stable_copy_never_overwrites_existing_destination(self) -> None:
        source = self._source(b"new")
        self.output.parent.mkdir(parents=True)
        self.output.write_bytes(b"existing")

        with self.assertRaises(FileExistsError):
            stable_copy(source, self.output, self.root)

        self.assertEqual(self.output.read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
