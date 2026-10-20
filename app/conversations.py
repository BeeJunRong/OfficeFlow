"""对话持久化：每个用户多个对话，对话内保存完整轮次（turns JSON）。

- 表 conversations(id, user_id, title, turns, created_at, updated_at)。
- 所有操作都带 user_id 校验：用户之间互相不可见（隔离由调用方传入 user_id 保证）。
- turns 是前端整个对话流的 JSON 序列（user/response/edited/…），整存整取。
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(os.getenv("OFFICEFLOW_DB_PATH", "officeflow_logs.db"))


class ConversationStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '新对话',
                    turns TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversations_user
                    ON conversations(user_id, updated_at DESC);
                """
            )

    @staticmethod
    def _conv_id() -> str:
        return uuid.uuid4().hex[:12]

    # ---- CRUD ----

    def list(self, user_id: int) -> list[dict[str, Any]]:
        """该用户的对话列表（不含 turns，最近更新在前）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, title, updated_at FROM conversations"
                " WHERE user_id=? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def create(self, user_id: int, title: str = "新对话") -> dict[str, Any]:
        conv_id = self._conv_id()
        now = datetime.now(timezone.utc).isoformat()
        title = (title or "新对话").strip() or "新对话"
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO conversations (id, user_id, title, turns, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (conv_id, user_id, title, "[]", now, now),
            )
        return {"id": conv_id, "title": title, "updated_at": now}

    def get(self, conv_id: str, user_id: int) -> dict[str, Any] | None:
        """取单个对话（含 turns）；不属于该用户返回 None。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, title, turns, updated_at FROM conversations"
                " WHERE id=? AND user_id=?",
                (conv_id, user_id),
            ).fetchone()
        if row is None:
            return None
        conv = dict(row)
        try:
            conv["turns"] = json.loads(conv.get("turns") or "[]")
        except json.JSONDecodeError:
            conv["turns"] = []
        return conv

    def update(
        self,
        conv_id: str,
        user_id: int,
        title: str | None = None,
        turns: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """更新标题和/或轮次；不属于该用户返回 None。"""
        existing = self.get(conv_id, user_id)
        if existing is None:
            return None
        new_title = title.strip() if title and title.strip() else existing["title"]
        new_turns = turns if turns is not None else existing["turns"]
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE conversations SET title=?, turns=?, updated_at=?"
                " WHERE id=? AND user_id=?",
                (
                    new_title,
                    json.dumps(new_turns, ensure_ascii=False),
                    now,
                    conv_id,
                    user_id,
                ),
            )
        return {"id": conv_id, "title": new_title, "updated_at": now}

    def delete(self, conv_id: str, user_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM conversations WHERE id=? AND user_id=?",
                (conv_id, user_id),
            )
        return cur.rowcount > 0


_conv_store: ConversationStore | None = None


def get_conv_store() -> ConversationStore:
    global _conv_store
    if _conv_store is None:
        _conv_store = ConversationStore()
    return _conv_store
