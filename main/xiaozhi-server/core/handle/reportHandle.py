"""
Chức năng báo cáo TTS đã được tích hợp vào lớp ConnectionHandler.

Chức năng báo cáo gồm:
1. Mỗi đối tượng kết nối có hàng đợi báo cáo và thread xử lý riêng
2. Vòng đời của thread báo cáo gắn với đối tượng kết nối
3. Dùng phương thức ConnectionHandler.enqueue_tts_report để báo cáo

Triển khai cụ thể xem code liên quan trong core/connection.py.
"""

import time
import json
import opuslib_next
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from config.manage_api_client import report as manage_report

TAG = __name__


async def report(conn: "ConnectionHandler", chat_type, text, audio_data, report_time):
    """Thực hiện thao tác báo cáo lịch sử chat

    Args:
        conn: Đối tượng kết nối
        chat_type: Loại báo cáo, 1 là người dùng (ASR/PCM), 2 là Trợ lý AI (TTS/Opus), 3 là gọi tool
        text: Text tổng hợp
        audio_data: Dữ liệu audio (chat_type=1 là định dạng PCM, chat_type=2 là định dạng Opus)
        report_time: Thời gian báo cáo
    """
    try:
        if audio_data:

            if chat_type == 1:
                wav_data = pcm_to_wav(conn, audio_data)
            elif chat_type == 2:
                wav_data = opus_to_wav(conn, audio_data)
            else:
                wav_data = None
        else:
            wav_data = None
        # Thực hiện báo cáo bất đồng bộ
        await manage_report(
            mac_address=conn.device_id,
            session_id=conn.session_id,
            chat_type=chat_type,
            content=text,
            audio=wav_data,
            report_time=report_time,
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"Báo cáo lịch sử chat thất bại: {e}")


def pcm_to_wav(conn: "ConnectionHandler", pcm_data):
    """Chuyển dữ liệu PCM thành luồng byte định dạng WAV

    Args:
        conn: Đối tượng kết nối
        pcm_data: Dữ liệu audio PCM (có thể là list hoặc bytes)

    Returns:
        bytes: Dữ liệu audio định dạng WAV
    """
    try:
        # Xử lý dữ liệu PCM có thể là list hoặc bytes
        if isinstance(pcm_data, list):
            pcm_data_bytes = b"".join(pcm_data)
        else:
            pcm_data_bytes = pcm_data

        if not pcm_data_bytes:
            raise ValueError("Không có dữ liệu PCM hợp lệ")

        # Tạo header file WAV
        num_samples = len(pcm_data_bytes) // 2  # 16-bit samples

        # Header file WAV
        wav_header = bytearray()
        wav_header.extend(b"RIFF")  # ChunkID
        wav_header.extend((36 + len(pcm_data_bytes)).to_bytes(4, "little"))  # ChunkSize
        wav_header.extend(b"WAVE")  # Format
        wav_header.extend(b"fmt ")  # Subchunk1ID
        wav_header.extend((16).to_bytes(4, "little"))  # Subchunk1Size
        wav_header.extend((1).to_bytes(2, "little"))  # AudioFormat (PCM)
        wav_header.extend((1).to_bytes(2, "little"))  # NumChannels
        wav_header.extend((16000).to_bytes(4, "little"))  # SampleRate
        wav_header.extend((32000).to_bytes(4, "little"))  # ByteRate
        wav_header.extend((2).to_bytes(2, "little"))  # BlockAlign
        wav_header.extend((16).to_bytes(2, "little"))  # BitsPerSample
        wav_header.extend(b"data")  # Subchunk2ID
        wav_header.extend(len(pcm_data_bytes).to_bytes(4, "little"))  # Subchunk2Size

        # Trả về dữ liệu WAV đầy đủ
        return bytes(wav_header) + pcm_data_bytes
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"Chuyển PCM sang WAV thất bại: {e}", exc_info=True)
        raise


def opus_to_wav(conn: "ConnectionHandler", opus_data):
    """Chuyển dữ liệu Opus thành luồng byte định dạng WAV

    Args:
        conn: Đối tượng kết nối
        opus_data: Dữ liệu audio Opus (có thể là list hoặc bytes)

    Returns:
        bytes: Dữ liệu audio định dạng WAV
    """
    decoder = None
    try:
        decoder = opuslib_next.Decoder(16000, 1)
        pcm_data = []

        if isinstance(opus_data, list):
            for opus_packet in opus_data:
                try:
                    pcm_frame = decoder.decode(opus_packet, 960)
                    pcm_data.append(pcm_frame)
                except opuslib_next.OpusError as e:
                    conn.logger.bind(tag=TAG).error(f"Lỗi giải mã Opus: {e}", exc_info=True)
        elif isinstance(opus_data, bytes):
            pcm_frame = decoder.decode(opus_data, 960)
            pcm_data.append(pcm_frame)

        if not pcm_data:
            raise ValueError("Không có dữ liệu audio hợp lệ")

        pcm_data_bytes = b"".join(pcm_data)

        wav_header = bytearray()
        wav_header.extend(b"RIFF")
        wav_header.extend((36 + len(pcm_data_bytes)).to_bytes(4, "little"))
        wav_header.extend(b"WAVE")
        wav_header.extend(b"fmt ")
        wav_header.extend((16).to_bytes(4, "little"))
        wav_header.extend((1).to_bytes(2, "little"))
        wav_header.extend((1).to_bytes(2, "little"))
        wav_header.extend((16000).to_bytes(4, "little"))
        wav_header.extend((32000).to_bytes(4, "little"))
        wav_header.extend((2).to_bytes(2, "little"))
        wav_header.extend((16).to_bytes(2, "little"))
        wav_header.extend(b"data")
        wav_header.extend(len(pcm_data_bytes).to_bytes(4, "little"))

        return bytes(wav_header) + pcm_data_bytes
    finally:
        if decoder is not None:
            try:
                del decoder
            except Exception as e:
                conn.logger.bind(tag=TAG).debug(f"Lỗi khi giải phóng tài nguyên decoder: {e}")


def enqueue_tts_report(conn: "ConnectionHandler", text, opus_data):
    """Thêm dữ liệu TTS vào hàng đợi báo cáo

    Args:
        conn: Đối tượng kết nối
        text: Text tổng hợp
        opus_data: Dữ liệu audio opus
    """
    if not conn.read_config_from_api or conn.need_bind or not conn.report_tts_enable:
        return
    if conn.chat_history_conf == 0:
        return
    try:
        # Dùng hàng đợi của đối tượng kết nối, truyền text và dữ liệu nhị phân thay vì đường dẫn file
        if conn.chat_history_conf == 2:
            conn.report_queue.put((2, text, opus_data, int(time.time() * 1000)))
            conn.logger.bind(tag=TAG).debug(
                f"Dữ liệu TTS đã vào hàng đợi báo cáo: {conn.device_id}, kích thước audio: {len(opus_data)} "
            )
        else:
            conn.report_queue.put((2, text, None, int(time.time() * 1000)))
            conn.logger.bind(tag=TAG).debug(
                f"Dữ liệu TTS đã vào hàng đợi báo cáo: {conn.device_id}, không báo cáo audio"
            )
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"Thêm vào hàng đợi báo cáo TTS thất bại: {text}, {e}")


def enqueue_tool_report(conn: "ConnectionHandler", tool_name: str, tool_input: dict, tool_result: str = None, report_tool_call: bool = True):
    """Thêm dữ liệu gọi tool vào hàng đợi báo cáo

    Args:
        conn: Đối tượng kết nối
        tool_name: Tên tool
        tool_input: Param đầu vào của tool
        tool_result: Kết quả thực thi tool (tuỳ chọn)
        report_tool_call: Có báo cáo bản thân lệnh gọi tool, mặc định True; chỉ đặt False khi chỉ báo cáo kết quả
    """
    if not conn.read_config_from_api or conn.need_bind:
        return
    if conn.chat_history_conf == 0:
        return

    try:
        timestamp = int(time.time() * 1000)

        # Dựng nội dung gọi tool
        if report_tool_call:
            tool_text = json.dumps(
                [
                    {
                        "type": "tool",
                        "text": f"{tool_name}({json.dumps(tool_input, ensure_ascii=False)})",
                    }
                ]
            )
            conn.report_queue.put((3, tool_text, None, timestamp))

        # Dựng nội dung kết quả tool
        if tool_result:
            result_display = f'{{"result":"{str(tool_result)}"}}'
            result_content = json.dumps([{"type": "tool_result", "text": result_display}], ensure_ascii=False)
            conn.report_queue.put((3, result_content, None, timestamp + 1))
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"Thêm vào hàng đợi báo cáo tool thất bại: {e}")


def enqueue_asr_report(conn: "ConnectionHandler", text, opus_data):
    """Thêm dữ liệu ASR vào hàng đợi báo cáo

    Args:
        conn: Đối tượng kết nối
        text: Text tổng hợp
        opus_data: Dữ liệu audio opus
    """
    if not conn.read_config_from_api or conn.need_bind or not conn.report_asr_enable:
        return
    if conn.chat_history_conf == 0:
        return
    try:
        # Dùng hàng đợi của đối tượng kết nối, truyền text và dữ liệu nhị phân thay vì đường dẫn file
        if conn.chat_history_conf == 2:
            conn.report_queue.put((1, text, opus_data, int(time.time() * 1000)))
            conn.logger.bind(tag=TAG).debug(
                f"Dữ liệu ASR đã vào hàng đợi báo cáo: {conn.device_id}, kích thước audio: {len(opus_data)} "
            )
        else:
            conn.report_queue.put((1, text, None, int(time.time() * 1000)))
            conn.logger.bind(tag=TAG).debug(
                f"Dữ liệu ASR đã vào hàng đợi báo cáo: {conn.device_id}, không báo cáo audio"
            )
    except Exception as e:
        conn.logger.bind(tag=TAG).debug(f"Thêm vào hàng đợi báo cáo ASR thất bại: {text}, {e}")
