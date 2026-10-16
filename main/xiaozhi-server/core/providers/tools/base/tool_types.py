"""Định nghĩa kiểu của hệ thống công cụ"""

from enum import Enum

from dataclasses import dataclass
from typing import Any, Dict, Optional
from plugins_func.register import Action


class ToolType(Enum):
    """Enum kiểu công cụ"""

    SERVER_PLUGIN = "server_plugin"  # Plugin phía máy chủ
    SERVER_MCP = "server_mcp"  # MCP phía máy chủ
    DEVICE_IOT = "device_iot"  # IoT phía thiết bị
    DEVICE_MCP = "device_mcp"  # MCP phía thiết bị
    MCP_ENDPOINT = "mcp_endpoint"  # Điểm truy cập MCP


@dataclass
class ToolDefinition:
    """Định nghĩa công cụ"""

    name: str  # Tên công cụ
    description: Dict[str, Any]  # Mô tả công cụ (định dạng gọi hàm OpenAI)
    tool_type: ToolType  # Kiểu công cụ
    parameters: Optional[Dict[str, Any]] = None  # Tham số bổ sung
