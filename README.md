# Codex history backup

> **公开且未加密：这个流程会把所选 Codex 对话原文和引用附件作为 GitHub Release 资产发布。任何能访问仓库的人都能下载、阅读和永久复制这些内容。工具不会扫描、脱敏或删除对话里的密码、令牌、个人信息或其他秘密。**

本仓库提供一个可校验、可恢复的本地 Codex 对话备份流程。它只读取明确允许的历史文件，将一个或多个会话 ID 完整排除，生成确定性的 `tar.zst` 资产，下载回验后才把 GitHub Draft Release 转为公开 Release。恢复默认只是 dry-run，并且永不覆盖目标电脑上已有的文件。

## 依赖

- Python 3.11 或更高版本；本项目本身只使用标准库。
- GNU `tar`，需支持 `--sort`、`--mtime`、`--numeric-owner` 和 `--pax-option`。
- `zstd` 命令行工具。
- GitHub CLI `gh`，且已通过 `gh auth status` 验证登录状态；只有发布和下载 Release 时需要。
- `git`，发布器要求仓库处于干净、已推送的 `main`，并且 `HEAD` 与 `origin/main` 完全一致。

在本仓库根目录运行测试：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q codex_history scripts tests
```

## 读取范围与排除规则

导出器不会遍历整个 `$CODEX_HOME`。允许读取和打包的范围只有：

- `sessions/**/*.jsonl`
- `archived_sessions/**/*.jsonl`
- 上述已选会话 JSONL 中明确引用、且位于 `$CODEX_HOME/attachments/` 下的普通文件

以下内容不在允许列表中，也不会作为备份内容复制：`auth.json`、`config.toml`、SQLite 状态库、日志、缓存、插件及未被已选会话引用的附件。附件路径采用隐私优先规则：只要某个附件同时被排除会话引用，它就不会进入备份，即使另一个已选会话也引用了它。

`--exclude-thread` 可重复使用。当前 rollout 身份取 `session_meta.payload.id`（旧格式缺失时使用 `payload.session_id`），并与文件名 UUID 交叉验证；`payload.session_id` 还可能表示其父/根会话。排除规则同时匹配 rollout 自身 ID 和父/根会话 ID，因此当前会话派生的子代理记录也会一并排除。当前这次迁移必须使用：

```text
--exclude-thread 01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b
```

如果 rollout 无法解析、文件名与内部会话 ID 不一致、源文件在复制时变化，或引用附件丢失，导出会失败关闭，不会悄悄生成不完整备份。

## 1. 导出

使用全新的输出目录。以下变量名只是示例；不要把输出目录放进 `$CODEX_HOME`：

```bash
backup_id="$(date -u +%Y-%m-%dT%H%M%SZ)"
backup_dir="$(mktemp -d /tmp/codex-history-build.XXXXXX)"

python3 scripts/export_codex_history.py \
  --codex-home "$HOME/.codex" \
  --output-dir "$backup_dir" \
  --backup-id "$backup_id" \
  --exclude-thread 01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b
```

生成的 Release 资产名称为：

- `codex-history-<backup-id>.tar.zst`；若超过分片阈值，则为连续的 `.part001`、`.part002`……
- `codex-history-<backup-id>.tar.zst.sha256`，记录每个上传归档资产的外层 SHA-256。
- `codex-history-<backup-id>.manifest.json`，记录文件摘要、会话 ID、计数及显式排除项。

归档内部还有独立的 `manifest.json` 和 `SHA256SUMS`。压缩包不应直接提交进 Git；它们应作为 Release 资产上传。

## 2. 独立校验并安全解包

单文件归档：

```bash
verify_dir="$(mktemp -d /tmp/codex-history-verify.XXXXXX)"

python3 scripts/verify_backup.py \
  --archive "$backup_dir/codex-history-$backup_id.tar.zst" \
  --checksum "$backup_dir/codex-history-$backup_id.tar.zst.sha256" \
  --manifest "$backup_dir/codex-history-$backup_id.manifest.json" \
  --destination "$verify_dir"
```

对于分片归档，按编号为每个分片重复一次 `--archive`：

```bash
python3 scripts/verify_backup.py \
  --archive "$backup_dir/codex-history-$backup_id.tar.zst.part001" \
  --archive "$backup_dir/codex-history-$backup_id.tar.zst.part002" \
  --checksum "$backup_dir/codex-history-$backup_id.tar.zst.sha256" \
  --manifest "$backup_dir/codex-history-$backup_id.manifest.json" \
  --destination "$verify_dir"
```

校验器先验证外层摘要，再拒绝绝对路径、`..`、重复成员、错误根目录、符号链接、硬链接、设备和 FIFO，之后才解包；最后逐一核对 manifest、内部摘要、文件大小、会话记录、计数和排除 ID。

## 3. 恢复到另一台电脑

先安装依赖并克隆本仓库。通过浏览器下载 Release 资产，或使用：

```bash
mkdir -p downloaded-assets
gh release download "codex-history-$backup_id" \
  --repo panxy1019/codex \
  --dir downloaded-assets
```

按照上一节校验下载文件，得到 `"$verify_dir/codex-history"`。目标 `$CODEX_HOME` 必须已经存在且不能是符号链接：

```bash
mkdir -p "$HOME/.codex"

# 默认只预演，不写入；输出人类可读摘要和 JSON 摘要。
python3 scripts/restore_codex_history.py \
  --snapshot-root "$verify_dir/codex-history" \
  --target-codex-home "$HOME/.codex"
```

检查结果没有 `conflict` 或 `semantic_conflicts` 后，再显式应用：

```bash
python3 scripts/restore_codex_history.py \
  --snapshot-root "$verify_dir/codex-history" \
  --target-codex-home "$HOME/.codex" \
  --apply \
  --journal "$PWD/$backup_id.restore-journal.json"
```

恢复器只独占创建缺失文件；相同文件会跳过，路径相同但内容不同、同一会话 ID 内容不同、备份内重复会话 ID，或预演后的竞态变化都会阻止恢复。它不会写 SQLite，也不会复制认证或配置文件。

恢复完成后，重新启动 Codex 并查看全部历史：

```bash
codex resume --all
```

如需撤销本次新增文件：

```bash
python3 scripts/restore_codex_history.py \
  --rollback "$PWD/$backup_id.restore-journal.json"
```

回滚只删除日志中记录且当前 SHA-256 仍与安装时一致的文件；用户后来修改过的文件会被拒绝删除。

## 4. 提交元数据并发布 Release

在发布前，把公开 manifest 和外层摘要作为小型审计元数据提交并推送：

```bash
mkdir -p manifests checksums
cp "$backup_dir/codex-history-$backup_id.manifest.json" "manifests/$backup_id.json"
cp "$backup_dir/codex-history-$backup_id.tar.zst.sha256" "checksums/$backup_id.sha256"
git add "manifests/$backup_id.json" "checksums/$backup_id.sha256"
git commit -m "backup: record Codex history snapshot $backup_id"
git push origin main
```

然后运行发布器。对每个分片重复一次 `--archive`：

```bash
python3 scripts/publish_codex_history.py \
  --repo panxy1019/codex \
  --checkout "$PWD" \
  --archive "$backup_dir/codex-history-$backup_id.tar.zst" \
  --checksum "$backup_dir/codex-history-$backup_id.tar.zst.sha256" \
  --manifest "$backup_dir/codex-history-$backup_id.manifest.json"
```

发布器要求工作区干净、分支为 `main`、本地提交等于 `origin/main`，且跟踪的元数据与上传字节完全相同。它创建或复用同标签的 Draft Release，上传资产，再下载到新临时目录执行完整校验；只有下载回验成功后才执行 `--draft=false`。上传或验证失败时 Release 保持 Draft。

安全假设和本次公开发布决定见 [SECURITY-NOTICE.md](SECURITY-NOTICE.md)。
