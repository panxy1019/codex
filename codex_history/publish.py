from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from .verify import IntegrityError, UnsafeArchiveError, verify_archive


class ReleaseVerificationError(RuntimeError):
    """Raised when a Release transaction cannot be proven safe to publish."""


class CommandRunner:
    def run(
        self, argv: Sequence[str], cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class PublishConfig:
    repo: str
    tag: str
    title: str
    archive_paths: tuple[Path, ...]
    checksum_path: Path
    manifest_path: Path
    checkout: Path


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _checked(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner.run(argv, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseVerificationError(f"command failed: {argv[0]} {argv[1]}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ReleaseVerificationError(
            f"command failed: {argv[0]} {argv[1]}: {detail}"
        )
    return result


def _manifest_backup_id(path: Path) -> str:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError("manifest is not readable JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("backup_id"), str):
        raise ReleaseVerificationError("manifest has no backup_id")
    return value["backup_id"]


def _validate_asset_names(config: PublishConfig, backup_id: str) -> None:
    base = f"codex-history-{backup_id}.tar.zst"
    archive_names = [path.name for path in config.archive_paths]
    if not archive_names:
        raise ReleaseVerificationError("at least one archive asset is required")
    if len(archive_names) == 1:
        if archive_names[0] != base:
            raise ReleaseVerificationError("archive asset name does not match backup_id")
    elif archive_names != [f"{base}.part{index:03d}" for index in range(1, len(archive_names) + 1)]:
        raise ReleaseVerificationError("split archive asset names are not contiguous")
    if config.checksum_path.name != f"{base}.sha256":
        raise ReleaseVerificationError("checksum asset name does not match backup_id")
    if config.manifest_path.name != f"codex-history-{backup_id}.manifest.json":
        raise ReleaseVerificationError("manifest asset name does not match backup_id")
    if config.tag != f"codex-history-{backup_id}":
        raise ReleaseVerificationError("release tag does not match backup_id")
    if config.title != f"Codex history backup {backup_id}":
        raise ReleaseVerificationError("release title does not match backup_id")


def _preflight_checkout(config: PublishConfig, runner: CommandRunner, backup_id: str) -> Path:
    checkout = _absolute(config.checkout)
    if checkout.is_symlink() or not checkout.is_dir():
        raise ReleaseVerificationError("checkout must be a real directory")
    status = _checked(runner, ["git", "status", "--porcelain"], cwd=checkout)
    if status.stdout:
        raise ReleaseVerificationError("checkout is dirty")
    branch = _checked(runner, ["git", "branch", "--show-current"], cwd=checkout)
    if branch.stdout.strip() != "main":
        raise ReleaseVerificationError("checkout is not on main")
    head = _checked(runner, ["git", "rev-parse", "HEAD"], cwd=checkout)
    origin = _checked(
        runner, ["git", "rev-parse", "refs/remotes/origin/main"], cwd=checkout
    )
    if head.stdout.strip() != origin.stdout.strip():
        raise ReleaseVerificationError("local HEAD does not equal origin/main")

    tracked_manifest = Path("manifests") / f"{backup_id}.json"
    tracked_checksum = Path("checksums") / f"{backup_id}.sha256"
    _checked(
        runner,
        [
            "git",
            "ls-files",
            "--error-unmatch",
            "--",
            tracked_manifest.as_posix(),
            tracked_checksum.as_posix(),
        ],
        cwd=checkout,
    )
    try:
        if (checkout / tracked_manifest).read_bytes() != config.manifest_path.read_bytes():
            raise ReleaseVerificationError("tracked manifest differs from upload asset")
        if (checkout / tracked_checksum).read_bytes() != config.checksum_path.read_bytes():
            raise ReleaseVerificationError("tracked checksum differs from upload asset")
    except OSError as exc:
        raise ReleaseVerificationError("tracked metadata could not be read") from exc
    return checkout


def _inspect_release(config: PublishConfig, runner: CommandRunner) -> tuple[bool, str]:
    command = [
        "gh",
        "release",
        "view",
        config.tag,
        "--repo",
        config.repo,
        "--json",
        "isDraft,tagName,url",
    ]
    try:
        result = runner.run(command)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseVerificationError("could not inspect existing Release") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).lower()
        if "not found" not in detail:
            raise ReleaseVerificationError("Release lookup failed ambiguously")
        return False, f"https://github.com/{config.repo}/releases/tag/{config.tag}"
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseVerificationError("Release lookup returned invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("tagName") != config.tag
        or not isinstance(value.get("url"), str)
    ):
        raise ReleaseVerificationError("Release lookup was ambiguous")
    if value.get("isDraft") is not True:
        raise ReleaseVerificationError("tag already belongs to a non-draft Release")
    return True, value["url"]


def publish_verified_release(config: PublishConfig, runner: CommandRunner) -> str:
    config = dataclasses.replace(
        config,
        archive_paths=tuple(_absolute(path) for path in config.archive_paths),
        checksum_path=_absolute(config.checksum_path),
        manifest_path=_absolute(config.manifest_path),
        checkout=_absolute(config.checkout),
    )
    backup_id = _manifest_backup_id(config.manifest_path)
    _validate_asset_names(config, backup_id)
    for asset in (*config.archive_paths, config.checksum_path, config.manifest_path):
        if asset.is_symlink() or not asset.is_file():
            raise ReleaseVerificationError(f"upload asset is not a regular file: {asset.name}")
    checkout = _preflight_checkout(config, runner, backup_id)
    existed, url = _inspect_release(config, runner)
    if not existed:
        created = _checked(
            runner,
            [
                "gh",
                "release",
                "create",
                config.tag,
                "--draft",
                "--title",
                config.title,
                "--repo",
                config.repo,
            ],
            cwd=checkout,
        )
        if created.stdout.strip():
            url = created.stdout.strip().splitlines()[-1]

    assets = (*config.archive_paths, config.checksum_path, config.manifest_path)
    _checked(
        runner,
        [
            "gh",
            "release",
            "upload",
            config.tag,
            *(str(path) for path in assets),
            "--clobber",
            "--repo",
            config.repo,
        ],
        cwd=checkout,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="codex-history-release-") as temporary_name:
            download = Path(temporary_name) / "assets"
            patterns: list[str] = []
            for asset in assets:
                patterns.extend(["--pattern", asset.name])
            _checked(
                runner,
                [
                    "gh",
                    "release",
                    "download",
                    config.tag,
                    "--repo",
                    config.repo,
                    "--dir",
                    str(download),
                    *patterns,
                ],
                cwd=checkout,
            )
            downloaded_archives = tuple(download / path.name for path in config.archive_paths)
            downloaded_checksum = download / config.checksum_path.name
            downloaded_manifest = download / config.manifest_path.name
            expected_names = {path.name for path in assets}
            actual_names = {path.name for path in download.iterdir() if path.is_file()}
            if actual_names != expected_names:
                raise ReleaseVerificationError("downloaded Release asset set differs from upload")
            verify_archive(
                downloaded_archives,
                downloaded_checksum,
                downloaded_manifest,
                Path(temporary_name) / "verified",
            )
    except ReleaseVerificationError:
        raise
    except (IntegrityError, UnsafeArchiveError, OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ReleaseVerificationError("downloaded Release assets failed verification") from exc

    _checked(
        runner,
        [
            "gh",
            "release",
            "edit",
            config.tag,
            "--draft=false",
            "--repo",
            config.repo,
        ],
        cwd=checkout,
    )
    return url


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish a Codex backup only after a downloaded round-trip verifies."
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--archive", required=True, action="append", type=Path)
    parser.add_argument("--checksum", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        backup_id = _manifest_backup_id(args.manifest)
        config = PublishConfig(
            repo=args.repo,
            tag=f"codex-history-{backup_id}",
            title=f"Codex history backup {backup_id}",
            archive_paths=tuple(args.archive),
            checksum_path=args.checksum,
            manifest_path=args.manifest,
            checkout=args.checkout,
        )
        print(publish_verified_release(config, CommandRunner()))
        return 0
    except (ReleaseVerificationError, OSError, ValueError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
