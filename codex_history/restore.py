from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Literal, Sequence

from .safeio import sha256_file, stable_copy
from .verify import IntegrityError, verify_extracted_snapshot


RestoreStatus = Literal["add", "skip-identical", "conflict"]


class RestoreConflictError(RuntimeError):
    """Raised when a restore cannot proceed without overwriting data."""


@dataclasses.dataclass(frozen=True, slots=True)
class RestoreAction:
    source: Path
    destination: Path
    relative_path: str
    sha256: str
    status: RestoreStatus


@dataclasses.dataclass(frozen=True, slots=True)
class RestorePlan:
    backup_id: str
    actions: tuple[RestoreAction, ...]
    semantic_conflicts: tuple[str, ...]
    target_codex_home: Path
    snapshot_root: Path

    @property
    def has_conflicts(self) -> bool:
        return bool(self.semantic_conflicts) or any(
            action.status == "conflict" for action in self.actions
        )


@dataclasses.dataclass(frozen=True, slots=True)
class RollbackReport:
    removed: tuple[Path, ...]
    refused: tuple[Path, ...]
    missing: tuple[Path, ...]


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _require_real_directory(path: Path, label: str) -> Path:
    absolute = _absolute(path)
    try:
        mode = absolute.lstat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"{label} must already exist") from exc
    if absolute.is_symlink() or not stat.S_ISDIR(mode):
        raise ValueError(f"{label} must be a real directory")
    return absolute


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("restore path must be a normalized relative POSIX path")
    return path


def _has_unsafe_existing_component(root: Path, relative: PurePosixPath) -> bool:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return False
        if current.is_symlink() or not stat.S_ISDIR(mode):
            return True
    return False


def _destination_status(destination: Path, expected_digest: str, root: Path, relative: PurePosixPath) -> RestoreStatus:
    if _has_unsafe_existing_component(root, relative):
        return "conflict"
    try:
        mode = destination.lstat().st_mode
    except FileNotFoundError:
        return "add"
    if destination.is_symlink() or not stat.S_ISREG(mode):
        return "conflict"
    return "skip-identical" if sha256_file(destination) == expected_digest else "conflict"


def _manifest_value(snapshot_root: Path) -> dict[str, object]:
    try:
        value = json.loads((snapshot_root / "manifest.json").read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("could not load verified restore manifest") from exc
    if not isinstance(value, dict):
        raise IntegrityError("restore manifest root is not an object")
    return value


def _target_rollout_digests(target_home: Path) -> dict[str, set[str]]:
    from .rollouts import discover_rollouts, read_rollout_info

    result: dict[str, set[str]] = {}
    for path in discover_rollouts(target_home):
        info = read_rollout_info(path, target_home)
        result.setdefault(info.thread_id, set()).add(sha256_file(path))
    return result


def plan_restore(snapshot_root: Path, target_codex_home: Path) -> RestorePlan:
    root = _require_real_directory(snapshot_root, "snapshot root")
    target = _require_real_directory(target_codex_home, "target CODEX_HOME")
    verify_extracted_snapshot(root)
    manifest = _manifest_value(root)
    raw_files = manifest.get("files")
    raw_rollouts = manifest.get("rollouts")
    duplicate_groups = manifest.get("duplicate_thread_ids")
    backup_id = manifest.get("backup_id")
    if (
        not isinstance(raw_files, list)
        or not isinstance(raw_rollouts, list)
        or not isinstance(duplicate_groups, dict)
        or not isinstance(backup_id, str)
    ):
        raise IntegrityError("restore manifest has invalid collections")

    actions: list[RestoreAction] = []
    for record in raw_files:
        if not isinstance(record, dict):
            raise IntegrityError("restore file record is not an object")
        relative_text = record.get("path")
        digest = record.get("sha256")
        if not isinstance(relative_text, str) or not isinstance(digest, str):
            raise IntegrityError("restore file record is incomplete")
        relative = _safe_relative(relative_text)
        source = root.joinpath(*relative.parts)
        destination = target.joinpath(*relative.parts)
        actions.append(
            RestoreAction(
                source=source,
                destination=destination,
                relative_path=relative.as_posix(),
                sha256=digest,
                status=_destination_status(destination, digest, target, relative),
            )
        )

    backup_rollouts: dict[str, set[str]] = {}
    for record in raw_rollouts:
        if not isinstance(record, dict):
            raise IntegrityError("restore rollout record is not an object")
        thread_id = record.get("thread_id")
        digest = record.get("sha256")
        if not isinstance(thread_id, str) or not isinstance(digest, str):
            raise IntegrityError("restore rollout record is incomplete")
        backup_rollouts.setdefault(thread_id, set()).add(digest)

    semantic_conflicts: set[str] = set()
    target_rollouts = _target_rollout_digests(target)
    for thread_id, backup_digests in backup_rollouts.items():
        existing = target_rollouts.get(thread_id)
        if existing is not None and not existing.issubset(backup_digests):
            semantic_conflicts.add(thread_id)

    return RestorePlan(
        backup_id=backup_id,
        actions=tuple(sorted(actions, key=lambda action: action.relative_path)),
        semantic_conflicts=tuple(sorted(semantic_conflicts)),
        target_codex_home=target,
        snapshot_root=root,
    )


def _ensure_parent_directories(root: Path, relative: PurePosixPath) -> tuple[str, ...]:
    current = root
    created: list[str] = []
    for index, part in enumerate(relative.parts[:-1], start=1):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            try:
                current.mkdir()
            except FileExistsError:
                mode = current.lstat().st_mode
                if current.is_symlink() or not stat.S_ISDIR(mode):
                    raise RestoreConflictError("restore parent became unsafe")
            else:
                created.append(PurePosixPath(*relative.parts[:index]).as_posix())
                continue
        if current.is_symlink() or not stat.S_ISDIR(mode):
            raise RestoreConflictError("restore parent is not a real directory")
    return tuple(created)


def _journal_payload(plan: RestorePlan, additions: list[dict[str, str]], directories: set[str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "backup_id": plan.backup_id,
        "target_codex_home": str(plan.target_codex_home),
        "additions": additions,
        "created_directories": sorted(directories),
    }


def _write_new_journal(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _replace_journal(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _preflight_apply(plan: RestorePlan) -> None:
    if plan.has_conflicts:
        raise RestoreConflictError("restore plan contains conflicts")
    _require_real_directory(plan.target_codex_home, "target CODEX_HOME")
    for action in plan.actions:
        relative = _safe_relative(action.relative_path)
        current = _destination_status(
            action.destination, action.sha256, plan.target_codex_home, relative
        )
        if current != action.status:
            raise RestoreConflictError(
                f"restore destination changed after dry-run: {action.relative_path}"
            )
        if sha256_file(action.source) != action.sha256:
            raise RestoreConflictError(
                f"restore source changed after verification: {action.relative_path}"
            )


def apply_restore(plan: RestorePlan, journal_path: Path) -> Path:
    journal = _absolute(journal_path)
    _preflight_apply(plan)
    additions: list[dict[str, str]] = []
    directories: set[str] = set()
    _write_new_journal(journal, _journal_payload(plan, additions, directories))
    installed: list[RestoreAction] = []
    try:
        for action in plan.actions:
            if action.status == "skip-identical":
                continue
            relative = _safe_relative(action.relative_path)
            directories.update(_ensure_parent_directories(plan.target_codex_home, relative))
            try:
                size, digest = stable_copy(
                    action.source, action.destination, plan.snapshot_root
                )
            except FileExistsError as exc:
                raise RestoreConflictError(
                    f"restore destination appeared after dry-run: {action.relative_path}"
                ) from exc
            if digest != action.sha256:
                action.destination.unlink(missing_ok=True)
                raise RestoreConflictError(
                    f"installed digest mismatch: {action.relative_path}"
                )
            installed.append(action)
            additions.append(
                {
                    "relative_path": action.relative_path,
                    "sha256": digest,
                    "size": str(size),
                }
            )
            _replace_journal(journal, _journal_payload(plan, additions, directories))
        return journal
    except Exception:
        for action in reversed(installed):
            try:
                if sha256_file(action.destination) == action.sha256:
                    action.destination.unlink()
            except (FileNotFoundError, OSError):
                pass
        journal.unlink(missing_ok=True)
        raise


def _load_journal(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("restore journal is not readable JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("unsupported restore journal")
    return value


def rollback_restore(journal_path: Path) -> RollbackReport:
    value = _load_journal(_absolute(journal_path))
    target_text = value.get("target_codex_home")
    additions = value.get("additions")
    directory_values = value.get("created_directories")
    if not isinstance(target_text, str) or not isinstance(additions, list) or not isinstance(directory_values, list):
        raise ValueError("restore journal is incomplete")
    target = _require_real_directory(Path(target_text), "journal target CODEX_HOME")
    removed: list[Path] = []
    refused: list[Path] = []
    missing: list[Path] = []
    for entry in reversed(additions):
        if not isinstance(entry, dict):
            raise ValueError("restore journal addition is invalid")
        relative_text = entry.get("relative_path")
        digest = entry.get("sha256")
        if not isinstance(relative_text, str) or not isinstance(digest, str):
            raise ValueError("restore journal addition is incomplete")
        relative = _safe_relative(relative_text)
        destination = target.joinpath(*relative.parts)
        if _has_unsafe_existing_component(target, relative):
            refused.append(destination)
            continue
        try:
            mode = destination.lstat().st_mode
        except FileNotFoundError:
            missing.append(destination)
            continue
        if destination.is_symlink() or not stat.S_ISREG(mode) or sha256_file(destination) != digest:
            refused.append(destination)
            continue
        destination.unlink()
        removed.append(destination)

    safe_directories: list[tuple[int, Path]] = []
    for value_path in directory_values:
        if not isinstance(value_path, str):
            raise ValueError("restore journal directory is invalid")
        relative = _safe_relative(value_path)
        safe_directories.append((len(relative.parts), target.joinpath(*relative.parts)))
    for _, directory in sorted(safe_directories, reverse=True):
        try:
            if not directory.is_symlink():
                directory.rmdir()
        except (FileNotFoundError, OSError):
            pass
    return RollbackReport(tuple(removed), tuple(refused), tuple(missing))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or safely apply a Codex history restore (dry-run by default)."
    )
    parser.add_argument("--snapshot-root", type=Path)
    parser.add_argument("--target-codex-home", type=Path)
    parser.add_argument("--apply", action="store_true", help="apply a conflict-free plan")
    parser.add_argument("--journal", type=Path, help="required with --apply")
    parser.add_argument("--rollback", type=Path, metavar="JOURNAL")
    return parser


def _plan_summary(plan: RestorePlan) -> dict[str, object]:
    counts = {
        status: sum(action.status == status for action in plan.actions)
        for status in ("add", "skip-identical", "conflict")
    }
    return {
        "backup_id": plan.backup_id,
        "dry_run": True,
        "counts": counts,
        "semantic_conflicts": plan.semantic_conflicts,
        "has_conflicts": plan.has_conflicts,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.rollback is not None:
            if args.snapshot_root or args.target_codex_home or args.apply or args.journal:
                parser.error("--rollback cannot be combined with restore planning options")
            report = rollback_restore(args.rollback)
            print(json.dumps(dataclasses.asdict(report), default=str, sort_keys=True))
            return 0
        if args.snapshot_root is None or args.target_codex_home is None:
            parser.error("--snapshot-root and --target-codex-home are required")
        if args.journal is not None and not args.apply:
            parser.error("--journal requires --apply")
        if args.apply and args.journal is None:
            parser.error("--apply requires --journal")
        plan = plan_restore(args.snapshot_root, args.target_codex_home)
        summary = _plan_summary(plan)
        if not args.apply:
            print(
                "Dry-run: "
                f"{summary['counts']['add']} add, "  # type: ignore[index]
                f"{summary['counts']['skip-identical']} identical, "  # type: ignore[index]
                f"{summary['counts']['conflict']} path conflicts"  # type: ignore[index]
            )
            print(json.dumps(summary, sort_keys=True))
            return 2 if plan.has_conflicts else 0
        apply_restore(plan, args.journal)
        summary["dry_run"] = False
        summary["journal"] = str(_absolute(args.journal))
        print(json.dumps(summary, sort_keys=True))
        return 0
    except (IntegrityError, RestoreConflictError, OSError, ValueError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
