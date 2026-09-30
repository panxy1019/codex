from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_history.safeio import PathSafetyError, SourceChangedError
from codex_history.snapshot import SnapshotConfig, build_snapshot
from tests.helpers import (
    relative_regular_files,
    tree_metadata,
    verify_sha256sums,
    write_attachment,
    write_rollout,
)


INCLUDED_ID = "01a09fc2-b66b-7742-b63a-93f58ba7d907"
EXCLUDED_ID = "01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b"
BACKUP_ID = "2026-09-30T010203Z"


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / ".codex"
        self.staging = self.base / "staging"

    def _config(self) -> SnapshotConfig:
        return SnapshotConfig(
            codex_home=self.home,
            staging_parent=self.staging,
            backup_id=BACKUP_ID,
            excluded_thread_ids=frozenset({EXCLUDED_ID}),
            codex_version="0.147.0",
            source_platform="linux",
        )

    def _build_source(self) -> tuple[Path, Path, Path]:
        included_attachment = write_attachment(
            self.home, "included/pasted-text.txt", "included attachment"
        )
        excluded_attachment = write_attachment(
            self.home, "private/pasted-text.txt", "private attachment"
        )
        included_rollout = write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=INCLUDED_ID,
            attachment_paths=(included_attachment,),
        )
        excluded_rollout = write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/29",
            filename_thread_id=EXCLUDED_ID,
            metadata_thread_id=EXCLUDED_ID,
            attachment_paths=(excluded_attachment,),
        )
        attachment_index = self.home / "attachments" / "pasted-text-attachments.json"
        attachment_index.write_text(
            json.dumps({"textExcerptsByPath": {str(excluded_attachment): "private text"}}),
            encoding="utf-8",
        )
        forbidden = {
            "auth.json": "token",
            "config.toml": "setting=true",
            "state_5.sqlite": "database",
            "logs/codex.log": "log",
            "cache/item": "cache",
        }
        for relative, content in forbidden.items():
            path = self.home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return included_rollout, excluded_rollout, included_attachment

    def test_snapshot_contains_only_allowlisted_selected_files_and_checksums(self) -> None:
        included_rollout, excluded_rollout, included_attachment = self._build_source()

        result = build_snapshot(self._config())

        expected = tuple(
            sorted(
                (
                    "SHA256SUMS",
                    "manifest.json",
                    included_rollout.relative_to(self.home).as_posix(),
                    included_attachment.relative_to(self.home).as_posix(),
                )
            )
        )
        self.assertEqual(relative_regular_files(result.root), expected)
        self.assertNotIn(excluded_rollout.name, "\n".join(expected))
        self.assertEqual(
            {record.thread_id for record in result.manifest.rollouts},
            {INCLUDED_ID},
        )
        self.assertEqual(result.manifest.exclusions[0].thread_id, EXCLUDED_ID)
        self.assertEqual(result.manifest.exclusions[0].matched_rollouts, 1)
        self.assertEqual(result.manifest.counts.session_count, 1)
        self.assertEqual(result.manifest.counts.rollout_file_count, 1)
        self.assertEqual(result.manifest.counts.attachment_count, 1)
        self.assertEqual(verify_sha256sums(result.checksums_path, result.root), [])
        manifest_bytes = result.manifest_path.read_bytes()
        self.assertNotIn(str(self.home).encode(), manifest_bytes)
        self.assertNotIn(b"private text", manifest_bytes)

    def test_snapshot_preserves_duplicate_rollouts_and_records_group(self) -> None:
        self._build_source()
        second = write_rollout(
            self.home,
            bucket="archived_sessions",
            relative_parent="",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=INCLUDED_ID,
            stamp="2026-09-28T01-02-03",
        )

        result = build_snapshot(self._config())

        self.assertTrue((result.root / second.relative_to(self.home)).is_file())
        self.assertEqual(result.manifest.counts.session_count, 1)
        self.assertEqual(result.manifest.counts.rollout_file_count, 2)
        self.assertEqual(len(result.manifest.duplicate_thread_ids[INCLUDED_ID]), 2)

    def test_snapshot_fails_when_referenced_attachment_is_missing(self) -> None:
        missing = self.home / "attachments" / "missing" / "pasted-text.txt"
        write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=INCLUDED_ID,
            attachment_paths=(missing,),
        )

        with self.assertRaises(PathSafetyError):
            build_snapshot(self._config())

        self.assertTrue(
            (self.staging / BACKUP_ID / "codex-history" / ".incomplete").is_file()
        )

    def test_snapshot_failure_leaves_codex_home_unchanged(self) -> None:
        self._build_source()
        before = tree_metadata(self.home)

        with mock.patch(
            "codex_history.snapshot.stable_copy",
            side_effect=SourceChangedError("source changed while copying"),
        ):
            with self.assertRaises(SourceChangedError):
                build_snapshot(self._config())

        self.assertEqual(tree_metadata(self.home), before)
        self.assertTrue(
            (self.staging / BACKUP_ID / "codex-history" / ".incomplete").is_file()
        )


if __name__ == "__main__":
    unittest.main()
