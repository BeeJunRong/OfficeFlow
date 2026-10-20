"""用户认证（登录/注册）：SQLite 存储 + PBKDF2 口令散列 + Bearer 令牌。

- 口令使用 PBKDF2-SHA256（120,000 迭代）+ 每用户随机盐，明文绝不入库、绝不出库。
- 登录成功签发 secrets.token_hex(32) 令牌，存 auth_tokens 表，默认 7 天过期。
- 与工具日志共用 OFFICEFLOW_DB_PATH 指定的 SQLite 文件。
"""

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

PBKDF2_ITERATIONS = 120_000
TOKEN_TTL_DAYS = 7
USERNAME_MIN, USERNAME_MAX = 2, 32
PASSWORD_MIN = 6

DEFAULT_DB_PATH = Path(os.getenv("OFFICEFLOW_DB_PATH", "officeflow_logs.db"))


class AuthError(Exception):
    """注册/登录失败；status 用于 HTTP 映射（400 校验 / 401 凭证错 / 409 重复）。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )


class AuthStore:
    """users / auth_tokens 两表的轻量存储（原型定位：本地单机，无密码找回）。"""

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
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash BLOB NOT NULL,
                    salt BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )

    # ---- 注册 / 登录 ----

    def register(self, username: str, password: str) -> dict:
        """注册新用户；成功返回用户信息（不含口令材料）。"""
        username = (username or "").strip()
        if not (USERNAME_MIN <= len(username) <= USERNAME_MAX):
            raise AuthError(
                f"用户名长度需在 {USERNAME_MIN}-{USERNAME_MAX} 个字符之间", status=400
            )
        if any(ch.isspace() for ch in username):
            raise AuthError("用户名不能包含空白字符", status=400)
        if len(password or "") < PASSWORD_MIN:
            raise AuthError(f"密码至少 {PASSWORD_MIN} 位", status=400)

        salt = secrets.token_bytes(16)
        password_hash = _hash_password(password, salt)
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO users (username, password_hash, salt, created_at)"
                    " VALUES (?,?,?,?)",
                    (
                        username,
                        password_hash,
                        salt,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                user_id = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise AuthError("该用户名已被注册", status=409) from exc
        return {"id": user_id, "username": username}

    def verify(self, username: str, password: str) -> dict:
        """校验用户名/密码；成功返回用户信息，失败抛 401。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, salt FROM users WHERE username=?",
                ((username or "").strip(),),
            ).fetchone()
        if row is None or _hash_password(password or "", row["salt"]) != row["password_hash"]:
            raise AuthError("用户名或密码错误", status=401)
        return {"id": row["id"], "username": row["username"]}

    # ---- 令牌 ----

    def issue_token(self, user_id: int) -> str:
        """签发登录令牌（7 天有效）；顺带清理过期令牌。"""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=TOKEN_TTL_DAYS)
        token = secrets.token_hex(32)
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM auth_tokens WHERE expires_at < ?", (now.isoformat(),)
            )
            conn.execute(
                "INSERT INTO auth_tokens (token, user_id, created_at, expires_at)"
                " VALUES (?,?,?,?)",
                (token, user_id, now.isoformat(), expires.isoformat()),
            )
        return token

    def user_for_token(self, token: str) -> dict | None:
        """令牌换用户；无效或过期返回 None。"""
        if not token:
            return None
        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT t.user_id, u.username FROM auth_tokens t"
                " JOIN users u ON u.id = t.user_id"
                " WHERE t.token = ? AND t.expires_at >= ?",
                (token, now.isoformat()),
            ).fetchone()
        return {"id": row["user_id"], "username": row["username"]} if row else None

    def revoke_token(self, token: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM auth_tokens WHERE token=?", (token,))


_auth_store: AuthStore | None = None


def get_auth_store() -> AuthStore:
    global _auth_store
    if _auth_store is None:
        _auth_store = AuthStore()
    return _auth_store
