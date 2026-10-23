"""Small JSON-file stores kept in the private data directory."""
from __future__ import annotations

import json
import os
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

PathLike = Union[str, os.PathLike]


def ensure_private_dir(path: PathLike) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def append_private_line(path: PathLike, line: str) -> None:
    """Append one line to a file that is created with mode 0600."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")


class JsonFile:
    def __init__(self, path: PathLike) -> None:
        self.path = Path(path)

    def read(self, default: Any) -> Any:
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return default

    def write(self, data: Any) -> None:
        """Write atomically: a 0600 temp file in the same directory, then os.replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class HistoryStore:
    """Recent messages, kept separately per business connection and chat."""

    def __init__(self, path: PathLike, limit: int) -> None:
        self._file = JsonFile(path)
        self.limit = limit
        data = self._file.read({})
        self._chats: Dict[str, List[Dict[str, Any]]] = data if isinstance(data, dict) else {}

    @staticmethod
    def key(connection_id: str, chat_id: int) -> str:
        return f"{connection_id}:{chat_id}"

    def get(self, connection_id: str, chat_id: int) -> List[Dict[str, Any]]:
        return [dict(item) for item in self._chats.get(self.key(connection_id, chat_id), [])]

    def add(
        self,
        connection_id: str,
        chat_id: int,
        role: str,
        name: str,
        text: str,
        lang: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> None:
        items = self._chats.setdefault(self.key(connection_id, chat_id), [])
        items.append({
            "role": role,
            "name": name,
            "text": text,
            "lang": lang,
            "ts": time.time() if ts is None else ts,
        })
        del items[:-self.limit]
        self._file.write(self._chats)

    def last_language(self, connection_id: str, chat_id: int) -> Optional[str]:
        for item in reversed(self._chats.get(self.key(connection_id, chat_id), [])):
            if item.get("lang"):
                return item["lang"]
        return None

    def chat_count(self) -> int:
        return len(self._chats)


class SeenMessages:
    """Remembers processed message keys (also across restarts) to drop duplicates."""

    def __init__(self, path: PathLike, capacity: int = 5000) -> None:
        self._file = JsonFile(path)
        self.capacity = capacity
        data = self._file.read([])
        self._keys: "OrderedDict[str, None]" = OrderedDict(
            (str(key), None) for key in (data if isinstance(data, list) else [])
        )

    def first_time(self, key: str) -> bool:
        if key in self._keys:
            return False
        self._keys[key] = None
        while len(self._keys) > self.capacity:
            self._keys.popitem(last=False)
        self._file.write(list(self._keys))
        return True


class RuntimeState:
    """Pause flag and usage-limit deadline; both survive restarts."""

    def __init__(self, path: PathLike) -> None:
        self._file = JsonFile(path)
        data = self._file.read({})
        data = data if isinstance(data, dict) else {}
        self._paused = bool(data.get("paused", False))
        limit = data.get("limit_until")
        self._limit_until: Optional[float] = float(limit) if isinstance(limit, (int, float)) else None

    def _save(self) -> None:
        self._file.write({"paused": self._paused, "limit_until": self._limit_until})

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = bool(value)
        self._save()

    @property
    def limit_until(self) -> Optional[float]:
        return self._limit_until

    @limit_until.setter
    def limit_until(self, value: Optional[float]) -> None:
        self._limit_until = value
        self._save()
