"""JSON 文件持久化。

所有集合保存在单个 JSON 文件中（默认 `.runtime/ledger.json`），写入采用
临时文件 + 原子替换，避免进程中断留下半个文件。测试可传 path=None 走纯内存。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

COLLECTIONS = (
    "players",
    "courses",
    "policies",
    "prescriptions",
    "pre_authorizations",
    "receipts",
    "claims",
    "payment_batches",
)


def empty_store() -> dict:
    data = {"counters": {}, "events": [], "consumptions": []}
    for name in COLLECTIONS:
        data[name] = {}
    return data


class JsonStore:
    def __init__(self, path: str | os.PathLike | None) -> None:
        self.path = Path(path) if path is not None else None
        self.data = empty_store()
        self.load()

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp_path, self.path)
