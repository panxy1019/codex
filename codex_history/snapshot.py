from __future__ import annotations

import dataclasses
import datetime as dt
import os
from pathlib import Path

from .models import (
    ArchiveParameters,
    ExclusionRecord,
    FileRecord,
    RolloutRecord,
    SnapshotCounts,
    SnapshotManifest,
)
from .rollouts import select_rollouts
from .safeio import sha256_file, stable_copy


@dataclasses.dataclass(frozen=True, slots=True)
class SnapshotConfig:
    codex_home: Path
    staging_parent: Path
    backup_id: str
    excluded_thread_ids: frozenset[str]
    codex_version: str
    source_platform: str
    compression_level: int = 1
    split_threshold_bytes: int = 1_932_735_283


@dataclasses.dataclass(frozen=True, slots=True)
class SnapshotResult:
    root: Path
    manifest_path: Path
    checksums_path: Path
    manifest: SnapshotManifest


def _created_at_utc(backup_id: str) -> str:
    parsed = dt.datetime.strptime(backup_id, "%Y-%m-%dT%H%M%SZ")
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _checksum_lines(root: Path) -> bytes:
    entries: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {"SHA256SUMS", ".incomplete"}:
            continue
        entries.append((relative, sha256_file(path)))
    return "".join(f"{digest}  {relative}\n" for relative, digest in sorted(entries)).encode(
        "utf-8"
    )


def build_snapshot(config: SnapshotConfig) -> SnapshotResult:
    codex_home = Path(os.path.abspath(config.codex_home))
    snapshot_root = (
        Path(os.path.abspath(config.staging_parent))
        / config.backup_id
        / "codex-history"
    )
    snapshot_root.mkdir(parents=True, exist_ok=False)
    incomplete_marker = snapshot_root / ".incomplete"
    _write_exclusive(incomplete_marker, b"snapshot construction incomplete\n")

    try:
        selection = select_rollouts(codex_home, config.excluded_thread_ids)
        file_records: list[FileRecord] = []
        rollout_records: list[RolloutRecord] = []

        for info in selection.included:
            destination = snapshot_root / Path(*info.relative_path.parts)
            size, digest = stable_copy(
                info.path,
                destination,
                codex_home / info.source_bucket,
            )
            relative = info.relative_path.as_posix()
            file_records.append(
                FileRecord(
                    path=relative,
                    kind="rollout",
                    size=size,
                    sha256=digest,
                    thread_id=info.thread_id,
                    source_bucket=info.source_bucket,
                )
            )
            rollout_records.append(
                RolloutRecord(
                    thread_id=info.thread_id,
                    source_bucket=info.source_bucket,
                    path=relative,
                    sha256=digest,
                )
            )

        for relative_path in selection.selected_attachments:
            source = codex_home / Path(*relative_path.parts)
            destination = snapshot_root / Path(*relative_path.parts)
            size, digest = stable_copy(
                source,
                destination,
                codex_home / "attachments",
            )
            file_records.append(
                FileRecord(
                    path=relative_path.as_posix(),
                    kind="attachment",
                    size=size,
                    sha256=digest,
                    thread_id=None,
                    source_bucket="attachments",
                )
            )

        sorted_files = tuple(sorted(file_records, key=lambda record: record.path))
        sorted_rollouts = tuple(sorted(rollout_records, key=lambda record: record.path))
        exclusions = tuple(
            ExclusionRecord(
                thread_id=thread_id,
                reason="explicitly-excluded-thread",
                matched_rollouts=sum(
                    info.thread_id == thread_id for info in selection.excluded
                ),
            )
            for thread_id in sorted(config.excluded_thread_ids)
        )
        manifest = SnapshotManifest(
            schema_version=1,
            backup_id=config.backup_id,
            created_at_utc=_created_at_utc(config.backup_id),
            source_codex_version=config.codex_version,
            source_platform=config.source_platform,
            source_root="$CODEX_HOME",
            counts=SnapshotCounts(
                session_count=len({record.thread_id for record in sorted_rollouts}),
                rollout_file_count=len(sorted_rollouts),
                attachment_count=sum(record.kind == "attachment" for record in sorted_files),
                total_bytes=sum(record.size for record in sorted_files),
            ),
            files=sorted_files,
            rollouts=sorted_rollouts,
            exclusions=exclusions,
            duplicate_thread_ids=selection.duplicate_thread_ids,
            archive=ArchiveParameters(
                format="tar.zst",
                compression="zstd",
                compression_level=config.compression_level,
                split_threshold_bytes=config.split_threshold_bytes,
            ),
        )
        manifest_path = snapshot_root / "manifest.json"
        _write_exclusive(manifest_path, manifest.to_json_bytes())
        checksums_path = snapshot_root / "SHA256SUMS"
        _write_exclusive(checksums_path, _checksum_lines(snapshot_root))
        incomplete_marker.unlink()
        return SnapshotResult(
            root=snapshot_root,
            manifest_path=manifest_path,
            checksums_path=checksums_path,
            manifest=manifest,
        )
    except Exception:
        if not incomplete_marker.exists():
            _write_exclusive(incomplete_marker, b"snapshot construction incomplete\n")
        raise
