import dataclasses
import unittest

from codex_history.models import (
    ArchiveParameters,
    ExclusionRecord,
    FileRecord,
    ManifestError,
    RolloutRecord,
    SnapshotCounts,
    SnapshotManifest,
)


INCLUDED_ID = "01a09fc2-b66b-7742-b63a-93f58ba7d907"
EXCLUDED_ID = "01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b"
ROLLOUT_PATH = (
    "sessions/2026/09/29/"
    "rollout-2026-09-29T01-02-03-01a09fc2-b66b-7742-b63a-93f58ba7d907.jsonl"
)


def sample_file_record(**overrides: object) -> FileRecord:
    values: dict[str, object] = {
        "path": ROLLOUT_PATH,
        "kind": "rollout",
        "size": 12,
        "sha256": "a" * 64,
        "thread_id": INCLUDED_ID,
        "source_bucket": "sessions",
    }
    values.update(overrides)
    return FileRecord(**values)


def sample_manifest() -> SnapshotManifest:
    return SnapshotManifest(
        schema_version=1,
        backup_id="2026-09-30T010203Z",
        created_at_utc="2026-09-30T01:02:03Z",
        source_codex_version="0.147.0",
        source_platform="linux",
        source_root="$CODEX_HOME",
        counts=SnapshotCounts(
            session_count=1,
            rollout_file_count=1,
            attachment_count=0,
            total_bytes=12,
        ),
        files=(sample_file_record(),),
        rollouts=(
            RolloutRecord(
                thread_id=INCLUDED_ID,
                source_bucket="sessions",
                path=ROLLOUT_PATH,
                sha256="a" * 64,
            ),
        ),
        exclusions=(
            ExclusionRecord(
                thread_id=EXCLUDED_ID,
                reason="active-thread",
                matched_rollouts=1,
            ),
        ),
        duplicate_thread_ids={},
        archive=ArchiveParameters(
            format="tar.zst",
            compression="zstd",
            compression_level=1,
            split_threshold_bytes=1_932_735_283,
        ),
    )


class ManifestModelTests(unittest.TestCase):
    def test_manifest_json_is_canonical_and_contains_no_absolute_source_root(self) -> None:
        payload = sample_manifest().to_json_bytes()

        expected = (
            '{"archive":{"compression":"zstd","compression_level":1,'
            '"format":"tar.zst","split_threshold_bytes":1932735283},'
            '"backup_id":"2026-09-30T010203Z",'
            '"counts":{"attachment_count":0,"rollout_file_count":1,'
            '"session_count":1,"total_bytes":12},'
            '"created_at_utc":"2026-09-30T01:02:03Z",'
            '"duplicate_thread_ids":{},'
            '"exclusions":[{"matched_rollouts":1,"reason":"active-thread",'
            f'"thread_id":"{EXCLUDED_ID}"}}],'
            f'"files":[{{"kind":"rollout","path":"{ROLLOUT_PATH}",'
            f'"sha256":"{"a" * 64}","size":12,'
            f'"source_bucket":"sessions","thread_id":"{INCLUDED_ID}"}}],'
            f'"rollouts":[{{"path":"{ROLLOUT_PATH}",'
            f'"sha256":"{"a" * 64}","source_bucket":"sessions",'
            f'"thread_id":"{INCLUDED_ID}"}}],'
            '"schema_version":1,"source_codex_version":"0.147.0",'
            '"source_platform":"linux","source_root":"$CODEX_HOME"}\n'
        ).encode()

        self.assertEqual(payload, expected)
        self.assertNotIn(b"/home/admin", payload)
        self.assertEqual(payload, sample_manifest().to_json_bytes())

    def test_manifest_rejects_non_relative_or_parent_path(self) -> None:
        for path in (
            "/absolute.jsonl",
            "../escape.jsonl",
            "sessions/../auth.json",
            "C:/Users/name/auth.json",
        ):
            with self.subTest(path=path), self.assertRaises(ManifestError):
                sample_file_record(path=path)

    def test_file_record_rejects_invalid_digest_size_or_bucket(self) -> None:
        invalid_values = (
            {"sha256": "A" * 64},
            {"sha256": "a" * 63},
            {"size": -1},
            {"kind": "database"},
            {"source_bucket": "cache"},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides), self.assertRaises(ManifestError):
                sample_file_record(**overrides)

    def test_manifest_rejects_invalid_schema_root_or_timestamp(self) -> None:
        manifest = sample_manifest()
        for field, value in (
            ("schema_version", 2),
            ("source_root", "/home/admin/.codex"),
            ("backup_id", "2026-09-30"),
            ("created_at_utc", "2026-09-30 01:02:03"),
        ):
            with self.subTest(field=field), self.assertRaises(ManifestError):
                dataclasses.replace(manifest, **{field: value})

    def test_records_are_immutable(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            sample_file_record().size = 99  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
