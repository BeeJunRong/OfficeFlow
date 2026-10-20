"""Regression: adding messages must not change the composer's delta path."""

from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_composer_keeps_mount_position_across_conversation_states():
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "frontend" / "app.py"),
        default_timeout=20,
    )
    # 已登录态：composer 才会挂载
    app.session_state["auth"] = {"token": "test-token", "username": "tester"}
    app.run()

    def mount_identity():
        assert not app.exception
        assert len(app.get("bidi_component")) == 1
        component = app.get("bidi_component")[0]
        position = next(i for i, node in app.main.children.items() if node is component)
        return position, component.proto.id

    initial = mount_identity()
    for count in (1, 3):
        app.session_state["turns"] = [
            {
                "user": f"测试消息 {i}",
                "response": {"markdown": "测试结果", "session_id": "test"},
                "animated": True,
                "export_result": None,
            }
            for i in range(count)
        ]
        app.run()
        assert mount_identity() == initial

    app.session_state["turns"] = []
    app.session_state["composer_epoch"] += 1
    app.run()
    assert mount_identity() == initial


def test_unauthenticated_state_shows_login_not_composer():
    """未登录时应渲染登录页，不挂载 composer。"""
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "frontend" / "app.py"),
        default_timeout=20,
    ).run()
    assert not app.exception
    assert len(app.get("bidi_component")) == 0
    assert len(app.get("tabs")) == 2  # 登录 + 注册

