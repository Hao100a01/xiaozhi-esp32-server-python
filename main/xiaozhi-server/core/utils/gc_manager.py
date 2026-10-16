"""
Module quản lý GC toàn cục
Thực thi thu gom rác định kỳ, tránh vấn đề khoá GIL do kích hoạt GC quá thường xuyên
"""

import gc
import asyncio
import threading
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class GlobalGCManager:
    """Bộ quản lý thu gom rác toàn cục"""

    def __init__(self, interval_seconds=300):
        """
        Khởi tạo bộ quản lý GC

        Args:
            interval_seconds: Khoảng cách thực thi GC (giây), mặc định 300 giây (5 phút)
        """
        self.interval_seconds = interval_seconds
        self._task = None
        self._stop_event = asyncio.Event()
        self._lock = threading.Lock()

    async def start(self):
        """Khởi động task GC định kỳ"""
        if self._task is not None:
            logger.bind(tag=TAG).warning("Bộ quản lý GC đã đang chạy")
            return

        logger.bind(tag=TAG).info(f"Khởi động bộ quản lý GC toàn cục, khoảng cách {self.interval_seconds} giây")
        self._stop_event.clear()
        self._task = asyncio.create_task(self._gc_loop())

    async def stop(self):
        """Dừng task GC định kỳ"""
        if self._task is None:
            return

        logger.bind(tag=TAG).info("Dừng bộ quản lý GC toàn cục")
        self._stop_event.set()

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._task = None

    async def _gc_loop(self):
        """Task vòng lặp GC"""
        try:
            while not self._stop_event.is_set():
                # Chờ khoảng cách chỉ định
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.interval_seconds
                    )
                    # Nếu stop_event được đặt, thoát vòng lặp
                    break
                except asyncio.TimeoutError:
                    # Timeout nghĩa là đã đến lúc thực thi GC
                    pass

                # Thực thi GC
                await self._run_gc()

        except asyncio.CancelledError:
            logger.bind(tag=TAG).info("Task vòng lặp GC đã bị huỷ")
            raise
        except Exception as e:
            logger.bind(tag=TAG).error(f"Task vòng lặp GC bất thường: {e}")
        finally:
            logger.bind(tag=TAG).info("Task vòng lặp GC đã thoát")

    async def _run_gc(self):
        """Thực thi thu gom rác"""
        try:
            # Thực thi GC trong thread pool, tránh chặn event loop
            loop = asyncio.get_running_loop()

            def do_gc():
                with self._lock:
                    before = len(gc.get_objects())
                    collected = gc.collect()
                    after = len(gc.get_objects())
                    return before, collected, after

            before, collected, after = await loop.run_in_executor(None, do_gc)
            logger.bind(tag=TAG).debug(
                f"GC toàn cục thực thi xong - đối tượng đã thu gom: {collected}, "
                f"Số lượng đối tượng: {before} -> {after}"
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Lỗi khi thực thi GC: {e}")


# Singleton toàn cục
_gc_manager_instance = None


def get_gc_manager(interval_seconds=300):
    """
    Lấy instance bộ quản lý GC toàn cục (mẫu singleton)

    Args:
        interval_seconds: Khoảng cách thực thi GC (giây), mặc định 300 giây (5 phút)

    Returns:
        Instance GlobalGCManager
    """
    global _gc_manager_instance
    if _gc_manager_instance is None:
        _gc_manager_instance = GlobalGCManager(interval_seconds)
    return _gc_manager_instance
