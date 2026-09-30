from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
import uuid
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .models import ManifestError


_UUID_TEXT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_FILENAME_UUID_RE = re.compile(
    rf"-(?P<thread_id>{_UUID_TEXT})(?:_{_UUID_TEXT})?\.jsonl$"
)
_ROLLOUT_BUCKETS = ("sessions", "archived_sessions")


class RolloutFormatError(ManifestError):
    """Raised when a rollout cannot be identified without ambiguity."""


@dataclasses.dataclass(frozen=True, slots=True)
class RolloutInfo:
    path: Path
    relative_path: PurePosixPath
    source_bucket: str
    thread_id: str
    session_id: str
    attachment_paths: frozenset[PurePosixPath]


@dataclasses.dataclass(frozen=True, slots=True)
class RolloutSelection:
    included: tuple[RolloutInfo, ...]
    excluded: tuple[RolloutInfo, ...]
    selected_attachments: tuple[PurePosixPath, ...]
    duplicate_thread_ids: dict[str, tuple[str, ...]]


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RolloutFormatError(f"{field} is not a UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise RolloutFormatError(f"{field} is not a UUID") from exc
    if str(parsed) != value:
        raise RolloutFormatError(f"{field} must use canonical lowercase UUID form")
    return value


def _relative_to_home(path: Path, codex_home: Path) -> Path:
    absolute_home = Path(os.path.abspath(codex_home))
    absolute_path = Path(os.path.abspath(path))
    try:
        return absolute_path.relative_to(absolute_home)
    except ValueError as exc:
        raise RolloutFormatError("rollout path is outside CODEX_HOME") from exc


def _reject_symlink_components(path: Path, codex_home: Path) -> None:
    relative = _relative_to_home(path, codex_home)
    current = Path(os.path.abspath(codex_home))
    if current.is_symlink():
        raise RolloutFormatError("CODEX_HOME must not be a symlink")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RolloutFormatError(f"symlink is not allowed in rollout path: {relative.as_posix()}")


def discover_rollouts(codex_home: Path) -> tuple[Path, ...]:
    home = Path(os.path.abspath(codex_home))
    discovered: list[Path] = []
    for bucket in _ROLLOUT_BUCKETS:
        root = home / bucket
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise RolloutFormatError(f"{bucket} root must be a real directory")
        for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            directory_path = Path(directory)
            for dirname in tuple(dirnames):
                child = directory_path / dirname
                if child.is_symlink():
                    raise RolloutFormatError(
                        f"symlink directory is not allowed below {bucket}: {child.relative_to(root)}"
                    )
            for filename in filenames:
                if not filename.endswith(".jsonl"):
                    continue
                candidate = directory_path / filename
                if candidate.is_symlink():
                    raise RolloutFormatError(
                        f"symlink rollout is not allowed: {candidate.relative_to(root)}"
                    )
                try:
                    mode = candidate.lstat().st_mode
                except FileNotFoundError as exc:
                    raise RolloutFormatError("rollout disappeared during discovery") from exc
                if not stat.S_ISREG(mode):
                    raise RolloutFormatError("rollout candidate must be a regular file")
                discovered.append(candidate)
    return tuple(sorted(discovered, key=lambda item: item.relative_to(home).as_posix()))


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _walk_strings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_strings(nested)


def _attachment_reference(value: str, codex_home: Path) -> PurePosixPath | None:
    if not value or "\x00" in value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        return None
    attachment_root = Path(os.path.abspath(codex_home / "attachments"))
    try:
        absolute_candidate = Path(os.path.abspath(candidate))
        relative = absolute_candidate.relative_to(attachment_root)
    except (OSError, ValueError):
        return None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    return PurePosixPath("attachments", *relative.parts)


def _raw_attachment_references(value: str, codex_home: Path) -> set[PurePosixPath]:
    prefix = str(Path(os.path.abspath(codex_home / "attachments"))) + "/"
    references: set[PurePosixPath] = set()
    start = 0
    while True:
        index = value.find(prefix, start)
        if index < 0:
            return references
        end = index + len(prefix)
        while end < len(value) and value[end] not in {'"', "\\", "\r", "\n", "\x00"}:
            end += 1
        reference = _attachment_reference(value[index:end], codex_home)
        if reference is not None:
            references.add(reference)
        start = max(end, index + 1)


def read_rollout_info(path: Path, codex_home: Path) -> RolloutInfo:
    home = Path(os.path.abspath(codex_home))
    candidate = Path(os.path.abspath(path))
    relative = _relative_to_home(candidate, home)
    if not relative.parts or relative.parts[0] not in _ROLLOUT_BUCKETS:
        raise RolloutFormatError("rollout is not in an allowed source bucket")
    _reject_symlink_components(candidate, home)
    try:
        mode = candidate.lstat().st_mode
    except FileNotFoundError as exc:
        raise RolloutFormatError("rollout file does not exist") from exc
    if not stat.S_ISREG(mode):
        raise RolloutFormatError("rollout must be a regular file")

    filename_match = _FILENAME_UUID_RE.search(candidate.name)
    filename_thread_id = (
        _canonical_uuid(filename_match.group("thread_id"), "filename thread id")
        if filename_match
        else None
    )
    metadata_thread_id: str | None = None
    metadata_session_id: str | None = None
    attachments: set[PurePosixPath] = set()

    with candidate.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                if metadata_thread_id is not None:
                    attachments.update(_raw_attachment_references(raw_line, home))
                    continue
                raise RolloutFormatError(f"invalid JSON at line {line_number}") from exc
            if not isinstance(record, dict):
                raise RolloutFormatError(f"JSONL record at line {line_number} is not an object")
            if record.get("type") == "session_meta" and metadata_thread_id is None:
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    raise RolloutFormatError("session_meta payload is not an object")
                session_id = payload.get("session_id")
                legacy_id = payload.get("id")
                if session_id is None and legacy_id is None:
                    raise RolloutFormatError("session_meta has no thread identity")
                canonical_session = (
                    _canonical_uuid(session_id, "payload.session_id")
                    if session_id is not None
                    else None
                )
                canonical_legacy = (
                    _canonical_uuid(legacy_id, "payload.id") if legacy_id is not None else None
                )
                found_id = canonical_legacy or canonical_session
                found_session_id = canonical_session or found_id
                metadata_thread_id = found_id
                metadata_session_id = found_session_id
            for text in _walk_strings(record):
                reference = _attachment_reference(text, home)
                if reference is not None:
                    attachments.add(reference)

    if metadata_thread_id is None:
        raise RolloutFormatError("rollout has no session_meta record")
    if filename_thread_id is not None and filename_thread_id != metadata_thread_id:
        raise RolloutFormatError("filename and metadata thread id mismatch")

    return RolloutInfo(
        path=candidate,
        relative_path=PurePosixPath(*relative.parts),
        source_bucket=relative.parts[0],
        thread_id=metadata_thread_id,
        session_id=metadata_session_id or metadata_thread_id,
        attachment_paths=frozenset(attachments),
    )


def select_rollouts(
    codex_home: Path,
    excluded_thread_ids: frozenset[str],
) -> RolloutSelection:
    excluded_ids = {
        _canonical_uuid(thread_id, "excluded thread id") for thread_id in excluded_thread_ids
    }
    included: list[RolloutInfo] = []
    excluded: list[RolloutInfo] = []
    for path in discover_rollouts(codex_home):
        info = read_rollout_info(path, codex_home)
        if info.thread_id in excluded_ids or info.session_id in excluded_ids:
            excluded.append(info)
        else:
            included.append(info)

    included_references = {
        reference for info in included for reference in info.attachment_paths
    }
    excluded_references = {
        reference for info in excluded for reference in info.attachment_paths
    }
    selected_attachments = tuple(sorted(included_references - excluded_references))

    paths_by_thread: defaultdict[str, list[str]] = defaultdict(list)
    for info in included:
        paths_by_thread[info.thread_id].append(info.relative_path.as_posix())
    duplicate_thread_ids = {
        thread_id: tuple(sorted(paths))
        for thread_id, paths in sorted(paths_by_thread.items())
        if len(paths) > 1
    }
    return RolloutSelection(
        included=tuple(included),
        excluded=tuple(excluded),
        selected_attachments=selected_attachments,
        duplicate_thread_ids=duplicate_thread_ids,
    )
