from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator, Sequence

from .models import (
    ArchiveParameters,
    ExclusionRecord,
    FileRecord,
    ManifestError,
    RolloutRecord,
    SnapshotCounts,
    SnapshotManifest,
)
from .safeio import sha256_file


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PART_RE = re.compile(r"^(?P<base>.+\.tar\.zst)\.part(?P<index>\d{3})$")


class UnsafeArchiveError(ValueError):
    """Raised before extraction when a tar member is unsafe."""


class IntegrityError(ValueError):
    """Raised when verified bytes do not match their declared metadata."""


@dataclasses.dataclass(frozen=True, slots=True)
class VerificationReport:
    backup_id: str
    file_count: int
    total_bytes: int
    excluded_thread_ids: tuple[str, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class _MemberRecord:
    name: str
    is_directory: bool
    size: int


def _safe_member_path(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise UnsafeArchiveError("archive member path is invalid")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafeArchiveError("archive member path is absolute or traverses parents")
    if not path.parts or path.parts[0] != "codex-history":
        raise UnsafeArchiveError("archive has wrong root; expected codex-history")
    return path


def _zstd_tar_members(archive_path: Path) -> Iterator[tuple[tarfile.TarInfo, tarfile.TarFile]]:
    zstd = shutil.which("zstd")
    if zstd is None:
        raise IntegrityError("required command not found: zstd")
    process = subprocess.Popen(
        [zstd, "-q", "-d", "-c", "--", str(archive_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None:
        process.kill()
        process.wait()
        raise IntegrityError("zstd stdout pipe was not created")
    archive: tarfile.TarFile | None = None
    try:
        archive = tarfile.open(fileobj=process.stdout, mode="r|")
        for member in archive:
            yield member, archive
        archive.close()
        archive = None
        process.stdout.close()
        stderr = process.stderr.read() if process.stderr is not None else b""
        return_code = process.wait()
        if return_code != 0:
            message = stderr.decode("utf-8", "replace").strip()
            raise IntegrityError(f"zstd stream failed with exit {return_code}: {message}")
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise IntegrityError(f"invalid or truncated tar.zst stream: {exc}") from exc
    finally:
        if archive is not None:
            archive.close()
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stderr is not None and not process.stderr.closed:
            process.stderr.close()


def _manifest_from_dict(value: Any) -> SnapshotManifest:
    if not isinstance(value, dict):
        raise IntegrityError("manifest root must be an object")
    try:
        duplicate_groups = value["duplicate_thread_ids"]
        if not isinstance(duplicate_groups, dict):
            raise TypeError("duplicate_thread_ids must be an object")
        return SnapshotManifest(
            schema_version=value["schema_version"],
            backup_id=value["backup_id"],
            created_at_utc=value["created_at_utc"],
            source_codex_version=value["source_codex_version"],
            source_platform=value["source_platform"],
            source_root=value["source_root"],
            counts=SnapshotCounts(**value["counts"]),
            files=tuple(FileRecord(**record) for record in value["files"]),
            rollouts=tuple(RolloutRecord(**record) for record in value["rollouts"]),
            exclusions=tuple(ExclusionRecord(**record) for record in value["exclusions"]),
            duplicate_thread_ids={
                thread_id: tuple(paths) for thread_id, paths in duplicate_groups.items()
            },
            archive=ArchiveParameters(**value["archive"]),
        )
    except (KeyError, TypeError, ManifestError) as exc:
        raise IntegrityError(f"manifest schema is invalid: {exc}") from exc


def _scan_archive(archive_path: Path) -> tuple[tuple[_MemberRecord, ...], bytes, SnapshotManifest]:
    seen: set[str] = set()
    members: list[_MemberRecord] = []
    manifest_bytes: bytes | None = None
    for member, archive in _zstd_tar_members(archive_path):
        path = _safe_member_path(member.name)
        normalized = path.as_posix()
        if normalized in seen:
            raise UnsafeArchiveError(f"duplicate archive member: {normalized}")
        seen.add(normalized)
        if not (member.isdir() or member.isreg()):
            raise UnsafeArchiveError(f"unsupported archive member type: {normalized}")
        members.append(
            _MemberRecord(
                name=normalized,
                is_directory=member.isdir(),
                size=member.size,
            )
        )
        if normalized == "codex-history/manifest.json":
            if member.size > 32 * 1024 * 1024:
                raise IntegrityError("manifest is unreasonably large")
            stream = archive.extractfile(member)
            if stream is None:
                raise IntegrityError("manifest could not be read")
            manifest_bytes = stream.read()

    if manifest_bytes is None:
        raise IntegrityError("archive is missing manifest.json")
    try:
        manifest_value = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("manifest is not valid UTF-8 JSON") from exc
    manifest = _manifest_from_dict(manifest_value)

    regular_members = {
        record.name.removeprefix("codex-history/"): record.size
        for record in members
        if not record.is_directory and record.name != "codex-history"
    }
    declared_data = {record.path: record.size for record in manifest.files}
    expected_regular = set(declared_data) | {"manifest.json", "SHA256SUMS"}
    actual_regular = set(regular_members)
    undeclared = actual_regular - expected_regular
    missing = expected_regular - actual_regular
    if undeclared:
        raise IntegrityError(f"undeclared archive file: {sorted(undeclared)[0]}")
    if missing:
        raise IntegrityError(f"missing declared archive file: {sorted(missing)[0]}")
    for path, expected_size in declared_data.items():
        if regular_members[path] != expected_size:
            raise IntegrityError(f"declared size/digest mismatch: {path}")
    return tuple(members), manifest_bytes, manifest


def _extract_second_pass(
    archive_path: Path,
    destination: Path,
    expected_members: tuple[_MemberRecord, ...],
) -> Path:
    destination = Path(os.path.abspath(destination))
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise UnsafeArchiveError("extraction destination must be empty")
    root = destination / "codex-history"
    observed: list[_MemberRecord] = []
    try:
        for member, archive in _zstd_tar_members(archive_path):
            safe_path = _safe_member_path(member.name)
            normalized = safe_path.as_posix()
            if not (member.isdir() or member.isreg()):
                raise UnsafeArchiveError(f"unsupported archive member type: {normalized}")
            observed.append(
                _MemberRecord(normalized, member.isdir(), member.size)
            )
            output = destination.joinpath(*safe_path.parts)
            if member.isdir():
                output.mkdir(parents=True, exist_ok=True)
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                raise IntegrityError(f"archive member could not be read: {normalized}")
            copied = 0
            with output.open("xb") as target:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    copied += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            if copied != member.size:
                raise IntegrityError(f"archive member was truncated: {normalized}")
        if tuple(observed) != expected_members:
            raise IntegrityError("archive changed between validation and extraction")
        return root
    except Exception:
        if root.exists():
            shutil.rmtree(root)
        raise


def safe_extract_tar_zst(archive_path: Path, destination: Path) -> Path:
    archive_path = Path(os.path.abspath(archive_path))
    before = sha256_file(archive_path)
    members, _, _ = _scan_archive(archive_path)
    if sha256_file(archive_path) != before:
        raise IntegrityError("archive changed during validation")
    root = _extract_second_pass(archive_path, destination, members)
    if sha256_file(archive_path) != before:
        shutil.rmtree(root)
        raise IntegrityError("archive changed during extraction")
    return root


def _parse_sha256sums(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise IntegrityError(f"checksum file is not UTF-8: {path.name}") from exc
    for line in lines:
        try:
            digest, filename = line.split("  ", 1)
        except ValueError as exc:
            raise IntegrityError(f"invalid checksum line in {path.name}") from exc
        if not _SHA256_RE.fullmatch(digest):
            raise IntegrityError(f"invalid SHA-256 in {path.name}")
        safe = PurePosixPath(filename)
        if (
            not filename
            or safe.is_absolute()
            or any(part in {"", ".", ".."} for part in safe.parts)
            or "\\" in filename
        ):
            raise IntegrityError(f"unsafe checksum path in {path.name}")
        if filename in entries:
            raise IntegrityError(f"duplicate checksum path in {path.name}")
        entries[filename] = digest
    return entries


def _load_manifest(path: Path) -> tuple[bytes, SnapshotManifest]:
    data = path.read_bytes()
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("manifest is not valid UTF-8 JSON") from exc
    manifest = _manifest_from_dict(value)
    if manifest.to_json_bytes() != data:
        raise IntegrityError("manifest is not canonical JSON")
    return data, manifest


def verify_extracted_snapshot(
    root: Path,
    public_manifest: Path | None = None,
) -> VerificationReport:
    root = Path(os.path.abspath(root))
    if root.name != "codex-history" or root.is_symlink() or not root.is_dir():
        raise IntegrityError("verified root must be a real codex-history directory")
    internal_bytes, manifest = _load_manifest(root / "manifest.json")
    if public_manifest is not None and Path(public_manifest).read_bytes() != internal_bytes:
        raise IntegrityError("public manifest does not match internal manifest")

    actual_data: dict[str, Path] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode) or path.is_symlink():
            raise IntegrityError(f"snapshot contains a non-regular path: {relative}")
        if relative not in {"manifest.json", "SHA256SUMS"}:
            actual_data[relative] = path

    declared = {record.path: record for record in manifest.files}
    undeclared = set(actual_data) - set(declared)
    missing = set(declared) - set(actual_data)
    if undeclared:
        raise IntegrityError(f"undeclared extracted file: {sorted(undeclared)[0]}")
    if missing:
        raise IntegrityError(f"missing declared extracted file: {sorted(missing)[0]}")
    for relative, record in declared.items():
        path = actual_data[relative]
        if path.stat().st_size != record.size or sha256_file(path) != record.sha256:
            raise IntegrityError(f"file digest or size mismatch: {relative}")

    internal_checksums = _parse_sha256sums(root / "SHA256SUMS")
    checksum_targets = {"manifest.json", *declared.keys()}
    if set(internal_checksums) != checksum_targets:
        missing_checksums = checksum_targets - set(internal_checksums)
        extra_checksums = set(internal_checksums) - checksum_targets
        if missing_checksums:
            raise IntegrityError(f"missing internal checksum: {sorted(missing_checksums)[0]}")
        raise IntegrityError(f"undeclared internal checksum: {sorted(extra_checksums)[0]}")
    for relative, expected in internal_checksums.items():
        if sha256_file(root / relative) != expected:
            raise IntegrityError(f"internal digest mismatch: {relative}")

    rollout_files = {record.path: record for record in manifest.files if record.kind == "rollout"}
    rollout_records = {record.path: record for record in manifest.rollouts}
    if set(rollout_files) != set(rollout_records):
        raise IntegrityError("rollout manifest records do not match rollout files")
    for path, rollout in rollout_records.items():
        file_record = rollout_files[path]
        if (
            rollout.thread_id != file_record.thread_id
            or rollout.sha256 != file_record.sha256
            or rollout.source_bucket != file_record.source_bucket
        ):
            raise IntegrityError(f"rollout record mismatch: {path}")

    excluded_ids = tuple(sorted(record.thread_id for record in manifest.exclusions))
    for excluded_id in excluded_ids:
        if any(record.thread_id == excluded_id for record in manifest.rollouts):
            raise IntegrityError(f"excluded thread appears in rollout records: {excluded_id}")
        if any(record.thread_id == excluded_id for record in manifest.files):
            raise IntegrityError(f"excluded thread appears in file records: {excluded_id}")
        if any(excluded_id in record.path for record in manifest.files):
            raise IntegrityError(f"excluded thread appears in archive path: {excluded_id}")

    unique_sessions = len({record.thread_id for record in manifest.rollouts})
    attachment_count = sum(record.kind == "attachment" for record in manifest.files)
    total_bytes = sum(record.size for record in manifest.files)
    if manifest.counts != SnapshotCounts(
        session_count=unique_sessions,
        rollout_file_count=len(manifest.rollouts),
        attachment_count=attachment_count,
        total_bytes=total_bytes,
    ):
        raise IntegrityError("manifest counts do not match declared files")
    return VerificationReport(
        backup_id=manifest.backup_id,
        file_count=len(manifest.files),
        total_bytes=total_bytes,
        excluded_thread_ids=excluded_ids,
    )


def _ordered_archive_parts(paths: Sequence[Path]) -> tuple[Path, ...]:
    if not paths:
        raise IntegrityError("at least one archive asset is required")
    absolute = tuple(Path(os.path.abspath(path)) for path in paths)
    if len(absolute) == 1 and _PART_RE.match(absolute[0].name) is None:
        return absolute
    parsed = []
    for path in absolute:
        match = _PART_RE.match(path.name)
        if match is None:
            raise IntegrityError("cannot mix split and unsplit archive assets")
        parsed.append((match.group("base"), int(match.group("index")), path))
    bases = {base for base, _, _ in parsed}
    if len(bases) != 1:
        raise IntegrityError("archive parts do not share one base name")
    parsed.sort(key=lambda item: item[1])
    if [index for _, index, _ in parsed] != list(range(1, len(parsed) + 1)):
        raise IntegrityError("archive part sequence is not contiguous")
    return tuple(path for _, _, path in parsed)


def _verify_outer_checksums(paths: tuple[Path, ...], checksum_path: Path) -> None:
    declared = _parse_sha256sums(checksum_path)
    expected_names = {path.name for path in paths}
    if set(declared) != expected_names:
        raise IntegrityError("outer checksum asset list does not match archive assets")
    for path in paths:
        if not path.is_file() or sha256_file(path) != declared[path.name]:
            raise IntegrityError(f"outer checksum mismatch: {path.name}")


def verify_archive(
    archive_paths: Sequence[Path],
    outer_checksum: Path,
    public_manifest: Path,
    destination: Path,
) -> VerificationReport:
    parts = _ordered_archive_parts(archive_paths)
    _verify_outer_checksums(parts, outer_checksum)
    destination = Path(os.path.abspath(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_parent = destination.parent
    with tempfile.TemporaryDirectory(
        prefix=".codex-history-reassemble-", dir=temporary_parent
    ) as temporary_name:
        if len(parts) == 1:
            logical_archive = parts[0]
        else:
            logical_archive = Path(temporary_name) / _PART_RE.match(parts[0].name).group("base")  # type: ignore[union-attr]
            with logical_archive.open("xb") as combined:
                for part in parts:
                    with part.open("rb") as stream:
                        shutil.copyfileobj(stream, combined, length=1024 * 1024)
        try:
            root = safe_extract_tar_zst(logical_archive, destination)
            return verify_extracted_snapshot(root, public_manifest)
        except Exception:
            root = destination / "codex-history"
            if root.exists():
                shutil.rmtree(root)
            raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify and safely extract a Codex backup.")
    parser.add_argument("--archive", required=True, action="append", type=Path)
    parser.add_argument("--checksum", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = verify_archive(
            args.archive,
            args.checksum,
            args.manifest,
            args.destination,
        )
        print(
            json.dumps(
                {
                    "backup_id": report.backup_id,
                    "file_count": report.file_count,
                    "total_bytes": report.total_bytes,
                    "excluded_thread_ids": report.excluded_thread_ids,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (IntegrityError, UnsafeArchiveError, OSError, ValueError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
