"""测试夹具：临时数据库与导出目录，保证测试可重复执行。"""

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("OFFICEFLOW_MODEL_API_KEY", "")  # 测试走规则引擎，离线确定性
os.environ["OFFICEFLOW_ENABLE_DB"] = "1"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个测试独立的 DB 与导出目录，避免污染工作区与重复记录。"""
    import importlib

    em = importlib.import_module("app.tools.export_markdown")
    from app.storage import LogStore

    db_path = tmp_path / "logs.db"
    export_dir = tmp_path / "exports"
    monkeypatch.setenv("OFFICEFLOW_DB_PATH", str(db_path))
    monkeypatch.setenv("OFFICEFLOW_EXPORT_DIR", str(export_dir))
    monkeypatch.setattr(em, "EXPORT_DIR", export_dir)

    import app.storage as storage

    monkeypatch.setattr(storage, "_store", LogStore(db_path))
    yield
