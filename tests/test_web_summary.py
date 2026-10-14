"""FR-04 网页整理测试：URL 提取、抓取（mock）、失败处理、要点抽取。"""

from fastapi.testclient import TestClient

import app.tools.web_fetch as web_fetch
from app.main import app
from app.tools.web_fetch import extract_url, fetch_page

client = TestClient(app)

MOCK_HTML = """
<html>
  <head><title>会议纪要写作指南</title></head>
  <body>
    <script>var tracking = 1;</script>
    <nav>导航菜单</nav>
    <p>会议纪要应包含会议日期、主题、关键决定与待办事项四个部分。</p>
    <p>待办事项需要记录负责人与截止日期，缺失信息应标注待确认。</p>
    <p>纪要生成后应由参会人员核对，确认无误后再进行归档与分享。</p>
    <footer>版权所有</footer>
  </body>
</html>
"""

LOGIN_HTML = """
<html><head><title>系统登录</title></head><body>
<form>用户名 <input name="user"><br>密码 <input type="password" name="password">
<button>登录</button></form>
</body></html>
"""


def test_extract_url():
    assert extract_url("请整理这个网页：https://example.com/article?id=1，谢谢") == \
        "https://example.com/article?id=1"
    assert extract_url("没有任何链接的文本") is None


def _mock_get(html: str, status: int = 200):
    """构造 mock 的 httpx.get，返回固定 HTML。"""

    class _MockResp:
        def __init__(self):
            self.status_code = status
            self.headers = {"content-type": "text/html; charset=utf-8"}
            self.text = html
            self.url = "https://example.com/page"

        def raise_for_status(self):
            if self.status_code >= 400:
                raise web_fetch.httpx.HTTPError(f"{self.status_code}")

    def _get(url, **kwargs):
        return _MockResp()

    return _get


def test_fetch_page_extracts_title_and_text(monkeypatch):
    monkeypatch.setattr(web_fetch.httpx, "get", _mock_get(MOCK_HTML))
    page, error = fetch_page("https://example.com/page")
    assert error == ""
    assert page is not None
    assert page.title == "会议纪要写作指南"
    assert "会议日期、主题、关键决定与待办事项" in page.text
    assert "tracking" not in page.text          # script 已剥离
    assert "导航菜单" not in page.text           # nav 已剥离
    assert "版权所有" not in page.text           # footer 已剥离


def test_fetch_page_rejects_login_page(monkeypatch):
    monkeypatch.setattr(web_fetch.httpx, "get", _mock_get(LOGIN_HTML))
    page, error = fetch_page("https://example.com/login")
    assert page is None
    assert "登录" in error or "login" in error.lower()


def test_fetch_page_network_error(monkeypatch):
    def _raise(url, **kwargs):
        raise web_fetch.httpx.ConnectError("refused")

    monkeypatch.setattr(web_fetch.httpx, "get", _raise)
    page, error = fetch_page("https://down.example.com")
    assert page is None
    assert "抓取失败" in error


def test_generate_api_web_summary(monkeypatch):
    monkeypatch.setattr(web_fetch.httpx, "get", _mock_get(MOCK_HTML))
    resp = client.post(
        "/api/generate",
        json={"input_text": "请整理网页内容：https://example.com/page", "task_type": "web_summary"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_type"] == "web_summary"
    result = data["web_summary"]
    assert result["title"] == "会议纪要写作指南"
    assert result["url"].startswith("https://example.com")
    assert len(result["points"]) >= 2
    # 工具链路：route → web_fetch → extract → render
    tools = [log["tool_name"] for log in data["tool_logs"]]
    assert tools == ["route", "web_fetch", "extract_fields", "render_markdown"]
    assert "# 网页整理" in data["markdown"]


def test_generate_api_no_url_error(monkeypatch):
    resp = client.post(
        "/api/generate",
        json={"input_text": "帮我整理一下这个网页", "task_type": "web_summary"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["error_message"] and "URL" in data["error_message"]
    # 失败也应有工具日志（验收证据）
    assert any(log["status"] == "failed" for log in data["tool_logs"])


def test_generate_api_fetch_failure_preserved(monkeypatch):
    def _raise(url, **kwargs):
        raise web_fetch.httpx.ConnectError("refused")

    monkeypatch.setattr(web_fetch.httpx, "get", _raise)
    resp = client.post(
        "/api/generate",
        json={"input_text": "整理 https://down.example.com 的内容", "task_type": "web_summary"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "抓取失败" in data["error_message"]
    assert "可重试" in data["error_message"]
