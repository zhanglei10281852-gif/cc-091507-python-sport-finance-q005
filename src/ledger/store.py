"""JSON 文件持久化。所有状态写入单个文件，保存时先写临时文件再原子替换。"""
from __future__ import annotations

import json
import os
from pathlib import Path


class Store:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
