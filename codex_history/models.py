from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import uuid
from pathlib import PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
SOURCE_ROOT = "$CODEX_HOME"
DEFAULT_SPLIT_THRESHOLD_BYTES = 1_932_735_283
_BACKUP_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:/")


class ManifestError(ValueError):
    """Raised when manifest data violates the versioned schema."""


def _validate_uuid(value: str, field: str = "thread_id") -> None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ManifestError(f"{field} is not a UUID") from exc
    if str(parsed) != value:
        raise ManifestError(f"{field} must use canonical lowercase UUID form")


def _validate_sha256(value: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ManifestError("sha256 must be 64 lowercase hexadecimal characters")


def _validate_relative_path(value: str) -> None:
    if not value or "\x00" in value or "\\" in value:
        raise ManifestError("path must be a non-empty POSIX path")
    if value.startswith("/") or _WINDOWS_DRIVE_RE.match(value):
        raise ManifestError("path must be relative")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ManifestError("path must not contain empty, dot, or parent segments")
    if PurePosixPath(value).as_posix() != value:
        raise ManifestError("path must be normalized POSIX form")


@dataclasses.dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    kind: str
    size: int
    sha256: str
    thread_id: str | None
    source_bucket: str

    def __post_init__(self) -> None:
        _validate_relative_path(self.path)
        _validate_sha256(self.sha256)
        if self.size < 0:
            raise ManifestError("size must be non-negative")
        if self.kind not in {"rollout", "attachment"}:
            raise ManifestError("kind must be rollout or attachment")
        if self.source_bucket not in {"sessions", "archived_sessions", "attachments"}:
            raise ManifestError("invalid source bucket")
        if not self.path.startswith(f"{self.source_bucket}/"):
            raise ManifestError("path must be rooted in its source bucket")
        if self.kind == "rollout":
            if self.source_bucket not in {"sessions", "archived_sessions"}:
                raise ManifestError("rollout must come from a rollout bucket")
            if self.thread_id is None:
                raise ManifestError("rollout requires thread_id")
        elif self.source_bucket != "attachments":
            raise ManifestError("attachment must come from attachments")
        if self.thread_id is not None:
            _validate_uuid(self.thread_id)


@dataclasses.dataclass(frozen=True, slots=True)
class RolloutRecord:
    thread_id: str
    source_bucket: str
    path: str
    sha256: str

    def __post_init__(self) -> None:
        _validate_uuid(self.thread_id)
        _validate_relative_path(self.path)
        _validate_sha256(self.sha256)
        if self.source_bucket not in {"sessions", "archived_sessions"}:
            raise ManifestError("rollout source bucket is invalid")
        if not self.path.startswith(f"{self.source_bucket}/"):
            raise ManifestError("rollout path must match source bucket")


@dataclasses.dataclass(frozen=True, slots=True)
class ExclusionRecord:
    thread_id: str
    reason: str
    matched_rollouts: int

    def __post_init__(self) -> None:
        _validate_uuid(self.thread_id)
        if not self.reason:
            raise ManifestError("exclusion reason is required")
        if self.matched_rollouts < 0:
            raise ManifestError("matched_rollouts must be non-negative")


@dataclasses.dataclass(frozen=True, slots=True)
class SnapshotCounts:
    session_count: int
    rollout_file_count: int
    attachment_count: int
    total_bytes: int

    def __post_init__(self) -> None:
        if min(
            self.session_count,
            self.rollout_file_count,
            self.attachment_count,
            self.total_bytes,
        ) < 0:
            raise ManifestError("snapshot counts must be non-negative")
        if self.session_count > self.rollout_file_count:
            raise ManifestError("session_count cannot exceed rollout_file_count")


@dataclasses.dataclass(frozen=True, slots=True)
class ArchiveParameters:
    format: str
    compression: str
    compression_level: int
    split_threshold_bytes: int

    def __post_init__(self) -> None:
        if self.format != "tar.zst" or self.compression != "zstd":
            raise ManifestError("archive must use tar.zst with zstd")
        if not 1 <= self.compression_level <= 22:
            raise ManifestError("compression_level must be between 1 and 22")
        if self.split_threshold_bytes <= 0:
            raise ManifestError("split_threshold_bytes must be positive")


@dataclasses.dataclass(frozen=True, slots=True)
class SnapshotManifest:
    schema_version: int
    backup_id: str
    created_at_utc: str
    source_codex_version: str
    source_platform: str
    source_root: str
    counts: SnapshotCounts
    files: tuple[FileRecord, ...]
    rollouts: tuple[RolloutRecord, ...]
    exclusions: tuple[ExclusionRecord, ...]
    duplicate_thread_ids: dict[str, tuple[str, ...]]
    archive: ArchiveParameters

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ManifestError(f"schema_version must be {SCHEMA_VERSION}")
        if self.source_root != SOURCE_ROOT:
            raise ManifestError(f"source_root must be {SOURCE_ROOT}")
        if not _BACKUP_ID_RE.fullmatch(self.backup_id):
            raise ManifestError("backup_id must use YYYY-MM-DDTHHMMSSZ")
        try:
            dt.datetime.strptime(self.created_at_utc, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise ManifestError("created_at_utc must use UTC RFC3339 seconds") from exc
        if not self.source_codex_version or not self.source_platform:
            raise ManifestError("source version and platform are required")
        if len({record.path for record in self.files}) != len(self.files):
            raise ManifestError("file paths must be unique")
        if len({record.path for record in self.rollouts}) != len(self.rollouts):
            raise ManifestError("rollout paths must be unique")
        for thread_id, paths in self.duplicate_thread_ids.items():
            _validate_uuid(thread_id)
            if len(paths) < 2:
                raise ManifestError("duplicate thread groups require at least two paths")
            for path in paths:
                _validate_relative_path(path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "backup_id": self.backup_id,
            "created_at_utc": self.created_at_utc,
            "source_codex_version": self.source_codex_version,
            "source_platform": self.source_platform,
            "source_root": self.source_root,
            "counts": dataclasses.asdict(self.counts),
            "files": [dataclasses.asdict(record) for record in self.files],
            "rollouts": [dataclasses.asdict(record) for record in self.rollouts],
            "exclusions": [dataclasses.asdict(record) for record in self.exclusions],
            "duplicate_thread_ids": {
                thread_id: list(paths)
                for thread_id, paths in sorted(self.duplicate_thread_ids.items())
            },
            "archive": dataclasses.asdict(self.archive),
        }

    def to_json_bytes(self) -> bytes:
        serialized = json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return f"{serialized}\n".encode("utf-8")
