from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import BinaryIO


COPY_CHUNK_SIZE = 1024 * 1024
FileIdentity = tuple[int, int, int, int]


class PathSafetyError(ValueError):
    """Raised when a source path is outside the allowed regular-file tree."""


class SourceChangedError(RuntimeError):
    """Raised when a source changes while it is being copied."""


def _identity_from_stat(result: os.stat_result) -> FileIdentity:
    return (result.st_dev, result.st_ino, result.st_size, result.st_mtime_ns)


def _path_identity(path: Path) -> FileIdentity:
    return _identity_from_stat(path.lstat())


def _lexical_relative(path: Path, root: Path) -> Path:
    absolute_path = Path(os.path.abspath(path))
    absolute_root = Path(os.path.abspath(root))
    try:
        return absolute_path.relative_to(absolute_root)
    except ValueError as exc:
        raise PathSafetyError("source is outside allowed root") from exc


def _reject_symlink_components(path: Path, root: Path) -> None:
    relative = _lexical_relative(path, root)
    current = Path(os.path.abspath(root))
    if current.is_symlink():
        raise PathSafetyError("allowed root must not be a symlink")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PathSafetyError("source path contains a symlink")


def _hash_stream(stream: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(COPY_CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        _, digest = _hash_stream(stream)
    return digest


def stable_copy(source: Path, destination: Path, allowed_root: Path) -> tuple[int, str]:
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    allowed_root = Path(os.path.abspath(allowed_root))
    _lexical_relative(source, allowed_root)
    _reject_symlink_components(source, allowed_root)

    try:
        source_stat = source.lstat()
    except FileNotFoundError as exc:
        raise PathSafetyError("source does not exist") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise PathSafetyError("source must be a regular file")
    before = _path_identity(source)

    open_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    source_fd = os.open(source, open_flags)
    destination_created = False
    try:
        opened_before = _identity_from_stat(os.fstat(source_fd))
        if opened_before != before:
            raise SourceChangedError("source changed while opening")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(source_fd, "rb", closefd=False) as source_stream:
            with destination.open("xb") as destination_stream:
                destination_created = True
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = source_stream.read(COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    destination_stream.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())

        opened_after = _identity_from_stat(os.fstat(source_fd))
        after = _path_identity(source)
        if opened_after != before or after != before:
            raise SourceChangedError("source changed while copying")
        digest_hex = digest.hexdigest()
        if size != before[2] or sha256_file(destination) != digest_hex:
            raise SourceChangedError("copied bytes do not match the stable source")
        return size, digest_hex
    except Exception:
        if destination_created:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(source_fd)
