from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import write_attachment, write_rollout


INCLUDED_ID = "01a09fc2-b66b-7742-b63a-93f58ba7d907"
EXCLUDED_ID = "01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b"
BACKUP_ID = "2026-09-30T010203Z"
SECRET_SENTINEL = "DO-NOT-PUBLISH-CURRENT-THREAD"


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repository = Path(__file__).resolve().parents[1]
        self.source = self.base / "source" / ".codex"
        self.output = self.base / "assets"
        self.extracted = self.base / "extracted"
        self.target = self.base / "target" / ".codex"
        self.target.mkdir(parents=True)

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, *arguments],
            cwd=self.repository,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_export_verify_restore_round_trip_is_byte_exact_and_idempotent(self) -> None:
        included_attachment = write_attachment(
            self.source, "included/note.txt", "included attachment"
        )
        excluded_attachment = write_attachment(
            self.source, "excluded/note.txt", SECRET_SENTINEL
        )
        included_rollout = write_rollout(
            self.source,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=INCLUDED_ID,
            attachment_paths=(included_attachment,),
        )
        excluded_rollout = write_rollout(
            self.source,
            bucket="sessions",
            relative_parent="2026/09/29",
            filename_thread_id=EXCLUDED_ID,
            metadata_thread_id=EXCLUDED_ID,
            attachment_paths=(excluded_attachment,),
            prefix_lines=(json.dumps({"secret": SECRET_SENTINEL}),),
        )

        outputs: list[str] = []
        export = self._run(
            "scripts/export_codex_history.py",
            "--codex-home",
            str(self.source),
            "--output-dir",
            str(self.output),
            "--exclude-thread",
            EXCLUDED_ID,
            "--backup-id",
            BACKUP_ID,
            "--codex-version",
            "test",
        )
        outputs.extend((export.stdout, export.stderr))
        archive = self.output / f"codex-history-{BACKUP_ID}.tar.zst"
        checksum = self.output / f"codex-history-{BACKUP_ID}.tar.zst.sha256"
        manifest = self.output / f"codex-history-{BACKUP_ID}.manifest.json"
        verify = self._run(
            "scripts/verify_backup.py",
            "--archive",
            str(archive),
            "--checksum",
            str(checksum),
            "--manifest",
            str(manifest),
            "--destination",
            str(self.extracted),
        )
        outputs.extend((verify.stdout, verify.stderr))
        report = json.loads(verify.stdout)
        self.assertEqual(report["excluded_thread_ids"], [EXCLUDED_ID])

        snapshot_root = self.extracted / "codex-history"
        journal = self.base / "restore-journal.json"
        apply = self._run(
            "scripts/restore_codex_history.py",
            "--snapshot-root",
            str(snapshot_root),
            "--target-codex-home",
            str(self.target),
            "--apply",
            "--journal",
            str(journal),
        )
        outputs.extend((apply.stdout, apply.stderr))
        second = self._run(
            "scripts/restore_codex_history.py",
            "--snapshot-root",
            str(snapshot_root),
            "--target-codex-home",
            str(self.target),
        )
        outputs.extend((second.stdout, second.stderr))
        second_summary = json.loads(second.stdout.splitlines()[-1])
        self.assertEqual(second_summary["counts"]["add"], 0)
        self.assertEqual(second_summary["counts"]["conflict"], 0)
        self.assertEqual(second_summary["counts"]["skip-identical"], 2)

        restored_rollout = self.target / included_rollout.relative_to(self.source)
        restored_attachment = self.target / included_attachment.relative_to(self.source)
        self.assertEqual(restored_rollout.read_bytes(), included_rollout.read_bytes())
        self.assertEqual(restored_attachment.read_bytes(), included_attachment.read_bytes())
        self.assertFalse((self.target / excluded_rollout.relative_to(self.source)).exists())
        self.assertFalse((self.target / excluded_attachment.relative_to(self.source)).exists())

        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(json.dumps(manifest_value).count(EXCLUDED_ID), 1)
        self.assertEqual(manifest_value["exclusions"][0]["thread_id"], EXCLUDED_ID)
        self.assertNotIn(SECRET_SENTINEL, "".join(outputs))
        self.assertTrue((self.repository / "README.md").is_file())
        self.assertTrue((self.repository / "SECURITY-NOTICE.md").is_file())


if __name__ == "__main__":
    unittest.main()
