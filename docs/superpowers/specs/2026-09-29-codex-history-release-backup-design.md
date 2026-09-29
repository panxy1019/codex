# Codex 历史对话 GitHub Release 备份与恢复设计

状态：已获口头方案批准，等待对本书面规格的最终审阅  
日期：2026-09-29  
目标仓库：<https://github.com/panxy1019/codex>  
仓库可见性：Public（由用户稍后自行改为 Private）

## 1. 背景与目标

本项目把当前电脑上的 Codex 历史对话制作成可校验、可下载、可合并恢复的离线快照，并把快照作为 GitHub Release 资产发布。另一台电脑下载快照后，应能在不覆盖当地已有历史的前提下，把这些 rollout 会话文件合并到当地 `CODEX_HOME`，再通过 `codex resume --all` 查找和继续会话。

当前仍在进行的会话必须完全排除：

```text
01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b
```

“完全排除”是硬性条件：任何对应 rollout 文件、仅由该会话引用的附件、清单条目或归档内容都不得进入 Release。导出过程对源 `CODEX_HOME` 只读，不锁定、不移动、不重命名、不截断任何源文件，因此不会中断正在运行的 Codex。

## 2. 可行性与兼容性边界

方案在“保存原始会话文件并在另一台机器合并恢复”的意义上可行。Codex 官方文档说明会话可由 `codex resume` 恢复，`codex resume --all` 会包含当前目录之外的会话；会话 ID 也可直接传给 `codex resume`。但是，官方文档没有承诺一套跨机器导入 API，且同步对话不会上传整个本地文件夹，也不会自动让另一台电脑获得本地状态。因此，本方案属于对本地 rollout 文件格式的可验证备份与最佳努力恢复，而不是 OpenAI 官方云同步功能。

兼容性承诺分为两层：

1. **数据可恢复**：快照中的每个文件都可通过 SHA-256 校验，并可还原为原始字节。
2. **Codex 可发现**：在相同或兼容 Codex 版本上，把 rollout 放回标准目录后，预期可由 `codex resume --all` 发现；这一点须在隔离测试目录和目标电脑上做烟雾验证，不能只由文件校验推断。

参考资料：

- [OpenAI：Projects and conversations](https://learn.chatgpt.com/docs/projects)
- [OpenAI：Codex command-line reference](https://learn.chatgpt.com/docs/developer-commands)
- [GitHub：About releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
- [GitHub：Repository limits](https://docs.github.com/en/repositories/creating-and-managing-repositories/repository-limits)

## 3. 已确认的产品决策

- 使用 GitHub Release 资产承载大型备份，不把数百 MiB 的归档文件直接提交到 Git 历史。
- 仓库保存脚本、说明、测试、公开清单和外层校验值。
- Release 资产不加密。
- 不设置“扫描到对话内疑似秘密就禁止发布”的安全闸门；对话内容不做删改或脱敏。
- 即使没有内容扫描闸门，认证材料、Codex 配置、缓存、数据库、日志等非会话文件仍属于禁止范围，永不进入备份。
- 当前仓库保持 Public。用户明确接受在改为 Private 之前，Release 中的提示词、回答、工具输出、代码片段、路径及附件可能被任何人下载。
- 唯一按用户指示排除的活动会话是 `01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b`。导出时仍会执行文件稳定性检查；发生变化的其他文件不会被强行复制，而会使导出失败并报告。
- 不复制 Codex 的 SQLite 索引。目标机器由 Codex 自行重建或重新发现会话，避免绝对路径、WAL 状态和版本差异造成损坏。

## 4. 数据范围

### 4.1 允许进入快照的源

只允许以下逻辑路径：

```text
$CODEX_HOME/sessions/**/*.jsonl
$CODEX_HOME/archived_sessions/**/*.jsonl
$CODEX_HOME/attachments/**
```

其中附件不是整目录盲拷贝：导出器先从被纳入的 rollout 中收集附件引用，只复制这些引用实际需要的文件。全局附件索引不得原样打包，因为其中可能包含被排除会话的文本摘录和源机器绝对路径；若恢复确实需要索引，则由导出器仅根据已选附件生成最小化、无被排除内容的新索引。

### 4.2 永久禁止进入快照的内容

以下项目即使存在于 `CODEX_HOME` 也不得进入快照：

```text
auth.json
config.toml
config.toml.*
installation_id
.codex-global-state.json
*.sqlite
*.sqlite-wal
*.sqlite-shm
logs/
log/
browser/
cache/
ipc/
node_repl/
shell_snapshots/
tmp/
plugins/
skills/
```

允许列表优先于排除列表：实现必须从三类允许源开始枚举，不能先复制整个 `CODEX_HOME` 再删除敏感项。

### 4.3 活动会话排除规则

导出器必须在复制任何字节前解析每个 rollout 的会话 ID。会话 ID 优先从规范文件名取得，并用 rollout 元数据交叉验证；旧格式无法从文件名判断时，仅读取足以识别 ID 的元数据。

对排除 ID 的处理规则：

1. 所有目录中匹配该 ID 的 rollout 都排除，不能只排除当前已知路径。
2. 该 rollout 不进入 staging、tar、公开清单或 checksum 文件。
3. 导出器不把全局附件索引原样复制。
4. 附件仅从已纳入 rollout 的引用集合选择；只被排除会话引用的附件自然不会入选。
5. 若同一附件同时被被排除会话和其他历史会话引用，隐私优先：该附件不发布，并在本地导出报告中标记依赖缺失；本地报告不上传。
6. 最终归档必须再次进行负向检查：文件名、归档路径、解析出的会话 ID 和文本型清单中都不得出现该 ID。检查失败即停止发布。

## 5. 源文件一致性与“不影响当前对话”

导出操作不得要求 Codex 退出，也不得向源目录写入锁文件或临时文件。每个候选文件采用如下稳定复制规则：

1. 以只读方式取得复制前的文件类型、设备号、inode、大小和纳秒级修改时间。
2. 拒绝符号链接、设备文件、FIFO、socket 以及任何不在允许根目录下的路径。
3. 流式读取源文件，同时计算 SHA-256，并写入独立 staging 目录。
4. 复制后再次读取源文件属性。
5. 两次属性不一致时删除 staging 副本并令本次导出失败；不发布部分成功的快照。
6. 对 staging 副本重新计算 SHA-256，确认与复制时的摘要一致。

这样不会阻止正在运行的会话继续追加内容。明确排除的活动 rollout 即使正在变化，也不会被复制；实现只需识别并记录“已排除”，不应对它进行完整内容哈希。

## 6. 快照格式

单个快照采用以下结构：

```text
codex-history/
  sessions/
  archived_sessions/
  attachments/
  manifest.json
  SHA256SUMS
```

归档格式为 `tar.zst`，默认使用快速、流式的 zstd 压缩，避免把完整原始数据或压缩数据一次性载入内存。

### 6.1 命名

备份 ID 使用 UTC 时间，例：

```text
2026-09-29T153045Z
```

Release tag、标题和资产名：

```text
codex-history-2026-09-29T153045Z
Codex history snapshot 2026-09-29T153045Z
codex-history-2026-09-29T153045Z.tar.zst
codex-history-2026-09-29T153045Z.tar.zst.sha256
codex-history-2026-09-29T153045Z.manifest.json
```

若归档接近 GitHub 单个 Release 资产 2 GiB 的上限，实现应在 1.8 GiB 处采用确定性分卷，并为每卷产生独立校验值。当前数据量预计可使用单一资产，但是否分卷必须依据最终文件大小决定。

### 6.2 `manifest.json`

清单至少包含：

- `schema_version`
- `backup_id`
- `created_at_utc`
- `source_codex_version`
- `source_platform`
- 逻辑源根（只写 `$CODEX_HOME`，不写用户名或绝对主目录）
- 包含的会话数量、rollout 文件数量、附件数量和总字节数
- 每个文件的相对路径、类型、大小和 SHA-256
- 每个 rollout 的会话 ID、来源类别（active/archived）和相对路径
- 重复会话 ID 分组
- 明确的排除记录：仅包含排除 ID、原因和匹配文件数量，不包含活动会话内容
- 导出器版本和归档参数

清单按稳定键排序并使用规范 JSON 序列化，使同一 staging 内容可重复校验。它既放入归档，也作为独立 Release 资产发布；两份内容必须逐字节一致。

### 6.3 `SHA256SUMS`

归档内部的 `SHA256SUMS` 覆盖除其自身外的所有文件，包括 `manifest.json`。Release 另有 `<archive>.sha256`，覆盖整个压缩归档。仓库保存外层校验值和公开清单的副本，便于无需下载归档即可确认预期资产。

## 7. Git 仓库职责

计划中的仓库布局：

```text
README.md
SECURITY-NOTICE.md
scripts/export_codex_history.py
scripts/restore_codex_history.py
scripts/verify_backup.py
tests/test_export.py
tests/test_restore.py
tests/test_verify.py
docs/superpowers/specs/
manifests/
checksums/
```

Git 历史只保存小型文本文件。`.tar.zst`、分卷和 staging 目录必须由 `.gitignore` 排除。大型归档只上传到 Release，既避免 Git 的 100 MiB 单对象限制，也避免每次快照永久膨胀仓库历史。

## 8. 发布事务

发布按以下顺序执行：

1. 在独立临时目录建立 staging 快照。
2. 完成允许列表、活动会话负向检查、逐文件 SHA-256 和结构校验。
3. 生成归档并验证能完整解压到另一个临时目录。
4. 对解压结果重新执行内部校验，确认 manifest 与独立资产一致。
5. 提交并推送脚本、清单副本和外层 checksum。
6. 创建 GitHub Release 并上传归档、checksum 和 manifest 资产。
7. 从 GitHub Release 重新下载公开资产到新的临时目录，执行一次端到端校验。
8. 只有步骤 7 成功才把该 Release 视为完成；失败时保留失败状态和诊断，不把未验证资产描述成可恢复备份。

本项目没有“对话内容秘密扫描”发布闸门。发布前仍执行结构性安全检查，确保归档成员全部来自允许列表，且认证、配置、数据库和被排除会话没有误入。

## 9. 目标电脑上的恢复流程

恢复器默认只做 `--dry-run`，并要求用户显式指定目标 `CODEX_HOME` 才能写入。流程如下：

1. 下载 Release 资产、外层 checksum 和 manifest。
2. 在解压前验证归档 SHA-256。
3. 拒绝绝对路径、`..`、路径穿越、符号链接和非普通文件，安全解压到 staging。
4. 校验内部 `SHA256SUMS`、manifest 架构、文件计数、大小和会话 ID。
5. 确认排除 ID 不在待恢复集合。
6. 生成合并计划，逐项标记 `add`、`skip-identical` 或 `conflict`。
7. 用户取消 dry-run 后，以“只新增、不覆盖”策略逐文件安装。
8. 写入恢复日志，记录本次新增文件，支持只删除本次新增项的可审计回滚。
9. 启动相同或兼容版本 Codex，运行 `codex resume --all`；必要时用 manifest 中的会话 UUID 执行 `codex resume <SESSION_ID>` 烟雾验证。

### 9.1 冲突规则

- 目标路径不存在：新增。
- 目标路径存在且 SHA-256 相同：跳过。
- 目标路径存在但内容不同：绝不覆盖；把备份版本留在 staging 的 `conflicts/` 报告区，并以非零状态结束。
- 目标已有同一会话 ID、但路径不同或内容不同：视为语义冲突，绝不自动择一。
- 恢复器不写入或替换 Codex SQLite 数据库，也不尝试编辑运行中的 Codex 状态。

建议在目标电脑关闭 Codex 后执行实际合并。即使 Codex 尚未关闭，dry-run 和解压 staging 也不能写目标 `CODEX_HOME`。

## 10. 校验与测试要求

### 10.1 自动化测试

测试使用临时伪造的 `CODEX_HOME`，不得依赖或改写真实历史。至少覆盖：

- sessions 与 archived_sessions 的正常导出。
- 精确排除指定会话 ID，包括同一 ID 出现在多个文件时。
- 被排除 ID 不出现在归档、公开 manifest 或 checksum 列表。
- 只复制已选择 rollout 所引用的附件。
- 共享附件与排除会话发生交集时隐私优先。
- 源文件在复制中变化时整个导出失败。
- 符号链接、路径穿越和允许根之外文件被拒绝。
- 重复会话 ID 被保留并在 manifest 标记。
- 外层归档校验失败、内部文件校验失败和 manifest 被篡改时拒绝恢复。
- dry-run 不产生目标目录写入。
- 相同文件幂等跳过，内容冲突不覆盖。
- 回滚只删除本次新增且摘要仍匹配的文件。
- 全流程打包、解压、恢复后的文件字节与源 staging 一致。

### 10.2 实际快照验收

首次发布必须满足全部条件：

- 源 `CODEX_HOME` 在导出前后没有由导出器造成的任何变化。
- 活动会话仍可继续使用，其 rollout 没有进入 staging 或 Release。
- 归档成员全部属于本规格的允许路径。
- 永久禁止项命中数为零。
- 所有内部和外部 SHA-256 校验通过。
- Release 下载回环校验通过。
- 在隔离的临时 `CODEX_HOME` 中恢复成功且二次运行保持幂等。
- 至少抽取一个非敏感历史会话 ID，确认兼容版本的 Codex 能列出或恢复它。
- 公开 manifest、checksum 与 Release 资产相互对应。

## 11. 错误处理与可观测性

- 默认 fail-closed：无法识别会话 ID、源文件变化、缺失附件、校验失败或冲突时，不创建“成功”Release。
- 命令输出显示阶段、计数、总大小和明确失败原因，不打印对话正文或附件正文。
- 本地详细报告可包含源相对路径，但不能包含提示词、回答、附件文本或认证数据。
- staging 保存在系统临时目录；成功后删除。失败时默认保留路径供用户检查，但不得自动加入 Git。
- GitHub 上传中断可重试同一个 draft Release；校验成功前不发布为正式 Release。

## 12. 非目标

本项目不承诺：

- 实时或双向同步两台电脑上的会话。
- 在 Codex 正在写入同一 rollout 时为其制作崩溃一致性快照。
- 迁移登录令牌、账号状态、配置、插件、技能、缓存、终端状态或浏览器状态。
- 复制或合并 SQLite 索引。
- 保证不同 Codex 主版本之间所有 UI 元数据完全一致。
- 把被排除的活动会话或其专属附件放入首个 Release。
- 自动把 GitHub 仓库从 Public 改为 Private。
- 自动删改、脱敏或扫描历史对话中的秘密。

## 13. 完成定义

本次任务只有在以下结果全部成立时才完成：

1. 仓库中有可复现的导出、验证和恢复工具及其测试。
2. GitHub 上存在一个通过下载回环校验的 Release 快照。
3. 快照不包含指定活动会话或永久禁止项。
4. 用户能在另一台电脑先 dry-run，再无覆盖地恢复历史。
5. 恢复后的 Codex 能通过 `codex resume --all` 找到历史会话，且至少一个会话完成烟雾验证。
6. README 明确记录 Public、未加密、无内容秘密扫描闸门，以及用户稍后自行改为 Private 的决定。
