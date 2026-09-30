from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_history.archive import (
    ArchiveError,
    create_tar_zst,
    require_archive_tools,
    split_archive,
)
from codex_history.safeio import sha256_file


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.snapshot_root = self.base / "codex-history"
        (self.snapshot_root / "sessions").mkdir(parents=True)
        (self.snapshot_root / "sessions" / "rollout.jsonl").write_bytes(b"rollout\n")
        (self.snapshot_root / "manifest.json").write_bytes(b'{"schema_version":1}\n')
        (self.snapshot_root / "SHA256SUMS").write_bytes(b"checksums\n")

    def test_create_tar_zst_is_deterministic_for_same_snapshot(self) -> None:
        first = create_tar_zst(self.snapshot_root, self.base / "first.tar.zst")
        second = create_tar_zst(self.snapshot_root, self.base / "second.tar.zst")

        self.assertEqual(sha256_file(first), sha256_file(second))
        self.assertGreater(first.stat().st_size, 0)

    def test_split_archive_parts_reassemble_to_exact_original_bytes(self) -> None:
        archive = self.base / "backup.tar.zst"
        original = b"0123456789abcdefghijkl"
        archive.write_bytes(original)

        parts = split_archive(archive, max_part_bytes=16)

        self.assertEqual(
            [part.name for part in parts],
            ["backup.tar.zst.part001", "backup.tar.zst.part002"],
        )
        self.assertEqual(b"".join(part.read_bytes() for part in parts), original)

    def test_split_archive_keeps_single_asset_at_threshold(self) -> None:
        archive = self.base / "backup.tar.zst"
        archive.write_bytes(b"1234567890abcdef")

        self.assertEqual(split_archive(archive, max_part_bytes=16), (archive,))
        self.assertEqual(list(self.base.glob("*.part*")), [])

    def test_missing_archive_tools_fail_before_work(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": ""}):
            with self.assertRaisesRegex(ArchiveError, "required command"):
                require_archive_tools()

    def test_failed_compressor_removes_partial_archive(self) -> None:
        fake_bin = self.base / "fake-bin"
        fake_bin.mkdir()
        fake_zstd = fake_bin / "zstd"
        fake_zstd.write_text("#!/bin/sh\n/usr/bin/cat >/dev/null\nexit 7\n")
        fake_zstd.chmod(0o755)
        output = self.base / "failed.tar.zst"

        with mock.patch.dict(
            os.environ,
            {"PATH": f"{fake_bin}:/usr/bin:/bin"},
        ):
            with self.assertRaisesRegex(ArchiveError, "zstd failed"):
                create_tar_zst(self.snapshot_root, output)

        self.assertFalse(output.exists())
        self.assertEqual(list(self.base.glob(".*failed.tar.zst.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
