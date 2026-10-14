"""配置读写（Web 配置面板的后端）。

设计目标：让用户**不碰命令行**就能选模型、填 Key，并尽量**立即生效**。

生效时机（依据各模块读取环境变量的位置）：
- **立即生效**：模型 Key / Base URL / 模型名（重置 LLM 单例即可）、
  LangExtract 开关、SMTP 配置、邮件发送总开关、后端地址
  —— 这些模块都是"每次调用时读环境变量"或可通过重置单例刷新。
- **需重启后端**：导出目录、数据库路径、启用日志库
  —— `app/storage.py` 与 `app/tools/export_markdown.py` 在 **import 时** 就读了这些变量。

安全约定：
1. **白名单**：只有 `CONFIG_FIELDS` 里登记的键可写，避免任意环境变量注入。
2. **密钥不回显**：`read_config()` 对 `SECRET_KEYS` 只返回掩码与"是否已配置"，
   绝不把 Key 完整回传前端。
3. **空值 = 不修改**：只有显式放进 `clear` 的键才会被清空，
   避免前端表单没填的字段把已有配置抹掉。
4. 写入 `UTF-8 无 BOM`（BOM 会让第一个变量名解析失败）。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

logger = logging.getLogger("officeflow.config")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"

MASK = "••••••••"


# ---------- 字段登记 ----------

@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    group: str
    default: str = ""
    secret: bool = False
    kind: str = "text"          # text / bool / int / path / url / password
    help: str = ""
    restart_required: bool = False
    choices: tuple[str, ...] = field(default_factory=tuple)


CONFIG_FIELDS: tuple[ConfigField, ...] = (
    # ---- 模型服务（核心）----
    ConfigField(
        "OFFICEFLOW_MODEL_API_KEY", "API Key", "model",
        secret=True, kind="password",
        help="在模型服务商控制台创建。本地 Ollama 无需真实 Key，随便填一个占位值即可。",
    ),
    ConfigField(
        "OFFICEFLOW_MODEL_BASE_URL", "Base URL", "model",
        default="https://api.deepseek.com/v1", kind="url",
        help="OpenAI 兼容端点地址，通常以 /v1 结尾。",
    ),
    ConfigField(
        "OFFICEFLOW_MODEL_NAME", "模型名", "model",
        default="deepseek-chat",
        help="填错会导致调用 404；建议用上方预设自动填充。",
    ),
    ConfigField(
        "OFFICEFLOW_MODEL_TIMEOUT", "超时（秒）", "model",
        default="60", kind="int",
    ),
    # ---- 抽取引擎 ----
    ConfigField(
        "OFFICEFLOW_LANGEXTRACT_ENABLE", "启用 LangExtract 溯源抽取", "engine",
        default="0", kind="bool",
        help="开启后抽取结果带字符级原文坐标，对齐失败的结果会被丢弃（更强的零编造保证）。",
    ),
    # ---- 邮件发送（Q-02 相关）----
    ConfigField(
        "OFFICEFLOW_EMAIL_ENABLE_SEND", "允许真实发送邮件", "email",
        default="0", kind="bool",
        help="总开关。为 0 时即使选 smtp 协议也只预览、绝不发送（Q-02 默认合规）。",
    ),
    ConfigField("OFFICEFLOW_SMTP_HOST", "SMTP 服务器", "email"),
    ConfigField("OFFICEFLOW_SMTP_PORT", "SMTP 端口", "email", default="587", kind="int"),
    ConfigField("OFFICEFLOW_SMTP_USER", "SMTP 用户名", "email"),
    ConfigField("OFFICEFLOW_SMTP_PASSWORD", "SMTP 密码/授权码", "email", secret=True, kind="password"),
    ConfigField("OFFICEFLOW_SMTP_FROM", "发件人地址", "email"),
    ConfigField("OFFICEFLOW_SMTP_TLS", "使用 STARTTLS", "email", default="1", kind="bool"),
    # ---- 应用 ----
    ConfigField(
        "OFFICEFLOW_BACKEND", "后端地址", "app",
        default="http://localhost:8000", kind="url",
        help="前端连接的后端地址；改动需刷新页面生效。",
    ),
    ConfigField(
        "OFFICEFLOW_EXPORT_DIR", "导出目录", "app",
        default="exports", kind="path", restart_required=True,
    ),
    ConfigField(
        "OFFICEFLOW_DB_PATH", "日志数据库路径", "app",
        default="officeflow_logs.db", kind="path", restart_required=True,
    ),
    ConfigField(
        "OFFICEFLOW_ENABLE_DB", "启用日志数据库", "app",
        default="1", kind="bool", restart_required=True,
    ),
)

FIELD_BY_KEY: dict[str, ConfigField] = {f.key: f for f in CONFIG_FIELDS}
SECRET_KEYS = frozenset(f.key for f in CONFIG_FIELDS if f.secret)


# ---------- 模型服务商预设 ----------

PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "label": "DeepSeek（便宜，中文好）",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-flash", "deepseek-v4-pro"],
        "key_hint": "在 platform.deepseek.com → API keys 创建，形如 sk-…",
        "needs_key": True,
    },
    {
        "id": "qwen",
        "label": "通义千问（阿里云 DashScope）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max"],
        "key_hint": "在阿里云百炼控制台创建 API Key。",
        "needs_key": True,
    },
    {
        "id": "ollama",
        "label": "本地 Ollama（完全免费、离线）",
        "base_url": "http://localhost:11434/v1",
        "models": ["qwen2.5:7b", "qwen2.5:14b", "llama3.1:8b"],
        "key_hint": "本地模型不校验 Key，随便填一个占位值（如 ollama）即可。",
        "needs_key": False,
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "key_hint": "在 platform.openai.com 创建，形如 sk-…",
        "needs_key": True,
    },
    {
        "id": "custom",
        "label": "自定义（任意 OpenAI 兼容端点）",
        "base_url": "",
        "models": [],
        "key_hint": "填写你自己的兼容端点 Base URL（一般以 /v1 结尾）。",
        "needs_key": True,
    },
]


def guess_preset(base_url: str) -> str:
    """按 Base URL 反推预设 id（用于打开面板时高亮当前服务商）。"""
    url = (base_url or "").strip().lower()
    if not url:
        return "custom"
    for preset in PROVIDER_PRESETS:
        if preset["base_url"] and url.rstrip("/") == preset["base_url"].lower().rstrip("/"):
            return preset["id"]
    return "custom"


# ---------- 读取 ----------


def _parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 为 dict（与 frontend/env_loader.py 行为一致）。"""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def mask_value(value: str) -> str:
    """密钥掩码：保留前 6 位便于用户辨认是哪把 Key，其余打点。"""
    if not value:
        return ""
    if len(value) <= 10:
        return MASK
    return f"{value[:6]}{'•' * 6}{value[-4:]}"


def read_raw() -> dict[str, str]:
    """读 .env 的原始值（含密钥明文，仅供后端内部使用）。"""
    if not ENV_PATH.is_file():
        return {}
    try:
        return _parse_env_text(ENV_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning("读取 %s 失败：%s", ENV_PATH, exc)
        return {}


def read_config() -> dict[str, Any]:
    """返回给前端的配置视图：密钥只给掩码。

    取值优先级：进程环境变量 > .env > 字段默认值
    （与运行时行为一致，因为 `load_env` 使用 override=False）。
    """
    raw = read_raw()
    items: list[dict[str, Any]] = []
    for f in CONFIG_FIELDS:
        env_value = os.environ.get(f.key)
        file_value = raw.get(f.key)
        value = env_value if env_value is not None else (file_value or "")
        configured = bool((env_value or file_value or "").strip())
        items.append(
            {
                "key": f.key,
                "label": f.label,
                "group": f.group,
                "kind": f.kind,
                "help": f.help,
                "secret": f.secret,
                "restart_required": f.restart_required,
                "choices": list(f.choices),
                "default": f.default,
                "configured": configured,
                # 密钥：只回掩码；非密钥：回原值
                "value": mask_value(value) if f.secret else value,
            }
        )
    return {
        "env_file": str(ENV_PATH),
        "env_file_exists": ENV_PATH.is_file(),
        "preset": guess_preset(raw.get("OFFICEFLOW_MODEL_BASE_URL", os.environ.get("OFFICEFLOW_MODEL_BASE_URL", ""))),
        "items": items,
        "model_ready": bool(
            (os.environ.get("OFFICEFLOW_MODEL_API_KEY") or raw.get("OFFICEFLOW_MODEL_API_KEY") or "").strip()
        ),
    }


# ---------- 写入 ----------

_ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def write_config(
    updates: dict[str, str] | None = None,
    clear: Sequence[str] | None = None,
) -> dict[str, Any]:
    """把配置写入 .env 并同步到当前进程环境变量。

    Args:
        updates: {键: 值}；**空字符串会被忽略**（视为"用户没改"）。
        clear:   需要清空的键（写成 `KEY=`）。

    Returns:
        {"applied": {...}, "skipped": [...], "restart_required": [...], "env_file": "..."}

    Raises:
        ValueError: 出现未登记的键（白名单拦截）。
    """
    updates = dict(updates or {})
    clear = list(clear or [])

    unknown = [k for k in list(updates) + clear if k not in FIELD_BY_KEY]
    if unknown:
        raise ValueError(f"不允许修改这些配置项：{'、'.join(unknown)}")

    # 空值 = 未修改；显式 clear 才清空
    effective: dict[str, str] = {
        k: v for k, v in updates.items() if isinstance(v, str) and v.strip() != ""
    }
    for key in clear:
        effective[key] = ""

    if not effective:
        return {
            "applied": {},
            "skipped": sorted(updates),
            "restart_required": [],
            "env_file": str(ENV_PATH),
        }

    _validate_values(effective)

    lines: list[str] = []
    if ENV_PATH.is_file():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    seen: set[str] = set()
    for i, line in enumerate(lines):
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        if key in effective:
            lines[i] = f"{key}={effective[key]}"
            seen.add(key)

    missing = [k for k in effective if k not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# ---------- 由 Web 配置面板写入 ----------")
        for key in missing:
            lines.append(f"{key}={effective[key]}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 同步到当前进程：让"每次调用时读环境变量"的模块立即生效
    for key, value in effective.items():
        os.environ[key] = value

    # 模型配置变化 → 重置 LLM 单例，无需重启
    if any(k.startswith("OFFICEFLOW_MODEL_") for k in effective):
        reset_llm_client()
        # LangExtract 读的是同一批变量，也一并刷新缓存（无缓存则无副作用）
        logger.info("模型配置已更新，LLM 客户端已重置")

    restart_required = sorted(
        k for k in effective if FIELD_BY_KEY[k].restart_required
    )
    logger.info("配置已保存：%s", "、".join(sorted(effective)))
    return {
        "applied": {k: (mask_value(v) if k in SECRET_KEYS else v) for k, v in effective.items()},
        "skipped": sorted(k for k in updates if k not in effective),
        "restart_required": restart_required,
        "env_file": str(ENV_PATH),
    }


def _validate_values(values: dict[str, str]) -> None:
    """按字段类型做基础校验，避免写入明显非法的值。"""
    for key, value in values.items():
        f = FIELD_BY_KEY[key]
        if not value:
            continue
        if f.kind == "int":
            try:
                int(value)
            except ValueError as exc:
                raise ValueError(f"「{f.label}」必须是整数，收到：{value}") from exc
        elif f.kind == "bool":
            if value.strip().lower() not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
                raise ValueError(f"「{f.label}」必须是布尔值（0/1）")
        elif f.kind == "url":
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(
                    f"「{f.label}」必须是完整 URL（含 http:// 或 https://），收到：{value}"
                )


def reset_llm_client() -> None:
    """重置 LLM 单例，使新的模型配置立即生效（无需重启后端）。"""
    try:
        import app.llm as llm_module

        llm_module._llm = None
    except Exception as exc:  # noqa: BLE001 - 重置失败不应影响保存
        logger.warning("重置 LLM 客户端失败：%s", exc)


# ---------- 连通性测试 ----------


def _looks_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def test_model(
    base_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """用一次极小的对话请求验证模型配置是否可用。

    未传的字段回落到当前生效配置。返回结构固定，前端直接展示。
    """
    import httpx

    raw = read_raw()
    url = (base_url or os.environ.get("OFFICEFLOW_MODEL_BASE_URL")
           or raw.get("OFFICEFLOW_MODEL_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/")
    key = (api_key or "").strip() or os.environ.get("OFFICEFLOW_MODEL_API_KEY") or raw.get(
        "OFFICEFLOW_MODEL_API_KEY", ""
    )
    model = (model_name or os.environ.get("OFFICEFLOW_MODEL_NAME")
             or raw.get("OFFICEFLOW_MODEL_NAME") or "deepseek-chat")

    if not url:
        return {"ok": False, "detail": "Base URL 为空", "hint": "请填写模型服务地址"}

    local = _looks_local(url)
    if not key and not local:
        return {
            "ok": False,
            "detail": "未填写 API Key",
            "hint": "请先填写 API Key（本地 Ollama 可填占位值）",
        }

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {key or 'not-needed'}"}

    import time

    started = time.perf_counter()
    try:
        resp = httpx.post(
            f"{url}/chat/completions", json=payload, headers=headers, timeout=timeout
        )
    except httpx.ConnectError as exc:
        return {
            "ok": False,
            "detail": f"连不上 {url}",
            "hint": "检查地址是否正确；本地 Ollama 请确认已启动（ollama serve）",
            "error": str(exc),
        }
    except httpx.TimeoutException:
        return {"ok": False, "detail": f"请求超时（>{timeout:.0f}s）", "hint": "网络较慢或地址不可达"}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"请求失败：{type(exc).__name__}", "error": str(exc)}

    latency_ms = int((time.perf_counter() - started) * 1000)
    if resp.status_code == 200:
        return {
            "ok": True,
            "detail": f"连接成功（{latency_ms} ms）",
            "model": model,
            "base_url": url,
            "latency_ms": latency_ms,
        }

    # 把常见错误翻译成人话
    hint = ""
    if resp.status_code in (401, 403):
        hint = "API Key 无效或没有权限，请重新复制一次 Key"
    elif resp.status_code == 404:
        hint = f"模型名「{model}」在该服务商不存在，或 Base URL 少了 /v1"
    elif resp.status_code == 429:
        hint = "触发限流或余额不足，稍后重试或检查账户余额"
    snippet = resp.text[:200].replace("\n", " ")
    return {
        "ok": False,
        "detail": f"HTTP {resp.status_code}",
        "hint": hint,
        "error": snippet,
        "latency_ms": latency_ms,
    }


def public_view() -> dict[str, Any]:
    """给前端的完整配置数据：字段 + 预设 + 当前状态。"""
    view = read_config()
    view["presets"] = PROVIDER_PRESETS
    view["secret_keys"] = sorted(SECRET_KEYS)
    return view
