import json
import uuid
import asyncio
import websockets
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__()
        self.interface_type = InterfaceType.STREAM
        self.config = config
        self.text = ""
        self.asr_ws = None
        self.forward_task = None
        self.is_processing = False
        self.server_ready = False  # Trạng thái sẵn sàng của máy chủ
        self.task_id = None  # ID tác vụ hiện tại

        # Cấu hình Alibaba Bailian
        self.api_key = config.get("api_key")
        self.model = config.get("model", "paraformer-realtime-v2")
        self.sample_rate = config.get("sample_rate", 16000)
        self.format = config.get("format", "pcm")

        # Tham số tuỳ chọn
        self.vocabulary_id = config.get("vocabulary_id")
        self.disfluency_removal_enabled = config.get("disfluency_removal_enabled", False)
        self.language_hints = config.get("language_hints")
        self.semantic_punctuation_enabled = config.get("semantic_punctuation_enabled", False)
        max_sentence_silence = config.get("max_sentence_silence")
        self.max_sentence_silence = int(max_sentence_silence) if max_sentence_silence else 200
        self.multi_threshold_mode_enabled = config.get("multi_threshold_mode_enabled", False)
        self.punctuation_prediction_enabled = config.get("punctuation_prediction_enabled", True)
        self.inverse_text_normalization_enabled = config.get("inverse_text_normalization_enabled", True)

        # WebSocket URL
        self.ws_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

        self.output_dir = config.get("output_dir", "./audio_output")
        self.delete_audio_file = delete_audio_file

    async def open_audio_channels(self, conn):
        await super().open_audio_channels(conn)

    async def receive_audio(self, conn, pcm_frame, audio_have_voice):
        # Gọi phương thức lớp cha trước để xử lý logic cơ bản
        await super().receive_audio(conn, pcm_frame, audio_have_voice)

        # Chỉ thiết lập kết nối khi có âm thanh và chưa có kết nối
        if audio_have_voice and not self.is_processing and not self.asr_ws:
            try:
                await self._start_recognition(conn)
            except Exception as e:
                logger.bind(tag=TAG).error(f"Bắt đầu nhận dạng thất bại: {str(e)}")
                await self._cleanup()
                return

        # Gửi dữ liệu âm thanh
        if self.asr_ws and self.is_processing and self.server_ready:
            try:
                await self.asr_ws.send(pcm_frame)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"Gửi âm thanh thất bại: {str(e)}")
                await self._cleanup()

    async def _start_recognition(self, conn: "ConnectionHandler"):
        """Bắt đầu phiên nhận dạng"""
        try:
            # Nếu là chế độ thủ công, đặt thời gian chờ thành giá trị tối đa
            if conn.client_listen_mode == "manual":
                self.max_sentence_silence = 6000

            self.is_processing = True
            self.task_id = uuid.uuid4().hex

            # Thiết lập kết nối WebSocket
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }

            logger.bind(tag=TAG).debug(f"Đang kết nối dịch vụ ASR Alibaba Bailian, task_id: {self.task_id}")

            self.asr_ws = await websockets.connect(
                self.ws_url,
                additional_headers=headers,
                max_size=1000000000,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=5,
            )

            logger.bind(tag=TAG).debug("Thiết lập kết nối WebSocket thành công")

            self.server_ready = False
            self.forward_task = asyncio.create_task(self._forward_results(conn))

            # Gửi lệnh run-task
            run_task_msg = self._build_run_task_message()
            await self.asr_ws.send(json.dumps(run_task_msg, ensure_ascii=False))
            logger.bind(tag=TAG).debug("Đã gửi lệnh run-task, đang chờ máy chủ sẵn sàng...")

        except Exception as e:
            logger.bind(tag=TAG).error(f"Thiết lập kết nối ASR thất bại: {str(e)}")
            if self.asr_ws:
                await self.asr_ws.close()
                self.asr_ws = None
            self.is_processing = False
            raise

    def _build_run_task_message(self) -> dict:
        """Tạo lệnh run-task"""
        message = {
            "header": {
                "action": "run-task",
                "task_id": self.task_id,
                "streaming": "duplex"
            },
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
                "parameters": {
                    "format": self.format,
                    "sample_rate": self.sample_rate,
                    "disfluency_removal_enabled": self.disfluency_removal_enabled,
                    "semantic_punctuation_enabled": self.semantic_punctuation_enabled,
                    "max_sentence_silence": self.max_sentence_silence,
                    "multi_threshold_mode_enabled": self.multi_threshold_mode_enabled,
                    "punctuation_prediction_enabled": self.punctuation_prediction_enabled,
                    "inverse_text_normalization_enabled": self.inverse_text_normalization_enabled,
                },
                "input": {}
            }
        }

        # Chỉ thêm tham số vocabulary_id khi tên mô hình kết thúc bằng v2
        if self.model.lower().endswith("v2"):
            message["payload"]["parameters"]["vocabulary_id"] = self.vocabulary_id

        if self.language_hints:
            message["payload"]["parameters"]["language_hints"] = self.language_hints

        return message

    async def _forward_results(self, conn: "ConnectionHandler"):
        """Chuyển tiếp kết quả nhận dạng"""
        try:
            while not conn.stop_event.is_set():
                # Lấy dữ liệu âm thanh của kết nối hiện tại
                audio_data = conn.asr_audio
                try:
                    response = await asyncio.wait_for(self.asr_ws.recv(), timeout=1.0)
                    result = json.loads(response)

                    header = result.get("header", {})
                    payload = result.get("payload", {})
                    event = header.get("event", "")

                    # Xử lý sự kiện task-started
                    if event == "task-started":
                        self.server_ready = True
                        logger.bind(tag=TAG).debug("Máy chủ đã sẵn sàng, bắt đầu gửi âm thanh đã đệm...")

                        # Gửi âm thanh đã đệm
                        if conn.asr_audio:
                            for cached_pcm in conn.asr_audio[-10:]:
                                try:
                                    await self.asr_ws.send(cached_pcm)
                                except Exception as e:
                                    logger.bind(tag=TAG).warning(f"Gửi âm thanh đã đệm thất bại: {e}")
                                    break
                        continue

                    # Xử lý sự kiện result-generated
                    elif event == "result-generated":
                        output = payload.get("output", {})
                        sentence = output.get("sentence", {})

                        text = sentence.get("text", "")
                        sentence_end = sentence.get("sentence_end", False)
                        end_time = sentence.get("end_time")

                        # Xác định có phải kết quả cuối cùng không (sentence_end là True và end_time không phải null)
                        is_final = sentence_end and end_time is not None

                        if is_final:
                            logger.bind(tag=TAG).info(f"Nhận dạng được văn bản: {text}")

                            # Tích luỹ kết quả nhận dạng ở chế độ thủ công
                            if conn.client_listen_mode == "manual":
                                if self.text:
                                    self.text += text
                                else:
                                    self.text = text

                                # Ở chế độ thủ công, chỉ kích hoạt xử lý sau khi nhận tín hiệu stop
                                if conn.client_voice_stop:
                                    logger.bind(tag=TAG).debug("Đã nhận kết quả nhận dạng cuối cùng, kích hoạt xử lý")
                                    await self.handle_voice_stop(conn, audio_data)
                                    break
                            else:
                                # Ở chế độ tự động ghi đè trực tiếp
                                self.text = text
                                await self.handle_voice_stop(conn, audio_data)
                                break

                    # Xử lý sự kiện task-finished
                    elif event == "task-finished":
                        logger.bind(tag=TAG).debug("Tác vụ đã hoàn thành")
                        break

                    # Xử lý sự kiện task-failed
                    elif event == "task-failed":
                        error_code = header.get("error_code", "UNKNOWN")
                        error_message = header.get("error_message", "Lỗi không xác định")
                        logger.bind(tag=TAG).error(f"Tác vụ thất bại: {error_code} - {error_message}")
                        break

                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    logger.bind(tag=TAG).info("Kết nối dịch vụ ASR đã đóng")
                    self.is_processing = False
                    break
                except Exception as e:
                    logger.bind(tag=TAG).error(f"Xử lý kết quả thất bại: {str(e)}")
                    break

        except Exception as e:
            logger.bind(tag=TAG).error(f"Chuyển tiếp kết quả thất bại: {str(e)}")
        finally:
            # Dọn dẹp bộ đệm âm thanh của kết nối
            await self._cleanup()
            conn.reset_audio_states()

    async def _send_stop_request(self):
        """Gửi yêu cầu dừng (dùng để dừng ghi âm ở chế độ thủ công)"""
        if self.asr_ws:
            try:
                # Dừng gửi âm thanh trước
                self.is_processing = False

                logger.bind(tag=TAG).debug("Đã nhận yêu cầu dừng, gửi lệnh finish-task")
                await self._send_finish_task()
            except Exception as e:
                logger.bind(tag=TAG).error(f"Gửi yêu cầu dừng thất bại: {e}")

    async def _send_finish_task(self):
        """Gửi lệnh finish-task"""
        if self.asr_ws and self.task_id:
            try:
                finish_msg = {
                    "header": {
                        "action": "finish-task",
                        "task_id": self.task_id,
                        "streaming": "duplex"
                    },
                    "payload": {
                        "input": {}
                    }
                }
                await self.asr_ws.send(json.dumps(finish_msg, ensure_ascii=False))
                logger.bind(tag=TAG).debug("Đã gửi lệnh finish-task")
            except Exception as e:
                logger.bind(tag=TAG).error(f"Gửi lệnh finish-task thất bại: {e}")

    async def _cleanup(self):
        """Dọn dẹp tài nguyên"""
        logger.bind(tag=TAG).debug(f"Bắt đầu dọn dẹp phiên ASR | Trạng thái hiện tại: processing={self.is_processing}, server_ready={self.server_ready}")

        # Đặt lại trạng thái
        self.is_processing = False
        self.server_ready = False
        logger.bind(tag=TAG).debug("Trạng thái ASR đã được đặt lại")

        # Đóng kết nối
        if self.asr_ws:
            try:
                # Gửi lệnh finish-task trước
                await self._send_finish_task()
                # Chờ một lúc để máy chủ xử lý
                await asyncio.sleep(0.1)

                logger.bind(tag=TAG).debug("Đang đóng kết nối WebSocket")
                await asyncio.wait_for(self.asr_ws.close(), timeout=2.0)
                logger.bind(tag=TAG).debug("Kết nối WebSocket đã đóng")
            except Exception as e:
                logger.bind(tag=TAG).error(f"Đóng kết nối WebSocket thất bại: {e}")
            finally:
                self.asr_ws = None

        # Dọn dẹp tham chiếu tác vụ
        self.forward_task = None
        self.task_id = None

        logger.bind(tag=TAG).debug("Dọn dẹp phiên ASR hoàn tất")

    async def speech_to_text(self, opus_data, session_id, artifacts=None):
        """Lấy kết quả nhận dạng"""
        result = self.text
        self.text = ""
        return result, None

    async def close(self):
        """Đóng tài nguyên"""
        await self._cleanup()