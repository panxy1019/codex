# Codex History Release Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一套只读导出、强校验、无覆盖恢复和 GitHub Release 发布工具，并首次发布除指定活动会话外的全部本地 Codex 历史。

**Architecture:** Python 标准库负责 rollout 解析、稳定复制、manifest、验证和恢复；GNU tar 与 zstd 负责流式确定性归档；GitHub CLI 负责先创建 draft Release、下载回环验证，再转为正式 Release。导出严格从允许列表枚举，不复制 Codex 数据库，恢复严格只新增不覆盖。

**Tech Stack:** Python 3.11+ 标准库、`unittest`、GNU tar 1.34+、zstd 1.5+、Git、GitHub CLI 2.46+

**Spec:** `docs/superpowers/specs/2026-09-29-codex-history-release-backup-design.md`

## Global Constraints

- 源 `CODEX_HOME` 只读；导出器不得在其中创建、修改、移动或删除任何文件。
- 首次快照必须排除线程 `01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b` 的所有 rollout、专属附件和公开元数据。
- 只允许 `sessions/**/*.jsonl`、`archived_sessions/**/*.jsonl` 和被纳入 rollout 引用的 `attachments/**` 进入快照。
- `auth.json`、`config.toml*`、`installation_id`、全局状态、SQLite/WAL/SHM、日志、缓存、浏览器、IPC、插件、技能和临时状态永不进入快照。
- Release 保持 Public、资产不加密；不设置对话内容秘密扫描闸门，也不改写或脱敏对话正文。
- Git 只保存源码、文档、公开 manifest 和外层 SHA-256；`.tar.zst` 只作为 Release 资产。
- 每个源文件复制前后比较设备号、inode、大小和纳秒 mtime；不稳定即整次导出失败。
- 归档和恢复拒绝绝对路径、`..`、符号链接、硬链接、设备文件、FIFO 和 socket。
- 恢复默认 dry-run；实际恢复只新增、相同摘要跳过、不同摘要或同 UUID 异内容冲突，不写 SQLite。
- 全部命令不得打印提示词、回答或附件正文，只输出路径、计数、大小、摘要和错误类别。

## Implementation Assumptions

- 源机器是当前 Linux 环境；目标机器必须提供 Python 3.11+、tar、zstd 和一个显式目标 `CODEX_HOME`，不假设目标用户名或主目录相同。
- rollout 自身身份以已观察到的 `session_meta.payload.id` 为主，旧格式缺失时使用 `payload.session_id`；`session_id` 可合法指向父/根会话，排除规则必须同时匹配两者；任何未知或与文件名矛盾的格式均停止导出。
- v1 快照保存 rollout 原始字节，不重写其中的绝对路径；附件作为可校验补充数据恢复，Codex 对跨机器附件链接的 UI 行为属于烟雾验证范围。
- 缺失或变化中的已引用附件会使本次导出失败，不会生成一个被描述为完整的部分快照。
- 首次压缩资产预计低于 1.8 GiB；分卷仍实现并测试，以防执行时数据增长。
- Codex 没有公开承诺跨机器导入 API，因此“能被 `codex resume` 发现”必须由隔离环境和目标电脑验证，不能仅凭文件校验宣称。

## Review Focus

- 恶意归档成员（路径穿越、绝对路径、链接或特殊文件）必须在写出 staging 之前被拒绝；Task 5 固定该行为。
- 文件名 UUID 与 rollout 自身 `session_meta.payload.id`（旧格式为 `session_id`）不一致或 metadata 损坏时必须 fail-closed；Task 2 固定该行为。
- 源文件在复制过程中替换、增长或改变 mtime 时必须删除副本并使整个导出失败；Task 3 固定该行为。
- 恢复目标在 dry-run 后被并发创建，或已有同 UUID 异内容 rollout 时必须拒绝覆盖；Task 6 固定该行为。
- GitHub draft Release 的远端资产与已提交 manifest/checksum 不一致或下载中断时不得转为正式 Release；Task 7 固定该行为。

---

## Planned File Structure

- `pyproject.toml`：Python 版本、包元数据和测试配置，不引入运行时第三方依赖。
- `.gitignore`：排除构建、staging、下载回环目录、归档、分卷和 Python 缓存。
- `codex_history/models.py`：manifest、rollout、文件记录、验证结果和恢复计划的数据类型。
- `codex_history/rollouts.py`：rollout 发现、UUID/metadata 校验、附件引用提取和排除集合计算。
- `codex_history/safeio.py`：路径约束、SHA-256、稳定复制和安全流式文件操作。
- `codex_history/snapshot.py`：建立 staging、生成规范 manifest 与内部 `SHA256SUMS`。
- `codex_history/archive.py`：确定性 tar 流、zstd 压缩、分卷判断和资产命名。
- `codex_history/verify.py`：外层摘要、tar 成员安全、解压、manifest 和内部摘要验证。
- `codex_history/restore.py`：dry-run 合并计划、只新增安装、journal 和安全回滚。
- `codex_history/publish.py`：Git/远端前置检查、draft Release、下载回环验证和发布。
- `scripts/*.py`：四个薄 CLI 入口，分别调用 export、verify、restore 和 publish 模块。
- `tests/helpers.py`：临时 rollout、附件、tar.zst 和伪命令执行器构造器。
- `tests/test_*.py`：按组件组织的标准库 `unittest` 测试。
- `README.md`、`SECURITY-NOTICE.md`：使用、公开风险、恢复步骤和兼容性边界。
- `manifests/`、`checksums/`：首次及后续 Release 的公开小型元数据。

### Task 1: Project Skeleton and Canonical Manifest Models

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `codex_history/__init__.py`
- Create: `codex_history/models.py`
- Create: `tests/__init__.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Produces: `FileRecord(path: str, kind: str, size: int, sha256: str, thread_id: str | None, source_bucket: str)`
- Produces: `RolloutRecord(thread_id: str, source_bucket: str, path: str, sha256: str)`
- Produces: `ExclusionRecord(thread_id: str, reason: str, matched_rollouts: int)`
- Produces: `SnapshotCounts(session_count: int, rollout_file_count: int, attachment_count: int, total_bytes: int)`
- Produces: `ArchiveParameters(format: str, compression: str, compression_level: int, split_threshold_bytes: int)`
- Produces: `SnapshotManifest(schema_version: int, backup_id: str, created_at_utc: str, source_codex_version: str, source_platform: str, source_root: str, counts: SnapshotCounts, files: tuple[FileRecord, ...], rollouts: tuple[RolloutRecord, ...], exclusions: tuple[ExclusionRecord, ...], duplicate_thread_ids: dict[str, tuple[str, ...]], archive: ArchiveParameters)`
- Produces: `SnapshotManifest.to_dict() -> dict[str, object]` and `SnapshotManifest.to_json_bytes() -> bytes`
- Produces: `ManifestError(ValueError)` for invalid schema values.

- [ ] **Step 1: Write failing canonical-model tests**

```python
def test_manifest_json_is_canonical_and_contains_no_absolute_source_root():
    payload = sample_manifest().to_json_bytes()
    assert payload.endswith(b"\n")
    assert b'"source_root":"$CODEX_HOME"' in payload
    assert b"/home/admin" not in payload
    assert payload == sample_manifest().to_json_bytes()

def test_manifest_rejects_non_relative_or_parent_path():
    for path in ("/absolute.jsonl", "../escape.jsonl", "sessions/../auth.json"):
        with self.assertRaises(ManifestError):
            sample_file_record(path=path)
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run: `python3 -m unittest tests.test_models -v`
Expected: `ModuleNotFoundError: No module named 'codex_history'`.

- [ ] **Step 3: Implement immutable records and canonical JSON**

Use frozen dataclasses; validate POSIX-relative paths, lowercase 64-character SHA-256 values, non-negative sizes and UTC `backup_id` format. Serialize with `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)` plus one trailing newline. `schema_version` is exactly `1` and `source_root` exactly `$CODEX_HOME`.

- [ ] **Step 4: Run the model tests**

Run: `python3 -m unittest tests.test_models -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore codex_history tests
git commit -m "feat: add canonical backup manifest models"
```

### Task 2: Rollout Discovery, Identity Validation, and Attachment Selection

**Files:**
- Create: `codex_history/rollouts.py`
- Create: `tests/helpers.py`
- Create: `tests/test_rollouts.py`

**Interfaces:**
- Consumes: `RolloutRecord` and `ManifestError` from Task 1.
- Produces: `RolloutInfo(path: Path, relative_path: PurePosixPath, source_bucket: str, thread_id: str, attachment_paths: frozenset[PurePosixPath])`.
- Produces: `RolloutSelection(included: tuple[RolloutInfo, ...], excluded: tuple[RolloutInfo, ...], selected_attachments: tuple[PurePosixPath, ...], duplicate_thread_ids: dict[str, tuple[str, ...]])`.
- Produces: `RolloutFormatError(ValueError)` for missing, malformed or contradictory identity metadata.
- Produces: `discover_rollouts(codex_home: Path) -> tuple[Path, ...]`.
- Produces: `read_rollout_info(path: Path, codex_home: Path) -> RolloutInfo`.
- Produces: `select_rollouts(codex_home: Path, excluded_thread_ids: frozenset[str]) -> RolloutSelection`.

- [ ] **Step 1: Write failing rollout-selection tests**

```python
def test_select_rollouts_excludes_every_copy_of_id_and_its_private_attachment():
    selection = select_rollouts(home, frozenset({EXCLUDED_ID}))
    assert {item.thread_id for item in selection.included} == {INCLUDED_ID}
    assert len(selection.excluded) == 2
    assert PurePosixPath("attachments/private/pasted-text.txt") not in selection.selected_attachments

def test_shared_attachment_is_excluded_for_privacy():
    selection = select_rollouts(home, frozenset({EXCLUDED_ID}))
    assert PurePosixPath("attachments/shared/pasted-text.txt") not in selection.selected_attachments

def test_filename_and_session_meta_uuid_mismatch_fails_closed():
    with self.assertRaisesRegex(RolloutFormatError, "thread id mismatch"):
        read_rollout_info(mismatched_rollout, home)
```

Also test malformed JSON before metadata, absent metadata, both `payload.id` and a distinct parent/root `payload.session_id`, archived/source bucket preservation, sorted discovery, duplicate-ID grouping, and attachment strings outside `attachments/` being ignored.

Also reject symlinked rollout files or symlinked directories encountered below either rollout root before reading conversation content.

- [ ] **Step 2: Run the tests and verify failure**

Run: `python3 -m unittest tests.test_rollouts -v`
Expected: import failure for `codex_history.rollouts`.

- [ ] **Step 3: Implement streaming rollout parsing**

Read JSONL line-by-line. Require the first recognized `type == "session_meta"` record to contain a canonical UUID in `payload.id` or `payload.session_id`. When both exist, treat `id` as the rollout identity and `session_id` as its potentially distinct parent/root identity; cross-check the rollout identity against any UUID suffix in `rollout-*.jsonl`. Recursively inspect string values for resolved children of `<codex_home>/attachments`, convert them to logical POSIX-relative paths, and never return source absolute paths.

- [ ] **Step 4: Implement deterministic selection**

Recursively enumerate only regular `.jsonl` files beneath the two allowed rollout roots, sort by logical relative path, preserve duplicates, exclude records when either their rollout ID or parent/root session ID is explicitly excluded, calculate `included_refs - excluded_refs`, and group included duplicate thread IDs without reading the global attachment index.

- [ ] **Step 5: Run rollout tests**

Run: `python3 -m unittest tests.test_rollouts -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add codex_history/rollouts.py tests/helpers.py tests/test_rollouts.py
git commit -m "feat: select Codex rollouts and referenced attachments"
```

### Task 3: Stable Read-Only Copy and Snapshot Construction

**Files:**
- Create: `codex_history/safeio.py`
- Create: `codex_history/snapshot.py`
- Create: `tests/test_safeio.py`
- Create: `tests/test_snapshot.py`

**Interfaces:**
- Consumes: `RolloutSelection` from Task 2 and manifest models from Task 1.
- Produces: `sha256_file(path: Path) -> str`.
- Produces: `stable_copy(source: Path, destination: Path, allowed_root: Path) -> tuple[int, str]` returning size and SHA-256.
- Produces: `PathSafetyError(ValueError)` and `SourceChangedError(RuntimeError)`.
- Produces: `SnapshotConfig(codex_home: Path, staging_parent: Path, backup_id: str, excluded_thread_ids: frozenset[str], codex_version: str, source_platform: str, compression_level: int = 1, split_threshold_bytes: int = 1_932_735_283)`.
- Produces: `SnapshotResult(root: Path, manifest_path: Path, checksums_path: Path, manifest: SnapshotManifest)`.
- Produces: `build_snapshot(config: SnapshotConfig) -> SnapshotResult`.

- [ ] **Step 1: Write failing stable-I/O tests**

```python
def test_stable_copy_detects_source_growth_and_removes_destination():
    with mock.patch("codex_history.safeio._path_identity", side_effect=(before, after_growth)):
        with self.assertRaisesRegex(SourceChangedError, "changed while copying"):
            stable_copy(source, destination, allowed_root)
    assert not destination.exists()

def test_stable_copy_rejects_symlink_even_when_target_is_allowed():
    source.symlink_to(real_file)
    with self.assertRaises(PathSafetyError):
        stable_copy(source, destination, allowed_root)
```

Also test inode replacement, mtime-only change, path outside root, special file rejection, chunked hashing and byte-identical copy.

- [ ] **Step 2: Run safe-I/O tests and verify failure**

Run: `python3 -m unittest tests.test_safeio -v`
Expected: import failure for `codex_history.safeio`.

- [ ] **Step 3: Implement stable copy**

Use `lstat` before opening; reject non-regular files and symlinks; resolve and constrain the source beneath the explicit allowed root. Open with `O_RDONLY` plus `O_NOFOLLOW` where available, compare `fstat` with the path identity, copy in bounded chunks while hashing, then compare both the still-open descriptor and path `(st_dev, st_ino, st_size, st_mtime_ns)` identities. Re-hash the destination and unlink only the incomplete destination on any failure.

- [ ] **Step 4: Write failing snapshot tests**

```python
def test_snapshot_contains_only_allowlisted_selected_files_and_checksums():
    result = build_snapshot(config)
    archived = relative_regular_files(result.root)
    assert archived == EXPECTED_FILES
    assert {record.thread_id for record in result.manifest.rollouts} == {INCLUDED_ID}
    assert result.manifest.exclusions[0].thread_id == EXCLUDED_ID
    assert verify_sha256sums(result.root / "SHA256SUMS", result.root) == []

def test_snapshot_failure_leaves_codex_home_unchanged():
    before = tree_metadata(codex_home)
    with self.assertRaises(SourceChangedError):
        build_snapshot(config)
    assert tree_metadata(codex_home) == before
```

Also test missing referenced attachment, duplicate-ID manifest grouping, excluded matched count, no absolute path in manifest, deterministic `SHA256SUMS` ordering, omission of the original global attachment index, and non-inclusion of populated `auth.json`, config, SQLite, logs and cache fixtures.

- [ ] **Step 5: Implement snapshot builder**

Create `<staging_parent>/<backup_id>/codex-history/`; copy selected rollouts and attachments only. Generate `manifest.json`, then `SHA256SUMS` over every regular file except `SHA256SUMS` itself. On failure, mark the staging directory incomplete and return no successful result; never clean or alter source files.

- [ ] **Step 6: Run component tests**

Run: `python3 -m unittest tests.test_safeio tests.test_snapshot -v`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add codex_history/safeio.py codex_history/snapshot.py tests/test_safeio.py tests/test_snapshot.py
git commit -m "feat: build read-only verified history snapshots"
```

### Task 4: Deterministic Archive and Export CLI

**Files:**
- Create: `codex_history/archive.py`
- Create: `codex_history/export.py`
- Create: `scripts/export_codex_history.py`
- Create: `tests/test_archive.py`
- Create: `tests/test_export.py`

**Interfaces:**
- Consumes: `build_snapshot()` from Task 3.
- Produces: `AssetSet(archive_paths: tuple[Path, ...], checksum_path: Path, manifest_path: Path, backup_id: str)`.
- Produces: `ArchiveError(RuntimeError)` for missing tools, failed subprocesses or partial assets.
- Produces: `create_tar_zst(snapshot_root: Path, output_path: Path, compression_level: int = 1) -> Path`.
- Produces: `split_archive(archive_path: Path, max_part_bytes: int = 1_932_735_283) -> tuple[Path, ...]` with `.part001` naming when splitting is required.
- Produces: `build_assets(config: SnapshotConfig, output_dir: Path) -> AssetSet`.
- Produces: `codex_history.export.main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Write failing archive tests**

```python
def test_create_tar_zst_is_deterministic_for_same_snapshot():
    first = create_tar_zst(snapshot_root, output / "first.tar.zst")
    second = create_tar_zst(snapshot_root, output / "second.tar.zst")
    assert sha256_file(first) == sha256_file(second)

def test_export_asset_names_and_outer_checksum_match():
    assets = build_assets(config, output)
    assert assets.archive_paths[0].name == f"codex-history-{BACKUP_ID}.tar.zst"
    assert parse_outer_checksum(assets.checksum_path) == sha256_file(assets.archive_paths[0])

def test_split_archive_parts_reassemble_to_exact_original_bytes():
    parts = split_archive(archive, max_part_bytes=16)
    assert [part.name for part in parts] == ["backup.tar.zst.part001", "backup.tar.zst.part002"]
    assert b"".join(part.read_bytes() for part in parts) == archive.read_bytes()
```

Also test missing `tar`/`zstd`, non-zero compressor exit, partial archive cleanup, exact independent manifest bytes, per-part outer checksums and no split at or below the 1.8 GiB threshold.

- [ ] **Step 2: Run archive tests and verify failure**

Run: `python3 -m unittest tests.test_archive tests.test_export -v`
Expected: import failure for `codex_history.archive`.

- [ ] **Step 3: Implement deterministic streaming archive**

Invoke GNU tar with sorted names, numeric owner/group `0`, normalized mtime `@0`, stable PAX options and a root member named `codex-history`; stream tar stdout into `zstd -1`. Write a temporary output beside the destination and rename only after both processes exit successfully. Keep a single `.tar.zst` at or below 1,932,735,283 bytes; otherwise split it sequentially into deterministic `.partNNN` files. The outer checksum file contains one SHA-256 line for every published archive asset.

- [ ] **Step 4: Implement export CLI**

Required arguments are `--codex-home`, `--output-dir` and repeatable `--exclude-thread`; optional arguments are `--backup-id`, `--codex-version`, `--compression-level`. Validate commands before snapshotting, print only summary counts/paths/digests, and return non-zero on any incomplete snapshot.

- [ ] **Step 5: Run export tests and CLI help**

Run: `python3 -m unittest tests.test_archive tests.test_export -v`
Expected: all tests pass.
Run: `python3 scripts/export_codex_history.py --help`
Expected: exit 0 and documented required arguments.

- [ ] **Step 6: Commit**

```bash
git add codex_history/archive.py codex_history/export.py scripts/export_codex_history.py tests/test_archive.py tests/test_export.py
git commit -m "feat: export deterministic tar zstd backup assets"
```

### Task 5: Safe Archive Verification

**Files:**
- Create: `codex_history/verify.py`
- Create: `scripts/verify_backup.py`
- Create: `tests/test_verify.py`

**Interfaces:**
- Consumes: manifest models and `sha256_file()`.
- Produces: `VerificationReport(backup_id: str, file_count: int, total_bytes: int, excluded_thread_ids: tuple[str, ...])`.
- Produces: `UnsafeArchiveError(ValueError)` and `IntegrityError(ValueError)`.
- Produces: `safe_extract_tar_zst(archive_path: Path, destination: Path) -> Path`.
- Produces: `verify_extracted_snapshot(root: Path, public_manifest: Path | None = None) -> VerificationReport`.
- Produces: `verify_archive(archive_paths: Sequence[Path], outer_checksum: Path, public_manifest: Path, destination: Path) -> VerificationReport`; it verifies every part and reassembles split assets into a temporary logical archive.
- Produces: `codex_history.verify.main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Write failing malicious-archive and integrity tests**

```python
def test_safe_extract_rejects_late_traversal_before_writing_any_member():
    archive = make_tar_zst({"codex-history/sessions/safe.jsonl": b"safe", "codex-history/../../escape": b"owned"})
    with self.assertRaises(UnsafeArchiveError):
        safe_extract_tar_zst(archive, destination)
    assert not outside_path.exists()
    assert not (destination / "codex-history/sessions/safe.jsonl").exists()

def test_safe_extract_rejects_absolute_symlink_hardlink_and_device_members():
    for member_type in MALICIOUS_MEMBER_TYPES:
        with self.subTest(member_type=member_type), self.assertRaises(UnsafeArchiveError):
            safe_extract_tar_zst(make_malicious_archive(member_type), destination)

def test_verify_rejects_public_manifest_or_file_digest_mismatch():
    with self.assertRaises(IntegrityError):
        verify_archive((archive,), checksum, altered_manifest, destination)
```

Also test outer checksum mismatch before decompression, wrong root name, duplicate tar member, undeclared file, missing file, excluded ID appearing in rollout path/record, truncated zstd stream and successful round trip.

- [ ] **Step 2: Run verifier tests and verify failure**

Run: `python3 -m unittest tests.test_verify -v`
Expected: import failure for `codex_history.verify`.

- [ ] **Step 3: Implement safe streaming extraction**

Verify every outer checksum first. Reassemble ordered `.partNNN` assets into a fresh temporary file when needed. Perform two streaming decompression passes: the first reads all headers, captures and validates the manifest, rejects duplicate or unsafe members, and verifies declared sizes without writing extracted members; the second extracts only after the entire member table passes. Accept only directories and regular files under one `codex-history/` root, create files with exclusive mode, and remove only the new extraction directory on failure.

- [ ] **Step 4: Implement structural and digest verification**

Require schema version `1`, exact public/internal manifest bytes, a one-to-one match between manifest records and extracted data files, valid internal `SHA256SUMS`, correct counts/sizes, and absence of all manifest-declared excluded IDs from rollout records and paths.

The CLI requires repeatable `--archive`, plus `--checksum`, `--manifest` and `--destination`; it prints one summary only after verification succeeds.

- [ ] **Step 5: Run verifier tests and CLI help**

Run: `python3 -m unittest tests.test_verify -v`
Expected: all tests pass.
Run: `python3 scripts/verify_backup.py --help`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add codex_history/verify.py scripts/verify_backup.py tests/test_verify.py
git commit -m "feat: verify and safely extract backup archives"
```

### Task 6: Dry-Run Restore, Conflict Protection, and Rollback

**Files:**
- Create: `codex_history/restore.py`
- Create: `scripts/restore_codex_history.py`
- Create: `tests/test_restore.py`

**Interfaces:**
- Consumes: verified extracted root and `RolloutInfo` parser.
- Produces: `RestoreAction(source: Path, destination: Path, relative_path: str, sha256: str, status: Literal["add", "skip-identical", "conflict"])`.
- Produces: `RestorePlan(backup_id: str, actions: tuple[RestoreAction, ...], semantic_conflicts: tuple[str, ...])` with `has_conflicts: bool`.
- Produces: `RollbackReport(removed: tuple[Path, ...], refused: tuple[Path, ...], missing: tuple[Path, ...])`.
- Produces: `RestoreConflictError(RuntimeError)`.
- Produces: `plan_restore(snapshot_root: Path, target_codex_home: Path) -> RestorePlan`.
- Produces: `apply_restore(plan: RestorePlan, journal_path: Path) -> Path`.
- Produces: `rollback_restore(journal_path: Path) -> RollbackReport`.
- Produces: `codex_history.restore.main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Write failing restore-plan tests**

```python
def test_plan_restore_classifies_add_identical_and_path_conflict():
    plan = plan_restore(snapshot_root, target_home)
    assert [action.status for action in plan.actions] == ["add", "skip-identical", "conflict"]

def test_same_thread_id_at_different_path_with_different_bytes_is_semantic_conflict():
    plan = plan_restore(snapshot_root, target_home)
    assert THREAD_ID in plan.semantic_conflicts
    assert plan.has_conflicts
```

Also test a backup containing duplicate thread IDs, missing target roots, and absence of all SQLite writes.

- [ ] **Step 2: Write failing apply/race/rollback tests**

```python
def test_apply_never_overwrites_file_created_after_dry_run():
    plan = plan_restore(snapshot_root, target_home)
    destination.write_bytes(b"created concurrently")
    with self.assertRaises(RestoreConflictError):
        apply_restore(plan, journal)
    assert destination.read_bytes() == b"created concurrently"

def test_rollback_deletes_only_unchanged_files_installed_by_journal():
    apply_restore(plan, journal)
    changed_file.write_bytes(b"user changed this")
    report = rollback_restore(journal)
    assert not unchanged_added_file.exists()
    assert changed_file.exists()
    assert changed_file in report.refused
```

- [ ] **Step 3: Run restore tests and verify failure**

Run: `python3 -m unittest tests.test_restore -v`
Expected: import failure for `codex_history.restore`.

- [ ] **Step 4: Implement dry-run planner and exclusive installer**

Hash target candidates without mutating them, parse target rollout IDs for semantic conflicts, and make any conflict block the entire apply. During apply, re-check each destination and create it exclusively; stream-copy, fsync, verify the installed hash, and journal only completed additions. Never overwrite, rename or delete a pre-existing target file.

- [ ] **Step 5: Implement guarded rollback and CLI**

Default command requires `--snapshot-root` and `--target-codex-home` and produces a human-readable and JSON dry-run summary. Require `--apply --journal <path>` for mutation and `--rollback <journal>` for rollback. Rollback removes a recorded addition only when its current digest still equals the journal digest, then prunes only newly empty directories recorded by the journal.

- [ ] **Step 6: Run restore tests and CLI help**

Run: `python3 -m unittest tests.test_restore -v`
Expected: all tests pass.
Run: `python3 scripts/restore_codex_history.py --help`
Expected: exit 0 and dry-run documented as default.

- [ ] **Step 7: Commit**

```bash
git add codex_history/restore.py scripts/restore_codex_history.py tests/test_restore.py
git commit -m "feat: restore Codex history without overwriting"
```

### Task 7: Draft Release Publisher and Download Round-Trip Gate

**Files:**
- Create: `codex_history/publish.py`
- Create: `scripts/publish_codex_history.py`
- Create: `tests/test_publish.py`

**Interfaces:**
- Consumes: `verify_archive()` and `AssetSet` naming rules.
- Produces: `CommandRunner.run(argv: Sequence[str], cwd: Path | None = None) -> CompletedProcess[str]`.
- Produces: `PublishConfig(repo: str, tag: str, title: str, archive_paths: tuple[Path, ...], checksum_path: Path, manifest_path: Path, checkout: Path)`.
- Produces: `ReleaseVerificationError(RuntimeError)`.
- Produces: `publish_verified_release(config: PublishConfig, runner: CommandRunner) -> str` returning the Release URL.
- Produces: `codex_history.publish.main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Write failing publication transaction tests**

```python
def test_publisher_creates_draft_verifies_download_then_publishes():
    url = publish_verified_release(config, fake_runner)
    assert fake_runner.commands == EXPECTED_DRAFT_UPLOAD_DOWNLOAD_VERIFY_PUBLISH_SEQUENCE
    assert url == "https://github.com/panxy1019/codex/releases/tag/" + TAG

def test_downloaded_asset_mismatch_keeps_release_draft():
    fake_runner.downloaded_manifest = ALTERED_MANIFEST
    with self.assertRaises(ReleaseVerificationError):
        publish_verified_release(config, fake_runner)
    assert ["gh", "release", "edit", TAG, "--draft=false", "--repo", REPO] not in fake_runner.commands
```

Also test dirty checkout, local HEAD not equal to `origin/main`, metadata files not tracked/byte-identical, existing tag ambiguity, interrupted download, failed upload, and shell metacharacters remaining literal argv entries.

- [ ] **Step 2: Run publisher tests and verify failure**

Run: `python3 -m unittest tests.test_publish -v`
Expected: import failure for `codex_history.publish`.

- [ ] **Step 3: Implement preflight and draft transaction**

Require a clean checkout on `main`, `HEAD == refs/remotes/origin/main`, and tracked `manifests/<backup_id>.json` plus `checksums/<backup_id>.sha256` matching assets. Use argument arrays, never a shell string. Create or reuse only an unambiguous draft tag; upload assets; download them into a fresh temporary directory; call `verify_archive()` on the downloads; edit to `--draft=false` only after success.

The CLI requires `--repo`, `--checkout`, repeatable `--archive`, `--checksum` and `--manifest`; tag and title derive exactly from the manifest `backup_id`.

- [ ] **Step 4: Run publisher tests and CLI help**

Run: `python3 -m unittest tests.test_publish -v`
Expected: all tests pass.
Run: `python3 scripts/publish_codex_history.py --help`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add codex_history/publish.py scripts/publish_codex_history.py tests/test_publish.py
git commit -m "feat: publish backups through a verified draft release"
```

### Task 8: User Documentation and Full Synthetic End-to-End Test

**Files:**
- Create: `README.md`
- Create: `SECURITY-NOTICE.md`
- Create: `tests/test_end_to_end.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: all public CLIs from Tasks 4–7.
- Produces: documented export, verify, dry-run restore, apply, rollback and Release download commands.

- [ ] **Step 1: Write failing synthetic end-to-end test**

```python
def test_export_verify_restore_round_trip_is_byte_exact_and_idempotent():
    assets = export_fixture_home(excluding=EXCLUDED_ID)
    report = verify_assets_into_fresh_staging(assets)
    first = restore_assets(assets, fresh_target, apply=True)
    second = restore_assets(assets, fresh_target, apply=False)
    assert report.excluded_thread_ids == (EXCLUDED_ID,)
    assert all_files_match_fixture_except_excluded(fresh_target)
    assert {action.status for action in second.actions} == {"skip-identical"}
    assert excluded_id_occurs_only_in_exclusion_record(assets, EXCLUDED_ID)
    assert SECRET_SENTINEL not in captured_stdout_and_stderr()
```

- [ ] **Step 2: Run the end-to-end test and verify its initial failure**

Run: `python3 -m unittest tests.test_end_to_end -v`
Expected: failure until the CLI orchestration and fixture helpers are wired together.

- [ ] **Step 3: Complete orchestration and document exact commands**

README must lead with the public/unencrypted warning, supported Python/tar/zstd/gh versions, source allowlist, exact exclusion option, asset naming, verification, restoration and `codex resume --all`. SECURITY-NOTICE must state that conversation contents are not scanned or redacted and that the user will make the repository Private later.

- [ ] **Step 4: Run the complete test suite and static checks**

Run: `python3 -m unittest discover -s tests -v`
Expected: all tests pass.
Run: `python3 -m compileall -q codex_history scripts tests`
Expected: exit 0.
Run: `git diff --check`
Expected: no output and exit 0.

- [ ] **Step 5: Commit**

```bash
git add README.md SECURITY-NOTICE.md .gitignore codex_history scripts tests
git commit -m "docs: add verified backup and restore workflow"
```

### Task 9: Produce, Publish, and Re-Verify the First Real Snapshot

**Files:**
- Create: `manifests/<backup-id>.json`
- Create: `checksums/<backup-id>.sha256`
- No source changes under `/home/admin/.codex`

**Interfaces:**
- Consumes: all tested tools from Tasks 1–8.
- Produces: one public verified GitHub Release at `panxy1019/codex` and its committed public metadata.

- [ ] **Step 1: Record a read-only source baseline and repository preflight**

Create task-specific paths and record source identities without hashing conversation bytes:

```bash
backup_id="$(date -u +%Y-%m-%dT%H%M%SZ)"
backup_build_dir="$(mktemp -d /home/admin/Documents/ChatGPT/KT/codex-history-build.XXXXXX)"
backup_verify_dir="$(mktemp -d /home/admin/Documents/ChatGPT/KT/codex-history-verify.XXXXXX)"
backup_restore_home="$(mktemp -d /home/admin/Documents/ChatGPT/KT/codex-history-restore.XXXXXX)"
find /home/admin/.codex/sessions /home/admin/.codex/archived_sessions /home/admin/.codex/attachments \
  -type f ! -name '*01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b*' \
  -printf '%p\t%D\t%i\t%s\t%T@\n' | LC_ALL=C sort > "$backup_build_dir/source-before.tsv"
git status --short --branch
git rev-parse HEAD
git rev-parse refs/remotes/origin/main
df -h /home/admin/Documents/ChatGPT/KT
```

Expected: clean synchronized `main`; build, verify and isolated restore directories are distinct; free space is sufficient for source staging, compressed assets, extracted verification and restore copies.

- [ ] **Step 2: Run all tests immediately before touching real history**

Run: `python3 -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 3: Export with the exact exclusion ID into a fresh dedicated build directory**

```bash
python3 scripts/export_codex_history.py \
  --codex-home /home/admin/.codex \
  --output-dir "$backup_build_dir" \
  --backup-id "$backup_id" \
  --exclude-thread 01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b
```

Expected: exit 0, one logical asset set, exclusion matched at least one rollout, installed Codex version recorded, no unstable source file, and no source write.

- [ ] **Step 4: Independently verify the generated assets and excluded-ID absence**

Run `scripts/verify_backup.py` with every generated `--archive` part, `--checksum "$backup_build_dir/codex-history-$backup_id.tar.zst.sha256"`, `--manifest "$backup_build_dir/codex-history-$backup_id.manifest.json"` and `--destination "$backup_verify_dir"`. Compare manifest counts with the exporter summary; inspect archive member names, manifest, `SHA256SUMS` and parsed rollout IDs for the excluded ID. Expected: all digest and structural checks pass and the excluded ID has zero presence outside the explicit exclusion record.

- [ ] **Step 5: Exercise restore in an isolated temporary `CODEX_HOME`**

Run `scripts/restore_codex_history.py --snapshot-root "$backup_verify_dir/codex-history" --target-codex-home "$backup_restore_home"`, repeat with `--apply --journal "$backup_build_dir/restore-journal.json"`, then run dry-run again. Expected statuses are first `add`, then only `skip-identical`; byte hashes match the snapshot and no SQLite file is created by the restore tool. Launch Codex only with `CODEX_HOME="$backup_restore_home"` to smoke-test one non-sensitive manifest UUID via `codex resume <SESSION_ID>`; do not copy authentication material and do not point the smoke test at the live source home.

- [ ] **Step 6: Commit and push public metadata**

Create `manifests/` and `checksums/`, copy the generated independent manifest to `manifests/$backup_id.json` and outer checksum to `checksums/$backup_id.sha256`, run `git diff --check`, commit with `backup: record Codex history snapshot $backup_id`, and push `main`. Verify local HEAD equals `origin/main`.

- [ ] **Step 7: Publish via draft, download, verify, and release**

Run `scripts/publish_codex_history.py --repo panxy1019/codex --checkout .` with every generated `--archive` part, the exact `--checksum` and `--manifest` assets. Expected: a draft is created, all public assets are downloaded again, `verify_archive()` passes on downloaded bytes, and only then the Release becomes non-draft.

- [ ] **Step 8: Confirm remote state and source non-interference**

Use `gh release view` to confirm the public tag, asset names, sizes and URL. Generate `source-after.tsv` with the same exact `find` expression from Step 1 and compare it with `source-before.tsv`; the exporter must not have changed any included source inode, size or mtime. The excluded active rollout may continue changing because Codex owns it. Confirm the active conversation is still usable.

- [ ] **Step 9: Preserve evidence and remove only disposable staging**

Record the Release URL, tag, outer SHA-256, backup ID, included/excluded counts and round-trip result in the final handoff. Delete only explicitly identified temporary staging/download directories after verification; retain the Release assets and committed metadata.
