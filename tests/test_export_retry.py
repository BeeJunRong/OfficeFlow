"""异常场景测试：E1 导出失败/保留/重试 + E2 空输入（guardsrails §4）。"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from tests.samples.samples import get_sample

client = TestClient(app)


def _generate(sample_id: str = "E1") -> dict:
    sample = get_sample(sample_id)
    resp = client.post(
        "/api/generate",
        json={"input_text": sample["input"], "task_type": "meeting_minutes"},
    )
    assert resp.status_code == 200
    return resp.json()


def test_e1_export_failure_retry_and_no_duplicates():
    """E1：注入导出失败 → 明确提示、内容保留、重试成功、无重复导出记录。"""
    gen = _generate("E1")
    session_id = gen["session_id"]
    content = gen["markdown"]

    # 第一次导出：注入失败
    fail = client.post(
        "/api/export",
        json={
            "session_id": session_id,
            "content": content,
            "filename": "e1_minutes",
            "inject_failure": True,
        },
    ).json()
    assert fail["success"] is False
    assert "导出失败" in fail["error_message"]
    assert "重试" in fail["error_message"], "失败提示必须包含可重试指引"
    assert not fail.get("file_path"), "失败时不得声称有导出文件"

    # 内容保留：会话仍可查询到生成结果（工具状态可查）
    status = client.get(f"/api/status/{session_id}").json()
    assert status["tool_logs"], "内容/状态丢失"
    exports = [e for e in status["export_records"] if e["status"] == "failed"]
    assert len(exports) == 1

    # 重试成功（恢复后）
    retry = client.post(
        "/api/retry",
        json={
            "session_id": session_id,
            "content": content,
            "filename": "e1_minutes",
            "inject_failure": False,
        },
    ).json()
    assert retry["success"] is True, retry["error_message"]
    exported = Path(retry["file_path"]).read_text(encoding="utf-8")
    assert exported == content, "导出内容与编辑后内容不一致"

    # 无重复导出记录：重试成功后成功记录仅一条
    status2 = client.get(f"/api/status/{session_id}").json()
    success_records = [e for e in status2["export_records"] if e["status"] == "success"]
    assert len(success_records) == 1, "出现重复导出记录"


def test_e1_export_idempotent_no_duplicate_files(tmp_path):
    """E1 补充：多次重试成功不产生重复文件（原子替换）。"""
    gen = _generate("E1")
    for _ in range(3):
        ok = client.post(
            "/api/retry",
            json={
                "session_id": gen["session_id"],
                "content": gen["markdown"],
                "filename": "e1_idempotent",
            },
        ).json()
        assert ok["success"] is True
    files = list(Path(ok["file_path"]).parent.glob("e1_idempotent*"))
    assert len(files) == 1, f"产生重复文件: {files}"


def test_e2_empty_input_blocked():
    """E2：空输入阻止生成并提示，不产生会话。"""
    sample = get_sample("E2")
    resp = client.post(
        "/api/generate",
        json={"input_text": sample["input"], "task_type": "meeting_minutes"},
    )
    assert resp.status_code == 400
    assert "非空" in resp.json()["detail"]


def test_export_empty_content_rejected():
    resp = client.post(
        "/api/export",
        json={"session_id": "abc", "content": "   ", "filename": "x"},
    )
    assert resp.status_code == 400


def test_export_is_explicit_only():
    """Q-02：generate 不产生导出记录（导出必须显式触发）。"""
    gen = _generate("N1")
    status = client.get(f"/api/status/{gen['session_id']}").json()
    assert status["export_records"] == [], "未经显式点击出现导出记录，违反 Q-02"


def test_download_path_traversal_blocked():
    resp = client.get("/api/download", params={"file_path": "/etc/passwd"})
    assert resp.status_code == 403
