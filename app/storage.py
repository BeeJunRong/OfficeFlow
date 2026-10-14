"""SQLite 轻量日志存储（architecture.md §8，可选，仅用于验收证据）。

不参与核心生成流程；未启用数据库时所有查询静默返回空。
表结构：sessions / results / tool_logs。
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(os.getenv("OFFICEFLOW_DB_PATH", "officeflow_logs.db"))


class LogStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.enabled = os.getenv("OFFICEFLOW_ENABLE_DB", "1") == "1"
        if self.enabled:
            self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    task_type  TEXT NOT NULL,
                    input_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS results (
                    session_id TEXT PRIMARY KEY,
                    structured_output TEXT,
                    edited_output TEXT,
                    export_status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS tool_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT DEFAULT '',
                    timestamp TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS export_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    file_path TEXT,
                    status TEXT NOT NULL,
                    error_message TEXT DEFAULT '',
                    timestamp TEXT NOT NULL
                );
                """
            )

    # ---- 写入 ----

    def save_session(self, session_id: str, task_type: str, input_text: str) -> None:
        if not self.enabled:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                (session_id, task_type, input_text, datetime.now(timezone.utc).isoformat()),
            )

    def save_result(self, session_id: str, structured: dict[str, Any], status: str) -> None:
        if not self.enabled:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO results VALUES (?,?,?,?)",
                (session_id, json.dumps(structured, ensure_ascii=False), "", status),
            )

    def add_tool_log(
        self, session_id: str, tool_name: str, status: str, error_message: str = ""
    ) -> None:
        if not self.enabled:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO tool_logs (session_id, tool_name, status, error_message, timestamp)"
                " VALUES (?,?,?,?,?)",
                (
                    session_id,
                    tool_name,
                    status,
                    error_message,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def add_export_record(
        self, session_id: str, file_path: str, status: str, error_message: str = ""
    ) -> None:
        """导出记录：失败与成功都记录；重试成功后状态覆盖，不产生重复成功记录。"""
        if not self.enabled:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO export_records (session_id, file_path, status, error_message, timestamp)"
                " VALUES (?,?,?,?,?)",
                (
                    session_id,
                    file_path,
                    status,
                    error_message,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            if status == "success":
                # 同一 session 只保留一条成功记录（E1：重试后无重复导出记录）
                conn.execute(
                    "DELETE FROM export_records WHERE session_id=? AND status='success'"
                    " AND id < last_insert_rowid()",
                    (session_id,),
                )

    # ---- 查询 ----

    def get_status(self, session_id: str) -> dict[str, Any]:
        """GET /api/status/{session_id} 数据源：工具日志 + 导出记录。"""
        if not self.enabled:
            return {"session_id": session_id, "tool_logs": [], "export_records": []}
        with self._conn() as conn:
            logs = [
                dict(r)
                for r in conn.execute(
                    "SELECT tool_name, status, error_message, timestamp FROM tool_logs"
                    " WHERE session_id=? ORDER BY id",
                    (session_id,),
                )
            ]
            exports = [
                dict(r)
                for r in conn.execute(
                    "SELECT file_path, status, error_message, timestamp FROM export_records"
                    " WHERE session_id=? ORDER BY id",
                    (session_id,),
                )
            ]
            session = conn.execute(
                "SELECT task_type, created_at FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return {
            "session_id": session_id,
            "task_type": session["task_type"] if session else None,
            "tool_logs": logs,
            "export_records": exports,
        }

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """取会话基础信息（派生时需要源会话的 input_text 与 task_type）。"""
        if not self.enabled:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT session_id, task_type, input_text, created_at FROM sessions"
                " WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_structured_result(self, session_id: str) -> dict[str, Any] | None:
        """取会话的结构化产物（派生时复用，避免重新抽取）。"""
        if not self.enabled:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT structured_output FROM results WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row or not row["structured_output"]:
            return None
        try:
            parsed = json.loads(row["structured_output"])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


_store: LogStore | None = None


def get_store() -> LogStore:
    global _store
    if _store is None:
        _store = LogStore()
    return _store
