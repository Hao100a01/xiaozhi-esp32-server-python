import time
import asyncio
from collections import deque
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class AudioRateController:
    """
    Bộ điều khiển tốc độ audio - điều khiển chính xác việc gửi audio theo frame 60ms
    Giải quyết vấn đề sai số thời gian tích lũy khi đồng thời cao
    """

    def __init__(self, frame_duration=60, send_delay=0):
        """
        Args:
            frame_duration: Thời lượng một frame audio (mili giây), mặc định 60ms
            send_delay: Khoảng cách gửi tuỳ chỉnh (mili giây).
                        0 = gửi theo nhịp frame_duration;
                        >0 = gửi theo nhịp send_delay (thread chính không trễ khi vào hàng đợi, không đệm trước)
        """
        # Khoảng cách điều tiết luồng (mili giây): dùng giá trị cấu hình khi send_delay > 0, ngược lại dùng frame_duration
        self.interval_ms = send_delay if send_delay > 0 else frame_duration
        self.queue = deque()
        self.play_position = 0  # Vị trí phát ảo (mili giây)
        self.start_timestamp = None  # Timestamp bắt đầu (chỉ đọc, không sửa)
        self.pending_send_task = None
        self.logger = logger
        self.queue_empty_event = asyncio.Event()  # Sự kiện hàng đợi đã rỗng
        self.queue_empty_event.set()  # Trạng thái rỗng ban đầu
        self.queue_has_data_event = asyncio.Event()  # Sự kiện hàng đợi có dữ liệu
        self._last_queue_empty_time = 0  # Thời điểm hàng đợi rỗng lần trước (giây)

    def reset(self):
        """Đặt lại trạng thái bộ điều khiển"""
        if self.pending_send_task and not self.pending_send_task.done():
            self.pending_send_task.cancel()
            # Sau khi huỷ task, task sẽ được dọn dẹp ở vòng lặp sự kiện tiếp theo, không cần chờ chặn

        self.queue.clear()
        self.play_position = 0
        self.start_timestamp = None  # Được đặt bởi gói audio đầu tiên
        self._last_queue_empty_time = 0  # Đặt lại thời gian
        # Xử lý sự kiện liên quan
        self.queue_empty_event.set()
        self.queue_has_data_event.clear()

    def add_audio(self, opus_packet):
        """Thêm gói audio vào hàng đợi"""
        # Nếu hàng đợi trước đó rỗng, cần điều chỉnh timestamp để giữ thời gian phát liên tục
        # Như vậy trong lúc chờ gọi tool, audio mới thêm vào sẽ không bị phát sớm
        # Nếu khoảng cách rất ngắn (<1 frame) thì là truyền streaming bình thường, không cần đặt lại
        if len(self.queue) == 0 and self.play_position > 0:
            elapsed_since_empty = (time.monotonic() - self._last_queue_empty_time) * 1000
            # Chỉ khi khoảng cách vượt 1 frame mới coi là "tạm dừng rồi tiếp tục" thật sự
            if elapsed_since_empty >= self.interval_ms:
                self.start_timestamp = time.monotonic() - (self.play_position / 1000)
                self.logger.bind(tag=TAG).debug(
                    f"Hàng đợi phục hồi từ trạng thái rỗng, đặt lại timestamp, vị trí phát hiện tại: {self.play_position}ms, khoảng cách: {elapsed_since_empty:.0f}ms"
                )

        self.queue.append(("audio", opus_packet))
        # Xử lý sự kiện liên quan
        self.queue_empty_event.clear()
        self.queue_has_data_event.set()

    def add_message(self, message_callback):
        """
        Thêm message vào hàng đợi (gửi ngay, không chiếm thời gian phát)

        Args:
            message_callback: Hàm callback gửi message async def()
        """
        if len(self.queue) == 0 and self.play_position > 0:
            elapsed_since_empty = (time.monotonic() - self._last_queue_empty_time) * 1000
            if elapsed_since_empty >= self.interval_ms:
                self.start_timestamp = time.monotonic() - (self.play_position / 1000)
                self.logger.bind(tag=TAG).debug(
                    f"Hàng đợi phục hồi từ trạng thái rỗng, đặt lại timestamp, vị trí phát hiện tại: {self.play_position}ms, khoảng cách: {elapsed_since_empty:.0f}ms"
                )

        self.queue.append(("message", message_callback))
        # Xử lý sự kiện liên quan
        self.queue_empty_event.clear()
        self.queue_has_data_event.set()

    def _get_elapsed_ms(self):
        """Lấy thời gian đã trôi qua (mili giây)"""
        if self.start_timestamp is None:
            return 0
        return (time.monotonic() - self.start_timestamp) * 1000

    async def check_queue(self, send_audio_callback):
        """
        Kiểm tra hàng đợi và gửi audio/message đúng giờ

        Args:
            send_audio_callback: Hàm callback gửi audio async def(opus_packet)
        """
        while self.queue:
            item = self.queue[0]
            item_type = item[0]

            if item_type == "message":
                # Loại message: gửi ngay, không chiếm thời gian phát
                _, message_callback = item
                self.queue.popleft()
                try:
                    await message_callback()
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"Gửi message thất bại: {e}")
                    raise

            elif item_type == "audio":
                if self.start_timestamp is None:
                    self.start_timestamp = time.monotonic()

                _, opus_packet = item

                # Lặp chờ đến khi tới giờ
                while True:
                    # Tính chênh lệch thời gian
                    elapsed_ms = self._get_elapsed_ms()
                    output_ms = self.play_position

                    if elapsed_ms < output_ms:
                        # Chưa tới giờ gửi, tính thời gian chờ
                        wait_ms = output_ms - elapsed_ms

                        # Sau khi chờ thì kiểm tra tiếp (cho phép bị ngắt)
                        try:
                            await asyncio.sleep(wait_ms / 1000)
                        except asyncio.CancelledError:
                            self.logger.bind(tag=TAG).debug("Task gửi audio đã bị huỷ")
                            raise
                        # Sau khi chờ xong kiểm tra lại thời gian (vòng lặp quay về while True)
                    else:
                        # Đã tới giờ, thoát vòng lặp chờ
                        break

                # Đã tới giờ, lấy ra khỏi hàng đợi và gửi
                self.queue.popleft()
                self.play_position += self.interval_ms
                try:
                    await send_audio_callback(opus_packet)
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"Gửi audio thất bại: {e}")
                    raise

        # Xóa sự kiện sau khi xử lý xong hàng đợi
        self.queue_empty_event.set()
        self.queue_has_data_event.clear()
        self._last_queue_empty_time = time.monotonic()  # Ghi lại thời điểm hàng đợi rỗng

    def start_sending(self, send_audio_callback):
        """
        Khởi động task gửi bất đồng bộ

        Args:
            send_audio_callback: Hàm callback gửi audio

        Returns:
            asyncio.Task: Task gửi
        """

        async def _send_loop():
            try:
                while True:
                    # Chờ sự kiện dữ liệu hàng đợi, không polling chờ tốn CPU
                    await self.queue_has_data_event.wait()

                    await self.check_queue(send_audio_callback)
            except asyncio.CancelledError:
                self.logger.bind(tag=TAG).debug("Vòng lặp gửi audio đã dừng")
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"Vòng lặp gửi audio bất thường: {e}")

        self.pending_send_task = asyncio.create_task(_send_loop())
        return self.pending_send_task

    def stop_sending(self):
        """Dừng task gửi"""
        if self.pending_send_task and not self.pending_send_task.done():
            self.pending_send_task.cancel()
            self.logger.bind(tag=TAG).debug("Đã huỷ task gửi audio")
