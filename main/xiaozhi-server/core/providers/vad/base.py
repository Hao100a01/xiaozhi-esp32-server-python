from abc import ABC, abstractmethod
from typing import Optional


class VADProviderBase(ABC):
    @abstractmethod
    def is_vad(self, conn, data) -> bool:
        """Phát hiện hoạt động giọng nói trong dữ liệu âm thanh"""
        pass
