from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path


class ArchiveError(RuntimeError):
    """Raised when deterministic archive creation cannot complete."""


def require_archive_tools() -> tuple[str, str]:
    tar = shutil.which("tar")
    zstd = shutil.which("zstd")
    if tar is None:
        raise ArchiveError("required command not found: tar")
    if zstd is None:
        raise ArchiveError("required command not found: zstd")
    return tar, zstd


def create_tar_zst(
    snapshot_root: Path,
    output_path: Path,
    compression_level: int = 1,
) -> Path:
    tar, zstd = require_archive_tools()
    snapshot_root = snapshot_root.resolve()
    output_path = Path(os.path.abspath(output_path))
    if snapshot_root.name != "codex-history" or not snapshot_root.is_dir():
        raise ArchiveError("snapshot root must be a codex-history directory")
    if output_path.exists():
        raise ArchiveError(f"archive output already exists: {output_path.name}")
    if not 1 <= compression_level <= 22:
        raise ArchiveError("compression level must be between 1 and 22")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.parent / f".{output_path.name}.tmp-{uuid.uuid4().hex}"

    tar_command = [
        tar,
        "--sort=name",
        "--mtime=@0",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        "--format=pax",
        "--pax-option=delete=atime,delete=ctime",
        "-C",
        str(snapshot_root.parent),
        "-cf",
        "-",
        snapshot_root.name,
    ]
    zstd_command = [
        zstd,
        "-q",
        f"-{compression_level}",
        "-T0",
        "-o",
        str(temporary),
        "-",
    ]
    tar_process: subprocess.Popen[bytes] | None = None
    zstd_process: subprocess.Popen[bytes] | None = None
    try:
        tar_process = subprocess.Popen(
            tar_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if tar_process.stdout is None:
            raise ArchiveError("tar stdout pipe was not created")
        zstd_process = subprocess.Popen(
            zstd_command,
            stdin=tar_process.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        tar_process.stdout.close()
        _, zstd_stderr = zstd_process.communicate()
        _, tar_stderr = tar_process.communicate()
        if zstd_process.returncode != 0:
            message = zstd_stderr.decode("utf-8", "replace").strip()
            raise ArchiveError(f"zstd failed with exit {zstd_process.returncode}: {message}")
        if tar_process.returncode != 0:
            message = tar_stderr.decode("utf-8", "replace").strip()
            raise ArchiveError(f"tar failed with exit {tar_process.returncode}: {message}")
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ArchiveError("archive compressor produced no output")
        os.replace(temporary, output_path)
        return output_path
    except OSError as exc:
        raise ArchiveError(f"archive process failed: {exc}") from exc
    finally:
        if zstd_process is not None and zstd_process.poll() is None:
            zstd_process.kill()
            zstd_process.wait()
        if tar_process is not None and tar_process.poll() is None:
            tar_process.kill()
            tar_process.wait()
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def split_archive(
    archive_path: Path,
    max_part_bytes: int = 1_932_735_283,
) -> tuple[Path, ...]:
    archive_path = Path(os.path.abspath(archive_path))
    if max_part_bytes <= 0:
        raise ArchiveError("max_part_bytes must be positive")
    if archive_path.stat().st_size <= max_part_bytes:
        return (archive_path,)

    created: list[Path] = []
    try:
        with archive_path.open("rb") as source:
            index = 1
            while True:
                chunk = source.read(max_part_bytes)
                if not chunk:
                    break
                part = archive_path.with_name(f"{archive_path.name}.part{index:03d}")
                with part.open("xb") as destination:
                    destination.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                created.append(part)
                index += 1
        return tuple(created)
    except Exception:
        for part in created:
            try:
                part.unlink()
            except FileNotFoundError:
                pass
        raise
