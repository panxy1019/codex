from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_history.archive import ArchiveError
from codex_history.export import build_assets, main
from codex_history.safeio import sha256_file
from codex_history.snapshot import SnapshotConfig
from tests.helpers import write_attachment, write_rollout


INCLUDED_ID = "01a09fc2-b66b-7742-b63a-93f58ba7d907"
EXCLUDED_ID = "01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b"
BACKUP_ID = "2026-09-30T010203Z"
SECRET_SENTINEL = "DO-NOT-PRINT-CONVERSATION-CONTENT"


def parse_outer_checksums(path: Path) -> dict[str, str]:
    return {
        filename: digest
        for digest, filename in (
            line.split("  ", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    }


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / ".codex"
        self.output = self.base / "output"
        attachment = write_attachment(
            self.home,
            "included/pasted-text.txt",
            SECRET_SENTINEL,
        )
        write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=INCLUDED_ID,
            attachment_paths=(attachment,),
        )
        write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/29",
            filename_thread_id=EXCLUDED_ID,
            metadata_thread_id=EXCLUDED_ID,
        )

    def _config(self, split_threshold: int = 1_932_735_283) -> SnapshotConfig:
        return SnapshotConfig(
            codex_home=self.home,
            staging_parent=self.base / "staging",
            backup_id=BACKUP_ID,
            excluded_thread_ids=frozenset({EXCLUDED_ID}),
            codex_version="0.147.0",
            source_platform="linux",
            split_threshold_bytes=split_threshold,
        )

    def test_export_asset_names_outer_checksum_and_manifest_match(self) -> None:
        assets = build_assets(self._config(), self.output)

        self.assertEqual(
            assets.archive_paths[0].name,
            f"codex-history-{BACKUP_ID}.tar.zst",
        )
        checksums = parse_outer_checksums(assets.checksum_path)
        self.assertEqual(
            checksums,
            {assets.archive_paths[0].name: sha256_file(assets.archive_paths[0])},
        )
        manifest = json.loads(assets.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["backup_id"], BACKUP_ID)
        self.assertEqual(manifest["source_root"], "$CODEX_HOME")
        self.assertEqual(manifest["exclusions"][0]["thread_id"], EXCLUDED_ID)
        self.assertNotIn(str(self.home), assets.manifest_path.read_text(encoding="utf-8"))

    def test_split_assets_each_have_outer_checksum(self) -> None:
        assets = build_assets(self._config(split_threshold=64), self.output)

        self.assertGreater(len(assets.archive_paths), 1)
        checksums = parse_outer_checksums(assets.checksum_path)
        self.assertEqual(
            checksums,
            {path.name: sha256_file(path) for path in assets.archive_paths},
        )

    def test_archive_failure_preserves_staging_for_diagnosis(self) -> None:
        config = self._config()
        with mock.patch(
            "codex_history.export.create_tar_zst",
            side_effect=ArchiveError("compression failed"),
        ):
            with self.assertRaisesRegex(ArchiveError, "compression failed"):
                build_assets(config, self.output)

        self.assertTrue(
            (config.staging_parent / BACKUP_ID / "codex-history" / "manifest.json").is_file()
        )

    def test_cli_prints_summary_without_conversation_content(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "--codex-home",
                    str(self.home),
                    "--output-dir",
                    str(self.output),
                    "--exclude-thread",
                    EXCLUDED_ID,
                    "--backup-id",
                    BACKUP_ID,
                    "--codex-version",
                    "0.147.0",
                ]
            )

        combined = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertNotIn(SECRET_SENTINEL, combined)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["backup_id"], BACKUP_ID)
        self.assertEqual(summary["excluded_rollouts"], 1)
        self.assertEqual(
            summary["archive_sha256"],
            {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (self.output / summary["archive_assets"][0],)
            },
        )


if __name__ == "__main__":
    unittest.main()
