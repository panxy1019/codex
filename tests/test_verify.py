from __future__ import annotations

import hashlib
import gc
import json
import tarfile
import tempfile
import unittest
import warnings
from pathlib import Path

from codex_history.archive import create_tar_zst
from codex_history.export import build_assets
from codex_history.safeio import sha256_file
from codex_history.snapshot import SnapshotConfig, build_snapshot
from codex_history.verify import (
    IntegrityError,
    UnsafeArchiveError,
    safe_extract_tar_zst,
    verify_archive,
)
from tests.helpers import make_tar_zst, write_attachment, write_rollout


INCLUDED_ID = "01a09fc2-b66b-7742-b63a-93f58ba7d907"
EXCLUDED_ID = "01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b"
BACKUP_ID = "2026-09-30T010203Z"


def regular(name: str, data: bytes = b"") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    info.mode = 0o600
    return info, data


def special(name: str, member_type: bytes, linkname: str = "") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = member_type
    info.linkname = linkname
    return info, None


class VerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / ".codex"
        attachment = write_attachment(self.home, "included/pasted-text.txt", "included")
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
        self.counter = 0

    def _config(self, *, split_threshold: int = 1_932_735_283) -> SnapshotConfig:
        self.counter += 1
        return SnapshotConfig(
            codex_home=self.home,
            staging_parent=self.base / f"staging-{self.counter}",
            backup_id=BACKUP_ID,
            excluded_thread_ids=frozenset({EXCLUDED_ID}),
            codex_version="0.147.0",
            source_platform="linux",
            split_threshold_bytes=split_threshold,
        )

    def _assets(self, *, split_threshold: int = 1_932_735_283):
        output = self.base / f"assets-{self.counter + 1}"
        return build_assets(self._config(split_threshold=split_threshold), output)

    def _archive_mutated_snapshot(self, mutate) -> tuple[Path, Path, Path]:
        snapshot = build_snapshot(self._config())
        mutate(snapshot.root, snapshot.manifest_path, snapshot.checksums_path)
        output = self.base / f"mutated-{self.counter}.tar.zst"
        create_tar_zst(snapshot.root, output)
        public_manifest = self.base / f"mutated-{self.counter}.manifest.json"
        public_manifest.write_bytes(snapshot.manifest_path.read_bytes())
        checksum = self.base / f"mutated-{self.counter}.sha256"
        checksum.write_text(f"{sha256_file(output)}  {output.name}\n", encoding="utf-8")
        return output, checksum, public_manifest

    def test_safe_extract_rejects_late_traversal_before_writing_any_member(self) -> None:
        archive = make_tar_zst(
            self.base / "traversal.tar.zst",
            [
                regular("codex-history/sessions/safe.jsonl", b"safe"),
                regular("codex-history/../../escape", b"owned"),
            ],
        )
        destination = self.base / "extract-traversal"

        with self.assertRaises(UnsafeArchiveError):
            safe_extract_tar_zst(archive, destination)

        self.assertFalse((self.base / "escape").exists())
        self.assertFalse((destination / "codex-history/sessions/safe.jsonl").exists())

    def test_rejected_archive_closes_subprocess_pipes(self) -> None:
        archive = make_tar_zst(
            self.base / "pipe-cleanup.tar.zst",
            [special("codex-history/link", tarfile.SYMTYPE, "target")],
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with self.assertRaises(UnsafeArchiveError):
                safe_extract_tar_zst(archive, self.base / "pipe-cleanup-out")
            gc.collect()

        self.assertEqual(
            [warning for warning in caught if issubclass(warning.category, ResourceWarning)],
            [],
        )

    def test_safe_extract_rejects_absolute_links_and_special_members(self) -> None:
        cases = (
            regular("/absolute", b"bad"),
            special("codex-history/link", tarfile.SYMTYPE, "target"),
            special("codex-history/hardlink", tarfile.LNKTYPE, "target"),
            special("codex-history/device", tarfile.CHRTYPE),
            special("codex-history/fifo", tarfile.FIFOTYPE),
        )
        for index, entry in enumerate(cases):
            with self.subTest(entry=entry[0].name):
                archive = make_tar_zst(self.base / f"special-{index}.tar.zst", [entry])
                with self.assertRaises(UnsafeArchiveError):
                    safe_extract_tar_zst(archive, self.base / f"extract-{index}")

    def test_safe_extract_rejects_wrong_root_duplicate_member_and_truncated_stream(self) -> None:
        wrong_root = make_tar_zst(
            self.base / "wrong-root.tar.zst",
            [regular("other/file", b"bad")],
        )
        duplicate = make_tar_zst(
            self.base / "duplicate.tar.zst",
            [
                regular("codex-history/file", b"one"),
                regular("codex-history/file", b"two"),
            ],
        )
        valid = make_tar_zst(
            self.base / "truncated.tar.zst",
            [regular("codex-history/file", b"content")],
        )
        valid.write_bytes(valid.read_bytes()[: max(1, valid.stat().st_size // 2)])

        with self.assertRaises(UnsafeArchiveError):
            safe_extract_tar_zst(wrong_root, self.base / "wrong-root-out")
        with self.assertRaises(UnsafeArchiveError):
            safe_extract_tar_zst(duplicate, self.base / "duplicate-out")
        with self.assertRaises(IntegrityError):
            safe_extract_tar_zst(valid, self.base / "truncated-out")

    def test_verify_round_trip_supports_single_and_split_assets(self) -> None:
        for split_threshold in (1_932_735_283, 64):
            with self.subTest(split_threshold=split_threshold):
                assets = self._assets(split_threshold=split_threshold)
                destination = self.base / f"verified-{split_threshold}"

                report = verify_archive(
                    assets.archive_paths,
                    assets.checksum_path,
                    assets.manifest_path,
                    destination,
                )

                self.assertEqual(report.backup_id, BACKUP_ID)
                self.assertEqual(report.file_count, 2)
                self.assertEqual(report.excluded_thread_ids, (EXCLUDED_ID,))
                self.assertTrue((destination / "codex-history/manifest.json").is_file())

    def test_outer_checksum_mismatch_is_rejected_before_decompression(self) -> None:
        archive = self.base / "not-even-zstd.tar.zst"
        archive.write_bytes(b"not a zstd stream")
        checksum = self.base / "bad.sha256"
        checksum.write_text(f"{'0' * 64}  {archive.name}\n", encoding="utf-8")
        manifest = self.base / "manifest.json"
        manifest.write_text("{}\n", encoding="utf-8")
        destination = self.base / "outer-mismatch"

        with self.assertRaisesRegex(IntegrityError, "outer checksum"):
            verify_archive((archive,), checksum, manifest, destination)

        self.assertFalse(destination.exists())

    def test_public_manifest_mismatch_is_rejected(self) -> None:
        assets = self._assets()
        altered = self.base / "altered.manifest.json"
        altered.write_text('{"schema_version":1,"altered":true}\n', encoding="utf-8")

        with self.assertRaisesRegex(IntegrityError, "public manifest"):
            verify_archive(
                assets.archive_paths,
                assets.checksum_path,
                altered,
                self.base / "altered-out",
            )

    def test_modified_declared_file_and_undeclared_file_are_rejected(self) -> None:
        def modify_declared(root: Path, _manifest: Path, _checksums: Path) -> None:
            rollout = next((root / "sessions").rglob("*.jsonl"))
            rollout.write_text("tampered\n", encoding="utf-8")

        archive, checksum, manifest = self._archive_mutated_snapshot(modify_declared)
        with self.assertRaisesRegex(IntegrityError, "digest"):
            verify_archive((archive,), checksum, manifest, self.base / "tampered-out")

        def add_undeclared(root: Path, _manifest: Path, _checksums: Path) -> None:
            (root / "undeclared.txt").write_text("extra", encoding="utf-8")

        archive, checksum, manifest = self._archive_mutated_snapshot(add_undeclared)
        with self.assertRaisesRegex(IntegrityError, "undeclared"):
            verify_archive((archive,), checksum, manifest, self.base / "undeclared-out")

    def test_missing_declared_file_is_rejected(self) -> None:
        def remove_attachment(root: Path, _manifest: Path, _checksums: Path) -> None:
            next((root / "attachments").rglob("*.txt")).unlink()

        archive, checksum, manifest = self._archive_mutated_snapshot(remove_attachment)
        with self.assertRaisesRegex(IntegrityError, "missing"):
            verify_archive((archive,), checksum, manifest, self.base / "missing-out")

    def test_excluded_id_in_included_rollout_record_is_rejected(self) -> None:
        def inject_excluded_id(root: Path, manifest_path: Path, checksums_path: Path) -> None:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["rollouts"][0]["thread_id"] = EXCLUDED_ID
            rollout_file = next(
                record for record in manifest["files"] if record["kind"] == "rollout"
            )
            rollout_file["thread_id"] = EXCLUDED_ID
            manifest_bytes = (
                json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                + "\n"
            ).encode("utf-8")
            manifest_path.write_bytes(manifest_bytes)
            lines = []
            for line in checksums_path.read_text(encoding="utf-8").splitlines():
                digest, relative = line.split("  ", 1)
                if relative == "manifest.json":
                    digest = hashlib.sha256(manifest_bytes).hexdigest()
                lines.append(f"{digest}  {relative}")
            checksums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        archive, checksum, manifest = self._archive_mutated_snapshot(inject_excluded_id)
        with self.assertRaisesRegex(IntegrityError, "excluded thread"):
            verify_archive((archive,), checksum, manifest, self.base / "excluded-out")


if __name__ == "__main__":
    unittest.main()
