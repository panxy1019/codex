from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from codex_history.rollouts import (
    RolloutFormatError,
    discover_rollouts,
    read_rollout_info,
    select_rollouts,
)
from tests.helpers import rollout_name, write_attachment, write_rollout


INCLUDED_ID = "01a09fc2-b66b-7742-b63a-93f58ba7d907"
SECOND_ID = "01a09fc2-b66b-7742-b63a-93f58ba7d908"
EXCLUDED_ID = "01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b"
MISMATCH_ID = "01a09fc2-b66b-7742-b63a-93f58ba7d999"


class RolloutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / ".codex"

    def test_select_rollouts_excludes_all_copies_and_private_or_shared_attachments(self) -> None:
        included_only = write_attachment(self.home, "included/pasted-text.txt")
        private = write_attachment(self.home, "private/pasted-text.txt")
        shared = write_attachment(self.home, "shared/pasted-text.txt")
        write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=INCLUDED_ID,
            attachment_paths=(included_only, shared),
        )
        write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/29",
            filename_thread_id=EXCLUDED_ID,
            metadata_thread_id=EXCLUDED_ID,
            attachment_paths=(private, shared),
        )
        write_rollout(
            self.home,
            bucket="archived_sessions",
            relative_parent="",
            filename_thread_id=EXCLUDED_ID,
            metadata_thread_id=EXCLUDED_ID,
            attachment_paths=(private,),
            stamp="2026-09-28T01-02-03",
        )

        selection = select_rollouts(self.home, frozenset({EXCLUDED_ID}))

        self.assertEqual({item.thread_id for item in selection.included}, {INCLUDED_ID})
        self.assertEqual(len(selection.excluded), 2)
        self.assertEqual(
            selection.selected_attachments,
            (PurePosixPath("attachments/included/pasted-text.txt"),),
        )

    def test_filename_and_session_meta_uuid_mismatch_fails_closed(self) -> None:
        path = write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=MISMATCH_ID,
        )

        with self.assertRaisesRegex(RolloutFormatError, "thread id mismatch"):
            read_rollout_info(path, self.home)

    def test_malformed_json_before_metadata_and_absent_metadata_fail_closed(self) -> None:
        malformed = write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=INCLUDED_ID,
            prefix_lines=("{not-json",),
        )
        absent_parent = self.home / "sessions" / "2026/09/29"
        absent_parent.mkdir(parents=True)
        absent = absent_parent / rollout_name(SECOND_ID)
        absent.write_text(json.dumps({"type": "event_msg", "payload": {}}) + "\n")

        with self.assertRaisesRegex(RolloutFormatError, "invalid JSON"):
            read_rollout_info(malformed, self.home)
        with self.assertRaisesRegex(RolloutFormatError, "session_meta"):
            read_rollout_info(absent, self.home)

    def test_malformed_record_after_identity_is_preserved_and_scanned_for_attachments(self) -> None:
        attachment = write_attachment(self.home, "legacy/referenced.txt")
        path = write_rollout(
            self.home,
            bucket="archived_sessions",
            relative_parent="",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=INCLUDED_ID,
        )
        with path.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"event_msg","attachment":"' + str(attachment) + '"\n')

        info = read_rollout_info(path, self.home)

        self.assertEqual(
            info.attachment_paths,
            frozenset({PurePosixPath("attachments/legacy/referenced.txt")}),
        )

    def test_id_is_rollout_identity_and_session_id_can_name_parent_for_exclusion(self) -> None:
        legacy = write_rollout(
            self.home,
            bucket="archived_sessions",
            relative_parent="",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=None,
            legacy_thread_id=INCLUDED_ID,
        )
        child = write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=SECOND_ID,
            metadata_thread_id=EXCLUDED_ID,
            legacy_thread_id=SECOND_ID,
        )

        info = read_rollout_info(legacy, self.home)
        self.assertEqual(info.thread_id, INCLUDED_ID)
        self.assertEqual(info.source_bucket, "archived_sessions")
        self.assertTrue(str(info.relative_path).startswith("archived_sessions/"))
        child_info = read_rollout_info(child, self.home)
        self.assertEqual(child_info.thread_id, SECOND_ID)
        self.assertEqual(child_info.session_id, EXCLUDED_ID)
        selection = select_rollouts(self.home, frozenset({EXCLUDED_ID}))
        self.assertEqual([item.thread_id for item in selection.excluded], [SECOND_ID])

    def test_filename_must_match_id_when_id_and_session_id_differ(self) -> None:
        path = write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=SECOND_ID,
            metadata_thread_id=SECOND_ID,
            legacy_thread_id=MISMATCH_ID,
        )

        with self.assertRaisesRegex(RolloutFormatError, "thread id mismatch"):
            read_rollout_info(path, self.home)

    def test_collision_suffix_uuid_does_not_replace_primary_filename_identity(self) -> None:
        path = write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=INCLUDED_ID,
        )
        suffixed = path.with_name(path.stem + f"_{SECOND_ID}.jsonl")
        path.rename(suffixed)

        info = read_rollout_info(suffixed, self.home)

        self.assertEqual(info.thread_id, INCLUDED_ID)

    def test_first_session_meta_defines_identity_when_parent_history_is_embedded(self) -> None:
        path = write_rollout(
            self.home,
            bucket="archived_sessions",
            relative_parent="",
            filename_thread_id=SECOND_ID,
            metadata_thread_id=INCLUDED_ID,
            legacy_thread_id=SECOND_ID,
        )
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": INCLUDED_ID,
                            "session_id": INCLUDED_ID,
                        },
                    }
                )
                + "\n"
            )

        info = read_rollout_info(path, self.home)

        self.assertEqual(info.thread_id, SECOND_ID)
        self.assertEqual(info.session_id, INCLUDED_ID)

    def test_discovery_is_recursive_sorted_and_groups_duplicate_included_ids(self) -> None:
        later = write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=INCLUDED_ID,
            stamp="2026-09-30T02-00-00",
        )
        earlier = write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/29",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=INCLUDED_ID,
            stamp="2026-09-29T02-00-00",
        )
        ignored = self.home / "sessions" / "not-a-rollout.txt"
        ignored.write_text("ignored")

        discovered = discover_rollouts(self.home)
        selection = select_rollouts(self.home, frozenset())

        self.assertEqual(discovered, tuple(sorted((earlier, later), key=lambda item: item.as_posix())))
        self.assertEqual(
            selection.duplicate_thread_ids,
            {
                INCLUDED_ID: tuple(
                    sorted(info.relative_path.as_posix() for info in selection.included)
                )
            },
        )

    def test_attachment_strings_outside_codex_attachments_are_ignored(self) -> None:
        inside = write_attachment(self.home, "inside/pasted-text.txt")
        path = write_rollout(
            self.home,
            bucket="sessions",
            relative_parent="2026/09/30",
            filename_thread_id=INCLUDED_ID,
            metadata_thread_id=INCLUDED_ID,
            attachment_paths=(inside, Path(self.temp.name) / "outside.txt"),
        )

        info = read_rollout_info(path, self.home)

        self.assertEqual(
            info.attachment_paths,
            frozenset({PurePosixPath("attachments/inside/pasted-text.txt")}),
        )

    def test_symlinked_rollout_file_or_directory_is_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        real = outside / rollout_name(INCLUDED_ID)
        real.write_text(
            json.dumps(
                {"type": "session_meta", "payload": {"session_id": INCLUDED_ID}}
            )
            + "\n"
        )
        sessions = self.home / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "linked.jsonl").symlink_to(real)
        (sessions / "linked-dir").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(RolloutFormatError, "symlink"):
            discover_rollouts(self.home)


if __name__ == "__main__":
    unittest.main()
