from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def rollout_name(thread_id: str, stamp: str = "2026-09-30T01-02-03") -> str:
    return f"rollout-{stamp}-{thread_id}.jsonl"


def write_rollout(
    codex_home: Path,
    *,
    bucket: str,
    relative_parent: str,
    filename_thread_id: str,
    metadata_thread_id: str | None,
    legacy_thread_id: str | None = None,
    attachment_paths: Iterable[Path] = (),
    prefix_lines: Iterable[str] = (),
    stamp: str = "2026-09-30T01-02-03",
) -> Path:
    parent = codex_home / bucket / relative_parent
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / rollout_name(filename_thread_id, stamp)
    records: list[str] = list(prefix_lines)
    if metadata_thread_id is not None or legacy_thread_id is not None:
        payload: dict[str, Any] = {}
        if metadata_thread_id is not None:
            payload["session_id"] = metadata_thread_id
        if legacy_thread_id is not None:
            payload["id"] = legacy_thread_id
        records.append(
            json.dumps(
                {"timestamp": "2026-09-30T01:02:03Z", "type": "session_meta", "payload": payload},
                separators=(",", ":"),
            )
        )
    if attachment_paths:
        records.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "nested": [
                            {"attachment": str(path)} for path in attachment_paths
                        ],
                        "ordinary": "/tmp/not-a-codex-attachment.txt",
                    },
                },
                separators=(",", ":"),
            )
        )
    path.write_text("\n".join(records) + "\n", encoding="utf-8")
    return path


def write_attachment(codex_home: Path, relative_path: str, content: str = "attachment") -> Path:
    path = codex_home / "attachments" / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path

