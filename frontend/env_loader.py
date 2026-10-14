"""前端专用的 .env 加载器（**刻意不依赖 app 包**）。

为什么不复用 `app/env.py`：
    本文件所在目录下的 `app.py` 就是 Streamlit 的入口脚本，它自身会被 Python
    注册为顶层模块 `app`。因此 `frontend/app.py` 内**任何** `from app.xxx import`
    都会解析到脚本自己 —— 轻则报 "No module named 'app.env'; 'app' is not a
    package"，若中间删掉 sys.modules 缓存项触发重新查找，则会重新执行脚本自身，
    导致 `RecursionError: maximum recursion depth exceeded`。

    结论：前端入口脚本不得导入 `app` 包。此处用纯标准库自行解析 .env，
    与 `app/env.py` 行为保持一致（已有的进程环境变量优先，不覆盖）。

用法（放在读取任何配置之前）：

    from env_loader import load_env
    load_env()
    BACKEND = os.getenv("OFFICEFLOW_BACKEND", "http://localhost:8000")
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("officeflow.frontend.env")

# frontend/env_loader.py -> parents[1] = 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"

_loaded = False


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """解析单行 KEY=VALUE；跳过空行、注释与非法行。"""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    # 去掉成对的引号（.env 常见写法 KEY="value"）
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def load_env(path: Path | None = None, *, force: bool = False) -> bool:
    """加载项目根目录的 .env（不覆盖已存在的进程环境变量）。

    Returns:
        是否成功读取到文件。文件不存在时返回 False，且不抛异常。
    """
    global _loaded
    if _loaded and not force:
        return True

    env_file = path or ENV_PATH
    if not env_file.is_file():
        logger.debug("未找到 %s，使用操作系统环境变量", env_file)
        _loaded = True
        return False

    try:
        content = env_file.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("读取 %s 失败，忽略该文件：%s", env_file, exc)
        _loaded = True
        return False

    count = 0
    for raw in content.splitlines():
        parsed = _parse_env_line(raw)
        if parsed is None:
            continue
        key, value = parsed
        # 已有进程环境变量优先（便于临时 $env: 覆盖）
        if key not in os.environ:
            os.environ[key] = value
            count += 1

    _loaded = True
    logger.info("已从 %s 载入 %d 项配置", env_file, count)
    return True
