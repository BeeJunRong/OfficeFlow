"""Web 配置面板的后端测试（app/config_store.py + /api/config）。

重点覆盖安全约定：
- 密钥只回掩码、绝不回显明文；
- 白名单拦截未登记键；
- **空值 = 不修改**（前端表单留空不会抹掉已配置的 Key）；
- 显式 clear 才清空；
- 写入 .env 时保留原有注释与顺序；
- 模型配置写入后重置 LLM 单例（立即生效），路径类配置报告"需重启"。
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config_store
from app.config_store import (
    CONFIG_FIELDS,
    MASK,
    FIELD_BY_KEY,
    guess_preset,
    mask_value,
    public_view,
    read_config,
    write_config,
)
# 别名导入：直接叫 test_model 会被 pytest 误当成测试函数收集
from app.config_store import test_model as probe_model
from app.main import app

client = TestClient(app)

FAKE_KEY = "sk-test-1234567890abcdef"


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把 .env 指到临时文件，并在测试后还原所有被改动的环境变量。"""
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config_store, "ENV_PATH", env_file)

    # 快照并清理，避免读到真实 .env 载入的进程变量
    keys = [f.key for f in CONFIG_FIELDS]
    snapshot = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)

    yield env_file

    for k, v in snapshot.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    config_store.reset_llm_client()


# ---------- 掩码与读取 ----------


def test_mask_value_keeps_prefix_and_suffix():
    assert mask_value("") == ""
    assert mask_value("short") == MASK
    masked = mask_value(FAKE_KEY)
    assert masked.startswith("sk-tes")
    assert masked.endswith("cdef")
    assert FAKE_KEY not in masked


def test_read_config_never_returns_plaintext_secret(isolated_env):
    write_config({"OFFICEFLOW_MODEL_API_KEY": FAKE_KEY})
    view = read_config()
    item = next(i for i in view["items"] if i["key"] == "OFFICEFLOW_MODEL_API_KEY")
    assert item["secret"] is True
    assert item["configured"] is True
    assert FAKE_KEY not in item["value"], "密钥绝不能明文回传"
    assert "•" in item["value"]


def test_read_config_reports_model_ready(isolated_env):
    assert read_config()["model_ready"] is False
    write_config({"OFFICEFLOW_MODEL_API_KEY": FAKE_KEY})
    assert read_config()["model_ready"] is True


# ---------- 写入 ----------


def test_write_creates_env_and_syncs_environ(isolated_env):
    result = write_config({"OFFICEFLOW_MODEL_API_KEY": FAKE_KEY})
    assert isolated_env.is_file()
    assert "OFFICEFLOW_MODEL_API_KEY=" in isolated_env.read_text(encoding="utf-8")
    # 同步到进程环境变量 → 每次调用读 env 的模块立即生效
    assert os.environ["OFFICEFLOW_MODEL_API_KEY"] == FAKE_KEY
    assert result["applied"]["OFFICEFLOW_MODEL_API_KEY"] != FAKE_KEY, "返回值也必须掩码"


def test_write_preserves_comments_and_updates_in_place(isolated_env):
    isolated_env.write_text(
        "# 我的注释\nOFFICEFLOW_MODEL_NAME=old-model\n\n# 尾部注释\n",
        encoding="utf-8",
    )
    write_config({"OFFICEFLOW_MODEL_NAME": "new-model"})
    text = isolated_env.read_text(encoding="utf-8")
    assert "# 我的注释" in text
    assert "# 尾部注释" in text
    assert "OFFICEFLOW_MODEL_NAME=new-model" in text
    assert "old-model" not in text


def test_empty_value_is_ignored(isolated_env):
    """空字符串 = 用户没改，不得把已配置的 Key 抹掉。"""
    write_config({"OFFICEFLOW_MODEL_API_KEY": FAKE_KEY})
    result = write_config({"OFFICEFLOW_MODEL_API_KEY": "", "OFFICEFLOW_MODEL_NAME": "m2"})
    assert os.environ["OFFICEFLOW_MODEL_API_KEY"] == FAKE_KEY, "空值不应覆盖"
    assert "OFFICEFLOW_MODEL_API_KEY" in result["skipped"]
    assert os.environ["OFFICEFLOW_MODEL_NAME"] == "m2"


def test_clear_explicitly_removes_value(isolated_env):
    write_config({"OFFICEFLOW_MODEL_API_KEY": FAKE_KEY})
    write_config(clear=["OFFICEFLOW_MODEL_API_KEY"])
    assert os.environ.get("OFFICEFLOW_MODEL_API_KEY", "") == ""
    assert read_config()["model_ready"] is False


def test_whitelist_rejects_unknown_key(isolated_env):
    for bad in ["PATH", "OFFICEFLOW_NOT_A_FIELD", "PYTHONPATH"]:
        with pytest.raises(ValueError, match="不允许修改"):
            write_config({bad: "x"})
    with pytest.raises(ValueError, match="不允许修改"):
        write_config(clear=["PATH"])


def test_whitelist_rejects_unknown_in_clear(isolated_env):
    with pytest.raises(ValueError):
        write_config({}, ["EVIL_KEY"])


def test_invalid_int_and_url_rejected(isolated_env):
    with pytest.raises(ValueError, match="整数"):
        write_config({"OFFICEFLOW_MODEL_TIMEOUT": "abc"})
    with pytest.raises(ValueError, match="URL"):
        write_config({"OFFICEFLOW_MODEL_BASE_URL": "api.deepseek.com"})


def test_restart_required_reported(isolated_env):
    """导出目录/数据库路径在 import 时读取 → 必须提示需重启。"""
    result = write_config({"OFFICEFLOW_EXPORT_DIR": "out2"})
    assert "OFFICEFLOW_EXPORT_DIR" in result["restart_required"]

    result2 = write_config({"OFFICEFLOW_MODEL_NAME": "m3"})
    assert result2["restart_required"] == []


def test_model_change_resets_llm_singleton(isolated_env, monkeypatch):
    """写入模型配置后应重置 LLM 单例，使新 Key 无需重启即生效。"""
    import app.llm as llm_module

    sentinel = object()
    monkeypatch.setattr(llm_module, "_llm", sentinel, raising=False)
    write_config({"OFFICEFLOW_MODEL_API_KEY": FAKE_KEY})
    assert llm_module._llm is None, "LLM 单例应被重置"


def test_missing_env_file_is_creatable(isolated_env):
    assert not isolated_env.exists()
    assert read_config()["env_file_exists"] is False
    write_config({"OFFICEFLOW_BACKEND": "http://127.0.0.1:9000"})
    assert read_config()["env_file_exists"] is True


# ---------- 预设 ----------


def test_guess_preset():
    assert guess_preset("https://api.deepseek.com/v1") == "deepseek"
    assert guess_preset("https://api.deepseek.com/v1/") == "deepseek"
    assert guess_preset("http://localhost:11434/v1") == "ollama"
    assert guess_preset("https://my-gateway.internal/v1") == "custom"
    assert guess_preset("") == "custom"


def test_public_view_shape(isolated_env):
    view = public_view()
    assert view["presets"], "应返回服务商预设"
    assert {p["id"] for p in view["presets"]} >= {"deepseek", "qwen", "ollama", "openai", "custom"}
    assert view["secret_keys"] == ["OFFICEFLOW_MODEL_API_KEY", "OFFICEFLOW_SMTP_PASSWORD"]
    assert all("key" in i and "label" in i for i in view["items"])


# ---------- 连通性测试 ----------


def test_test_model_requires_key_for_remote(isolated_env):
    """远程端点未填 Key 时应直接拒绝，不发起请求。"""
    res = probe_model(base_url="https://api.deepseek.com/v1", api_key="", model_name="x")
    assert res["ok"] is False
    assert "API Key" in res["detail"]


def test_test_model_local_endpoint_needs_no_key(isolated_env):
    """本地端点不强制要求 Key（Ollama 场景），应走到连接失败而非"未填 Key"。"""
    res = probe_model(base_url="http://127.0.0.1:59999/v1", api_key="", model_name="qwen2.5:7b", timeout=3)
    assert res["ok"] is False
    assert "API Key" not in res["detail"], f"本地端点不应要求 Key：{res}"


# ---------- API 端点 ----------


def test_api_get_config_ok(isolated_env):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and "presets" in body
    assert FAKE_KEY not in resp.text


def test_api_post_config_rejects_unknown(isolated_env):
    resp = client.post("/api/config", json={"updates": {"EVIL": "1"}})
    assert resp.status_code == 400
    assert "不允许修改" in resp.json()["detail"]


def test_api_post_config_saves(isolated_env):
    resp = client.post(
        "/api/config",
        json={"updates": {"OFFICEFLOW_MODEL_API_KEY": FAKE_KEY, "OFFICEFLOW_MODEL_NAME": "deepseek-flash"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"]
    assert FAKE_KEY not in resp.text, "响应体不得包含明文 Key"
    assert os.environ["OFFICEFLOW_MODEL_NAME"] == "deepseek-flash"


def test_api_post_config_clear(isolated_env):
    write_config({"OFFICEFLOW_MODEL_API_KEY": FAKE_KEY})
    resp = client.post("/api/config", json={"updates": {}, "clear": ["OFFICEFLOW_MODEL_API_KEY"]})
    assert resp.status_code == 200
    assert os.environ.get("OFFICEFLOW_MODEL_API_KEY", "") == ""


def test_api_test_endpoint_shape(isolated_env):
    """测试端点应返回固定结构，便于前端直接展示。"""
    resp = client.post("/api/config/test", json={"base_url": "", "api_key": "", "model_name": ""})
    assert resp.status_code == 200
    assert "ok" in resp.json()
