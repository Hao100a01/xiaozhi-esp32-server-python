"""
Định nghĩa chiến lược cache và cấu trúc dữ liệu
"""

import time
from enum import Enum
from typing import Any, Optional
from dataclasses import dataclass


class CacheStrategy(Enum):
    """Enum chiến lược cache"""

    TTL = "ttl"  # Hết hạn dựa trên thời gian
    LRU = "lru"  # Ít dùng gần đây nhất
    FIXED_SIZE = "fixed_size"  # Kích thước cố định
    TTL_LRU = "ttl_lru"  # Chiến lược hỗn hợp TTL + LRU


@dataclass
class CacheEntry:
    """Cấu trúc dữ liệu mục cache"""

    value: Any
    timestamp: float
    ttl: Optional[float] = None  # Thời gian sống (giây)
    access_count: int = 0
    last_access: float = None

    def __post_init__(self):
        if self.last_access is None:
            self.last_access = self.timestamp

    def is_expired(self) -> bool:
        """Kiểm tra đã hết hạn chưa"""
        if self.ttl is None:
            return False
        return time.time() - self.timestamp > self.ttl

    def touch(self):
        """Cập nhật thời gian truy cập và bộ đếm"""
        self.last_access = time.time()
        self.access_count += 1
