# Security notice

## Public, unencrypted conversation data

This backup format is intentionally unencrypted. GitHub Release archive assets contain the original selected Codex JSONL conversation records and their referenced attachments. The manifest also exposes selected conversation UUIDs, paths, sizes, hashes, counts, timestamps, platform and Codex version metadata.

The tools do **not** scan, redact, classify or remove secrets, credentials, personal data, source code, pasted text or other sensitive conversation content. A structurally valid backup may therefore contain sensitive material. Publishing it to a public repository makes it readable and copyable by anyone, including through mirrors, caches and forks that may remain after the repository is made private or an asset is deleted.

For the first approved backup, conversation `01a0e7c5-0d85-7c82-b4ee-f50f37e86a0b` is explicitly excluded because it is still active. Exclusion applies to every discovered rollout whose own `payload.id` or parent/root `payload.session_id` matches that ID and, conservatively, to attachments referenced by those excluded records. The manifest intentionally retains one exclusion record containing the ID so the omission is auditable.

The repository owner has approved publishing the backup publicly without encryption or a content-scanning upload gate and intends to change the repository to Private later. Making it private later does not revoke copies already downloaded while it was public.

Authentication files, Codex configuration, SQLite state, logs, caches and plugins are outside the exporter allowlist. This reduces scope but is not a secrecy guarantee for conversation text or attachments.

Before publishing any later backup, review the repository visibility and manifest, verify the exclusion list, and assume every selected byte will become public.
