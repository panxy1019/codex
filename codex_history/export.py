from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .archive import ArchiveError, create_tar_zst, require_archive_tools, split_archive
from .safeio import PathSafetyError, SourceChangedError, sha256_file
from .snapshot import SnapshotConfig, build_snapshot


@dataclasses.dataclass(frozen=True, slots=True)
class AssetSet:
    archive_paths: tuple[Path, ...]
    checksum_path: Path
    manifest_path: Path
    backup_id: str


def _write_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def build_assets(config: SnapshotConfig, output_dir: Path) -> AssetSet:
    require_archive_tools()
    output_dir = Path(os.path.abspath(output_dir))
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot = build_snapshot(config)
    archive = output_dir / f"codex-history-{config.backup_id}.tar.zst"
    manifest_path = output_dir / f"codex-history-{config.backup_id}.manifest.json"
    checksum_path = output_dir / f"codex-history-{config.backup_id}.tar.zst.sha256"
    completed = False
    try:
        create_tar_zst(snapshot.root, archive, config.compression_level)
        archive_paths = split_archive(archive, config.split_threshold_bytes)
        _write_exclusive(manifest_path, snapshot.manifest_path.read_bytes())
        checksum_bytes = "".join(
            f"{sha256_file(path)}  {path.name}\n" for path in archive_paths
        ).encode("utf-8")
        _write_exclusive(checksum_path, checksum_bytes)
        if archive_paths != (archive,):
            archive.unlink()
        completed = True
        return AssetSet(
            archive_paths=archive_paths,
            checksum_path=checksum_path,
            manifest_path=manifest_path,
            backup_id=config.backup_id,
        )
    except Exception:
        for path in (manifest_path, checksum_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        snapshot_parent = snapshot.root.parent
        if completed and snapshot_parent.exists():
            shutil.rmtree(snapshot_parent)


def _default_backup_id() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _installed_codex_version() -> str:
    try:
        result = subprocess.run(
            ["codex", "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ArchiveError("could not determine installed Codex version") from exc
    version = result.stdout.strip()
    if not version:
        raise ArchiveError("installed Codex version command returned no output")
    return version


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export verified local Codex history into tar.zst Release assets."
    )
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    filters = parser.add_mutually_exclusive_group(required=True)
    filters.add_argument("--exclude-thread", action="append")
    filters.add_argument("--include-thread", action="append")
    parser.add_argument("--backup-id", default=None)
    parser.add_argument("--codex-version", default=None)
    parser.add_argument("--compression-level", default=1, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        backup_id = args.backup_id or _default_backup_id()
        codex_version = args.codex_version or _installed_codex_version()
        output_dir = Path(os.path.abspath(args.output_dir))
        config = SnapshotConfig(
            codex_home=args.codex_home,
            staging_parent=output_dir / ".staging",
            backup_id=backup_id,
            excluded_thread_ids=frozenset(args.exclude_thread or ()),
            included_thread_ids=(
                frozenset(args.include_thread) if args.include_thread is not None else None
            ),
            codex_version=codex_version,
            source_platform=platform.system().lower(),
            compression_level=args.compression_level,
        )
        assets = build_assets(config, output_dir)
        manifest = json.loads(assets.manifest_path.read_text(encoding="utf-8"))
        archive_sha256 = {
            path.name: sha256_file(path) for path in assets.archive_paths
        }
        summary = {
            "backup_id": assets.backup_id,
            "archive_assets": [path.name for path in assets.archive_paths],
            "archive_sha256": archive_sha256,
            "checksum_asset": assets.checksum_path.name,
            "manifest_asset": assets.manifest_path.name,
            "session_count": manifest["counts"]["session_count"],
            "rollout_files": manifest["counts"]["rollout_file_count"],
            "attachments": manifest["counts"]["attachment_count"],
            "excluded_rollouts": sum(
                item["matched_rollouts"] for item in manifest["exclusions"]
            ),
            "included_thread_filter": sorted(args.include_thread or ()),
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except (ArchiveError, PathSafetyError, SourceChangedError, OSError, ValueError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
