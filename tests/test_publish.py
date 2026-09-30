from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_history.publish import (
    PublishConfig,
    ReleaseVerificationError,
    publish_verified_release,
)
from codex_history.verify import IntegrityError


BACKUP_ID = "2026-09-30T010203Z"
TAG = f"codex-history-{BACKUP_ID}"
REPO = "panxy1019/codex"
URL = f"https://github.com/{REPO}/releases/tag/{TAG}"


class FakeRunner:
    def __init__(self, config: PublishConfig) -> None:
        self.config = config
        self.commands: list[list[str]] = []
        self.dirty = False
        self.branch = "main"
        self.head = "abc123"
        self.origin_head = "abc123"
        self.tracked = True
        self.release: str = "missing"
        self.fail_operation: str | None = None
        self.alter_downloaded_manifest = False

    def run(self, argv, cwd=None):
        command = [str(item) for item in argv]
        self.commands.append(command)
        if command[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(command, 0, " M file\n" if self.dirty else "", "")
        if command[:3] == ["git", "branch", "--show-current"]:
            return subprocess.CompletedProcess(command, 0, self.branch + "\n", "")
        if command[:2] == ["git", "rev-parse"]:
            value = self.origin_head if command[2] == "refs/remotes/origin/main" else self.head
            return subprocess.CompletedProcess(command, 0, value + "\n", "")
        if command[:2] == ["git", "ls-files"]:
            code = 0 if self.tracked else 1
            return subprocess.CompletedProcess(command, code, "", "not tracked" if code else "")
        if command[:3] == ["gh", "release", "view"]:
            if self.release == "missing":
                return subprocess.CompletedProcess(command, 1, "", "not found")
            payload = {"isDraft": self.release == "draft", "tagName": TAG, "url": URL}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command[:3] == ["gh", "release", "create"]:
            self.release = "draft"
            return subprocess.CompletedProcess(
                command,
                0,
                "https://github.com/owner/repo/releases/tag/untagged-draft\n",
                "",
            )
        if command[:3] == ["gh", "release", "upload"]:
            if self.fail_operation == "upload":
                raise subprocess.CalledProcessError(1, command, stderr="upload failed")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["gh", "release", "download"]:
            if self.fail_operation == "download":
                raise subprocess.CalledProcessError(1, command, stderr="download interrupted")
            destination = Path(command[command.index("--dir") + 1])
            destination.mkdir(parents=True, exist_ok=True)
            for path in (
                *self.config.archive_paths,
                self.config.checksum_path,
                self.config.manifest_path,
            ):
                shutil.copy2(path, destination / path.name)
            if self.alter_downloaded_manifest:
                (destination / self.config.manifest_path.name).write_bytes(b"altered")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["gh", "release", "edit"]:
            self.release = "published"
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")


class PublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.checkout = self.base / "checkout"
        self.assets = self.base / "assets"
        self.checkout.mkdir()
        self.assets.mkdir()
        self.archive = self.assets / f"codex-history-{BACKUP_ID}.tar.zst"
        self.checksum = self.assets / f"codex-history-{BACKUP_ID}.tar.zst.sha256"
        self.manifest = self.assets / f"codex-history-{BACKUP_ID}.manifest.json"
        self.archive.write_bytes(b"archive")
        self.checksum.write_text("checksum\n", encoding="utf-8")
        self.manifest.write_text(json.dumps({"backup_id": BACKUP_ID}) + "\n", encoding="utf-8")
        tracked_manifest = self.checkout / "manifests" / f"{BACKUP_ID}.json"
        tracked_checksum = self.checkout / "checksums" / f"{BACKUP_ID}.sha256"
        tracked_manifest.parent.mkdir()
        tracked_checksum.parent.mkdir()
        shutil.copy2(self.manifest, tracked_manifest)
        shutil.copy2(self.checksum, tracked_checksum)
        self.config = PublishConfig(
            repo=REPO,
            tag=TAG,
            title=f"Codex history backup {BACKUP_ID}",
            archive_paths=(self.archive,),
            checksum_path=self.checksum,
            manifest_path=self.manifest,
            checkout=self.checkout,
        )

    @mock.patch("codex_history.publish.verify_archive")
    def test_publisher_creates_draft_verifies_download_then_publishes(self, verify) -> None:
        runner = FakeRunner(self.config)

        url = publish_verified_release(self.config, runner)

        self.assertEqual(url, URL)
        operations = [command[2] for command in runner.commands if command[:2] == ["gh", "release"]]
        self.assertEqual(
            operations,
            ["view", "create", "upload", "download", "edit", "view"],
        )
        downloaded_archives = verify.call_args.args[0]
        self.assertNotEqual(downloaded_archives[0], self.archive)
        self.assertEqual(downloaded_archives[0].name, self.archive.name)

    @mock.patch("codex_history.publish.verify_archive")
    def test_downloaded_asset_mismatch_keeps_release_draft(self, verify) -> None:
        runner = FakeRunner(self.config)
        runner.alter_downloaded_manifest = True
        verify.side_effect = IntegrityError("public manifest mismatch")

        with self.assertRaises(ReleaseVerificationError):
            publish_verified_release(self.config, runner)

        self.assertEqual(runner.release, "draft")
        self.assertFalse(any(command[:3] == ["gh", "release", "edit"] for command in runner.commands))

    def test_dirty_wrong_branch_or_unpushed_checkout_is_rejected(self) -> None:
        for attribute, value in (("dirty", True), ("branch", "feature"), ("origin_head", "different")):
            with self.subTest(attribute=attribute):
                runner = FakeRunner(self.config)
                setattr(runner, attribute, value)
                with self.assertRaises(ReleaseVerificationError):
                    publish_verified_release(self.config, runner)
                self.assertFalse(any(command[0] == "gh" for command in runner.commands))

    def test_metadata_must_be_tracked_and_byte_identical(self) -> None:
        runner = FakeRunner(self.config)
        runner.tracked = False
        with self.assertRaises(ReleaseVerificationError):
            publish_verified_release(self.config, runner)

        runner = FakeRunner(self.config)
        (self.checkout / "manifests" / f"{BACKUP_ID}.json").write_bytes(b"different")
        with self.assertRaises(ReleaseVerificationError):
            publish_verified_release(self.config, runner)

    def test_existing_published_tag_is_ambiguous_but_draft_is_reusable(self) -> None:
        published = FakeRunner(self.config)
        published.release = "published"
        with self.assertRaises(ReleaseVerificationError):
            publish_verified_release(self.config, published)

        draft = FakeRunner(self.config)
        draft.release = "draft"
        with mock.patch("codex_history.publish.verify_archive"):
            self.assertEqual(publish_verified_release(self.config, draft), URL)
        self.assertFalse(any(command[:3] == ["gh", "release", "create"] for command in draft.commands))

    def test_failed_upload_or_interrupted_download_never_publishes(self) -> None:
        for operation in ("upload", "download"):
            with self.subTest(operation=operation):
                runner = FakeRunner(self.config)
                runner.fail_operation = operation
                with self.assertRaises(ReleaseVerificationError):
                    publish_verified_release(self.config, runner)
                self.assertEqual(runner.release, "draft")
                self.assertFalse(any(command[:3] == ["gh", "release", "edit"] for command in runner.commands))

    @mock.patch("codex_history.publish.verify_archive")
    def test_shell_metacharacters_remain_literal_argv_entries(self, verify) -> None:
        config = PublishConfig(
            repo="owner/repo;touch PWNED",
            tag=TAG,
            title=f"Codex history backup {BACKUP_ID}",
            archive_paths=self.config.archive_paths,
            checksum_path=self.checksum,
            manifest_path=self.manifest,
            checkout=self.checkout,
        )
        runner = FakeRunner(config)

        publish_verified_release(config, runner)

        flattened = [item for command in runner.commands for item in command]
        self.assertIn("owner/repo;touch PWNED", flattened)
        self.assertFalse((self.checkout / "PWNED").exists())

    def test_publish_script_runs_directly(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "scripts/publish_codex_history.py", "--help"],
            cwd=repository,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
