"""
Quản lý cấu hình cache
"""

from enum import Enum
from typing import Dict, Any, Optional
from dataclasses import dataclass
from .strategies import CacheStrategy


class CacheType(Enum):
    """Enum loại cache"""

    LOCATION = "location"
    WEATHER = "weather"
    LUNAR = "lunar"
    INTENT = "intent"
    IP_INFO = "ip_info"
    CONFIG = "config"
    DEVICE_PROMPT = "device_prompt"
    VOICEPRINT_HEALTH = "voiceprint_health"  # Kiểm tra sức khoẻ nhận diện voiceprint
    AUDIO_DATA = "audio_data"  # Cache dữ liệu audio


@dataclass
class CacheConfig:
    """Lớp cấu hình cache"""

    strategy: CacheStrategy = CacheStrategy.TTL
    ttl: Optional[float] = 300  # Mặc định 5 phút
    max_size: Optional[int] = 1000  # Mặc định tối đa 1000 mục
    cleanup_interval: float = 60  # Khoảng thời gian dọn dẹp (giây)

    @classmethod
    def for_type(cls, cache_type: CacheType) -> "CacheConfig":
        """Trả về cấu hình preset theo loại cache"""
        configs = {
            CacheType.LOCATION: cls(
                strategy=CacheStrategy.TTL, ttl=None, max_size=1000  # Hết hiệu lực thủ công
            ),
            CacheType.IP_INFO: cls(
                strategy=CacheStrategy.TTL, ttl=86400, max_size=1000  # 24 giờ
            ),
            CacheType.WEATHER: cls(
                strategy=CacheStrategy.TTL, ttl=28800, max_size=1000  # 8 giờ
            ),
            CacheType.LUNAR: cls(
                strategy=CacheStrategy.TTL, ttl=2592000, max_size=365  # Hết hạn sau 30 ngày
            ),
            CacheType.INTENT: cls(
                strategy=CacheStrategy.TTL_LRU, ttl=600, max_size=1000  # 10 phút
            ),
            CacheType.CONFIG: cls(
                strategy=CacheStrategy.FIXED_SIZE, ttl=None, max_size=20  # Hết hiệu lực thủ công
            ),
            CacheType.DEVICE_PROMPT: cls(
                strategy=CacheStrategy.TTL, ttl=None, max_size=1000  # Hết hiệu lực thủ công
            ),
            CacheType.VOICEPRINT_HEALTH: cls(
                strategy=CacheStrategy.TTL, ttl=600, max_size=100  # Hết hạn sau 10 phút
            ),
            CacheType.AUDIO_DATA: cls(
                strategy=CacheStrategy.TTL, ttl=600, max_size=100  # Hết hạn sau 10 phút
            ),
        }
        return configs.get(cache_type, cls())
