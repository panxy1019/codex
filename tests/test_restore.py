from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from codex_history.restore import (
    RestoreConflictError,
    apply_restore,
    plan_restore,
    rollback_restore,
)
from codex_history.snapshot import SnapshotConfig, build_snapshot
from tests.helpers import write_attachment, write_rollout


THREAD_A = "01a09fc2-b66b-7742-b63a-93f58ba7d907"
THREAD_B = "01a09fc2-b66b-7742-b63a-93f58ba7d908"
BACKUP_ID = "2026-09-30T010203Z"


class RestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source_home = self.base / "source" / ".codex"
        self.target_home = self.base / "target" / ".codex"
        self.target_home.mkdir(parents=True)

    def _snapshot(self, *, duplicate: bool = False) -> Path:
        attachment_a = write_attachment(self.source_home, "a/note.txt", "same")
        attachment_b = write_attachment(self.source_home, "b/note.txt", "backup")
        write_rollout(
            self.source_home,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=THREAD_A,
            metadata_thread_id=THREAD_A,
            attachment_paths=(attachment_a, attachment_b),
        )
        if duplicate:
            write_rollout(
                self.source_home,
                bucket="archived_sessions",
                relative_parent="",
                filename_thread_id=THREAD_A,
                metadata_thread_id=THREAD_A,
                stamp="2026-09-29T01-02-03",
            )
        return build_snapshot(
            SnapshotConfig(
                codex_home=self.source_home,
                staging_parent=self.base / "staging",
                backup_id=BACKUP_ID,
                excluded_thread_ids=frozenset(),
                codex_version="test",
                source_platform="linux",
            )
        ).root

    def test_plan_restore_classifies_add_identical_and_path_conflict(self) -> None:
        root = self._snapshot()
        identical = self.target_home / "attachments/a/note.txt"
        identical.parent.mkdir(parents=True)
        identical.write_text("same", encoding="utf-8")
        conflict = self.target_home / "attachments/b/note.txt"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("target", encoding="utf-8")

        plan = plan_restore(root, self.target_home)

        statuses = {action.relative_path: action.status for action in plan.actions}
        self.assertEqual(statuses["attachments/a/note.txt"], "skip-identical")
        self.assertEqual(statuses["attachments/b/note.txt"], "conflict")
        self.assertEqual(statuses[next(path for path in statuses if path.endswith(".jsonl"))], "add")
        self.assertTrue(plan.has_conflicts)

    def test_same_thread_id_at_different_path_with_different_bytes_is_semantic_conflict(self) -> None:
        root = self._snapshot()
        write_rollout(
            self.target_home,
            bucket="archived_sessions",
            relative_parent="",
            filename_thread_id=THREAD_A,
            metadata_thread_id=THREAD_A,
            prefix_lines=(json.dumps({"different": True}),),
            stamp="2026-09-28T01-02-03",
        )

        plan = plan_restore(root, self.target_home)

        self.assertIn(THREAD_A, plan.semantic_conflicts)
        self.assertTrue(plan.has_conflicts)

    def test_duplicate_thread_ids_inside_backup_block_restore(self) -> None:
        plan = plan_restore(self._snapshot(duplicate=True), self.target_home)

        self.assertIn(THREAD_A, plan.semantic_conflicts)
        with self.assertRaises(RestoreConflictError):
            apply_restore(plan, self.base / "restore-journal.json")

    def test_missing_or_symlink_target_home_is_rejected(self) -> None:
        root = self._snapshot()
        missing = self.base / "missing" / ".codex"
        with self.assertRaises(ValueError):
            plan_restore(root, missing)
        link = self.base / "linked-codex"
        link.symlink_to(self.target_home, target_is_directory=True)
        with self.assertRaises(ValueError):
            plan_restore(root, link)

    def test_apply_never_overwrites_file_created_after_dry_run(self) -> None:
        plan = plan_restore(self._snapshot(), self.target_home)
        action = next(item for item in plan.actions if item.status == "add")
        action.destination.parent.mkdir(parents=True, exist_ok=True)
        action.destination.write_bytes(b"created concurrently")

        with self.assertRaises(RestoreConflictError):
            apply_restore(plan, self.base / "restore-journal.json")

        self.assertEqual(action.destination.read_bytes(), b"created concurrently")

    def test_apply_does_not_touch_sqlite_and_journals_only_completed_additions(self) -> None:
        root = self._snapshot()
        database = self.target_home / "state_5.sqlite"
        database.write_bytes(b"database sentinel")
        plan = plan_restore(root, self.target_home)
        journal = self.base / "restore-journal.json"

        result = apply_restore(plan, journal)

        self.assertEqual(result, journal)
        self.assertEqual(database.read_bytes(), b"database sentinel")
        payload = json.loads(journal.read_text(encoding="utf-8"))
        self.assertEqual(payload["backup_id"], BACKUP_ID)
        self.assertEqual(
            len(payload["additions"]),
            sum(action.status == "add" for action in plan.actions),
        )
        self.assertFalse(any("sqlite" in item["relative_path"] for item in payload["additions"]))

    def test_rollback_deletes_only_unchanged_files_installed_by_journal(self) -> None:
        plan = plan_restore(self._snapshot(), self.target_home)
        journal = self.base / "restore-journal.json"
        apply_restore(plan, journal)
        added = [action.destination for action in plan.actions if action.status == "add"]
        changed = added[0]
        unchanged = added[1]
        changed.write_bytes(b"user changed this")

        report = rollback_restore(journal)

        self.assertFalse(unchanged.exists())
        self.assertTrue(changed.exists())
        self.assertIn(changed, report.refused)
        self.assertIn(unchanged, report.removed)

    def test_apply_rejects_existing_conflicts_without_partial_writes(self) -> None:
        root = self._snapshot()
        conflict = self.target_home / "attachments/b/note.txt"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("target", encoding="utf-8")
        plan = plan_restore(root, self.target_home)

        with self.assertRaises(RestoreConflictError):
            apply_restore(plan, self.base / "restore-journal.json")

        self.assertFalse(any(
            action.destination.exists()
            for action in plan.actions
            if action.status == "add"
        ))

    def test_restore_script_runs_directly_and_documents_default_dry_run(self) -> None:
        repository = Path(__file__).resolve().parents[1]

        result = subprocess.run(
            [sys.executable, "scripts/restore_codex_history.py", "--help"],
            cwd=repository,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry-run by default", result.stdout)


if __name__ == "__main__":
    unittest.main()
