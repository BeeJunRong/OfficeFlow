"""网页读取工具（FR-04，architecture.md §6 工具表）。

约束（guardsrails §7 / proposal §5）：
- 仅单页读取：一次只取一个 URL，不做批量爬取。
- 不处理需要登录的网页：发现登录表单/重定向到登录页时明确报错。
- 抓取失败返回错误信息，由上层记录工具日志并支持重试。
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

import httpx

MAX_BYTES = 512 * 1024  # 单页上限，防止超大页面拖垮原型
FETCH_TIMEOUT = 20.0

_URL_RE = re.compile(r"https?://[^\s，。；、)）\]】\"']+")


@dataclass
class WebPage:
    url: str
    title: str
    text: str  # 去标签后的正文（段落以 \n\n 分隔）


class _TextExtractor(HTMLParser):
    """极简 HTML → 纯文本提取：跳过 script/style 等噪音，保留块级段落分隔。"""

    SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "svg"}
    BLOCK_TAGS = {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "h5", "h6",
                  "br", "tr", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._title = ""
        self._in_title = False

    @property
    def title(self) -> str:
        return self._title.strip()

    def text(self) -> str:
        raw = "".join(self._chunks)
        # 压缩空白：段内换行折叠，段落间保持双换行
        paragraphs = [re.sub(r"\s+", " ", p).strip() for p in raw.split("\n\n")]
        return "\n\n".join(p for p in paragraphs if p)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK_TAGS:
            self._chunks.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK_TAGS:
            self._chunks.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title += data
        elif self._skip_depth == 0 and data.strip():
            self._chunks.append(data)


def extract_url(text: str) -> Optional[str]:
    """从用户输入中提取第一个 URL。"""
    m = _URL_RE.search(text)
    return m.group(0) if m else None


def fetch_page(url: str) -> tuple[Optional[WebPage], str]:
    """抓取单页并抽取标题与正文；返回 (页面, 错误信息)，二者互斥。"""
    try:
        resp = httpx.get(
            url,
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (OfficeFlow prototype)"},
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return None, f"网页抓取失败：{exc}，可重试"

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return None, f"不支持的内容类型：{content_type or '未知'}（仅支持网页/文本）"

    body = resp.text[:MAX_BYTES]
    # 登录页粗判：正文里出现典型登录表单特征即拒绝（不做任何登录）
    if re.search(r"(登录|login|sign[- ]?in).{0,80}(password|密码|账号|验证码)", body, re.I) or \
       re.search(r"(password|密码).{0,80}(登录|login|sign[- ]?in)", body, re.I):
        return None, "该网页疑似需要登录（检测到登录表单），首版不支持，请提供无需登录的页面"

    extractor = _TextExtractor()
    try:
        extractor.feed(body)
        extractor.close()
    except Exception as exc:  # noqa: BLE001 - 容忍畸形 HTML
        return None, f"网页解析失败：{exc}，可重试"

    page = WebPage(url=str(resp.url), title=extractor.title or "待确认", text=extractor.text())
    if not page.text:
        return None, "网页正文为空（可能是纯脚本渲染页面），无法整理"
    return page, ""
