"""Skill 基类：每个 Skill 定义输入/输出 schema、工具列表与验收样本引用。"""

from abc import ABC, abstractmethod
from typing import Any

from app.schemas import AgentState


class Skill(ABC):
    """任务型 Skill 契约。"""

    name: str
    display_name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    tools: list[str]

    @abstractmethod
    def run(self, state: AgentState) -> AgentState:
        """执行 Skill，返回更新后的状态（含工具日志追加）。"""
