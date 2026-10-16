"""Định nghĩa lớp cơ sở bộ thực thi công cụ"""

from abc import ABC, abstractmethod
from typing import Dict, Any
from .tool_types import ToolDefinition
from plugins_func.register import ActionResponse


class ToolExecutor(ABC):
    """Lớp cơ sở trừu tượng của bộ thực thi công cụ"""

    @abstractmethod
    async def execute(
        self, conn, tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """Thực thi lệnh gọi công cụ"""
        pass

    @abstractmethod
    def get_tools(self) -> Dict[str, ToolDefinition]:
        """Lấy mọi công cụ do bộ thực thi này quản lý"""
        pass

    @abstractmethod
    def has_tool(self, tool_name: str) -> bool:
        """Kiểm tra có công cụ được chỉ định không"""
        pass
