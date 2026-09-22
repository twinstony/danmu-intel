"""原始弹幕记录契约（JSONL，只增不改 —— 设计 §5.2）。

每行一条，UTF-8，字段固定且顺序固定：

```json
{"ts":1758451200123,"platform":"huya","room_id":"660000","match_id":null,
 "user_hash":"3f9a…","text":"这波团开得太急了","extra":{}}
```

- `ts`：毫秒（epoch）。
- `user_hash`：平台用户 ID 的加盐哈希，**不落明文身份**。
- 写入用 `O_APPEND`，只增不改。

同场多房间聚合的去重键 `(platform, room_id, msg_hash)` 也在这里给（纯函数，见
`message_key` / `dedupe`）：键里带 `room_id` 意味着**不同房间的同文弹幕各算一条**。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Iterator

JSONL_FIELDS = ("ts", "platform", "room_id", "match_id", "user_hash", "text", "extra")
MSG_HASH_LENGTH = 32


@dataclass(frozen=True, slots=True)
class DanmuEvent:
    ts: int
    platform: str
    room_id: str
    user_hash: str
    text: str
    extra: dict[str, Any]
    match_id: int | None = None

    def with_match(self, match_id: int | None) -> "DanmuEvent":
        return replace(self, match_id=match_id)

    def to_json(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in JSONL_FIELDS}

    def to_line(self) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "DanmuEvent":
        missing = [field for field in JSONL_FIELDS if field not in payload]
        if missing:
            raise ValueError(f"弹幕记录缺少字段：{','.join(missing)}")
        ts = payload["ts"]
        match_id = payload["match_id"]
        extra = payload["extra"]
        if not isinstance(ts, int) or isinstance(ts, bool):
            raise ValueError("ts 必须是整数毫秒")
        if match_id is not None and not isinstance(match_id, int):
            raise ValueError("match_id 必须是整数或 null")
        if not isinstance(extra, dict):
            raise ValueError("extra 必须是对象")
        for field in ("platform", "room_id", "user_hash", "text"):
            if not isinstance(payload[field], str):
                raise ValueError(f"{field} 必须是字符串")
        return cls(
            ts=ts,
            platform=payload["platform"],
            room_id=payload["room_id"],
            user_hash=payload["user_hash"],
            text=payload["text"],
            extra=extra,
            match_id=match_id,
        )


def msg_hash(event: DanmuEvent) -> str:
    """一条记录的消息指纹（`msg_hash`）。

    平台不提供全局消息 id，所以指纹取自**落盘记录的稳定字段**
    `ts|user_hash|text`（平台与房间在去重键里另算）。同一条记录被重复落盘
    （重连补写、同一文件被读两遍），指纹相同，因此只算一条。
    """
    payload = f"{event.ts}|{event.user_hash}|{event.text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:MSG_HASH_LENGTH]


def message_key(event: DanmuEvent) -> tuple[str, str, str]:
    """跨房间去重键 `(platform, room_id, msg_hash)`（设计 §7.2；AC-15）。"""
    return (event.platform, event.room_id, msg_hash(event))


def dedupe(events: Iterable[DanmuEvent]) -> list[DanmuEvent]:
    """按去重键折叠重复记录，保留首次出现的顺序（纯函数，无 I/O、无时钟）。"""
    seen: set[tuple[str, str, str]] = set()
    kept: list[DanmuEvent] = []
    for event in events:
        key = message_key(event)
        if key not in seen:
            seen.add(key)
            kept.append(event)
    return kept


def decode_line(line: str) -> DanmuEvent:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"非法 JSONL 行：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSONL 行必须是 JSON 对象")
    return DanmuEvent.from_json(payload)


def count_lines(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def iter_events(path: Path) -> Iterator[tuple[int, DanmuEvent]]:
    """逐行读原始记录，产出 `(行号, 事件)`；行号从 1 开始（溯源引用要用）。"""
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if line:
                yield line_no, decode_line(line)


class JsonlAppender:
    """append-only 写入器：`O_APPEND` + 每条一次 write，崩溃最多丢最后一行。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    def append(self, event: DanmuEvent) -> None:
        os.write(self._fd, (event.to_line() + "\n").encode("utf-8"))

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> "JsonlAppender":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
