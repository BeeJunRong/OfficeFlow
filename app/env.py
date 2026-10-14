"""项目根目录 .env 加载器。

**必须在读取任何配置之前调用**，因为 `app/storage.py`、`frontend/app.py`、
`app/llm.py` 都在模块级或首次实例化时读取环境变量。

用法（放在模块最顶部，先于其它业务 import）：

    from app.env import load_env

    load_env()

    from app.graph import run_workflow   # 这些模块会读环境变量

特性：
- python-dotenv 未安装时静默跳过（不阻断启动），并回落到操作系统环境变量。
- 已存在的进程环境变量优先（`override=False`），便于临时 `$env:` 覆盖 .env。
- 无需手工 `cd`：按本文件位置定位项目根目录。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("officeflow.env")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"

_loaded = False


def load_env(path: Path | None = None, *, force: bool = False) -> bool:
    """加载项目根目录的 .env。

    Args:
        path: 自定义 .env 路径；默认项目根目录下的 .env。
        force: 为 True 时即使已加载过也重新读取。

    Returns:
        是否成功加载到文件。文件不存在或缺少 python-dotenv 时返回 False，
        且**不抛异常**——保证"没有 .env 也能正常启动"。
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
        from dotenv import load_dotenv
    except ImportError:
        # 不阻断启动：优先保证应用可用，配置缺失由调用方降级处理
        logger.warning(
            "检测到 %s 但未安装 python-dotenv，该文件被忽略。"
            "安装：pip install python-dotenv",
            env_file,
        )
        _loaded = True
        return False

    # override=False：已存在的进程环境变量优先，便于临时覆盖
    load_dotenv(env_file, override=False)
    os.environ.setdefault("OFFICEFLOW_ENV_FILE", str(env_file))
    _loaded = True
    logger.info("已加载环境配置：%s", env_file)
    return True
