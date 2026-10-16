import re
import os
import json
import copy
import wave
import socket
import sys
import asyncio
import threading
import requests
import subprocess
import numpy as np
import opuslib_next
from io import BytesIO
from contextlib import contextmanager
from core.utils import p3
from pydub import AudioSegment
from typing import Callable, Any

TAG = __name__


# Bỏ qua placeholder PATH ffmpeg/ffprobe bản rút gọn do IDE (Trae CN v.v.) inject: trong lúc gọi thì đưa
# bin cùng thư mục với trình thông dịch Python lên tiền tố PATH, pydub.utils.which() sẽ ưu tiên tìm thấy
# bản đầy đủ đi kèm conda env, gọi xong khôi phục PATH gốc, không làm bẩn toàn cục sau import.
# Tắt khi biến môi trường XIAOZHI_PYDUB_OVERRIDE_FFMPEG=0.
#
# Đếm tham chiếu + lock: khi đa thread ra vào khối with đồng thời, chỉ ghi lại PATH gốc ở lần vào đầu tiên
# rồi viết lại, chỉ khôi phục khi ref_count về 0. Như vậy finally không bị lệch do lưu/khôi phục nhiều lần
# gây tiền tố bin tồn tại vĩnh viễn, cũng tránh hợp đồng try/finally bị các thread ghi đè lẫn nhau.
_ffmpeg_path_lock = threading.Lock()
_ffmpeg_ref_count = 0
_ffmpeg_original_path: str | None = None


@contextmanager
def _ffmpeg_path_override():
    if os.environ.get("XIAOZHI_PYDUB_OVERRIDE_FFMPEG", "1") != "1":
        yield
        return
    bin_dir = os.path.dirname(sys.executable)
    ffprobe = os.path.join(bin_dir, "ffprobe")
    ffmpeg = os.path.join(bin_dir, "ffmpeg")
    if not (os.path.isfile(ffprobe) or os.path.isfile(ffmpeg)):
        yield
        return

    global _ffmpeg_ref_count, _ffmpeg_original_path
    with _ffmpeg_path_lock:
        if _ffmpeg_ref_count == 0:
            _ffmpeg_original_path = os.environ.get("PATH", "")
            os.environ["PATH"] = bin_dir + os.pathsep + _ffmpeg_original_path
        _ffmpeg_ref_count += 1
    try:
        yield
    finally:
        with _ffmpeg_path_lock:
            _ffmpeg_ref_count -= 1
            if _ffmpeg_ref_count == 0 and _ffmpeg_original_path is not None:
                os.environ["PATH"] = _ffmpeg_original_path
                _ffmpeg_original_path = None


# Uỷ thác cho util.ffmpeg_path_override, sau khi import util bên gọi chỉ cần một dòng là dùng được
ffmpeg_path_override = _ffmpeg_path_override


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Connect to Google's DNS servers
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception as e:
        return "127.0.0.1"


def is_private_ip(ip_addr):
    """
    Check if an IP address is a private IP address (compatible with IPv4 and IPv6).

    @param {string} ip_addr - The IP address to check.
    @return {bool} True if the IP address is private, False otherwise.
    """
    try:
        # Validate IPv4 or IPv6 address format
        if not re.match(
            r"^(\d{1,3}\.){3}\d{1,3}$|^([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}$", ip_addr
        ):
            return False  # Invalid IP address format

        # IPv4 private address ranges
        if "." in ip_addr:  # IPv4 address
            ip_parts = list(map(int, ip_addr.split(".")))
            if ip_parts[0] == 10:
                return True  # 10.0.0.0/8 range
            elif ip_parts[0] == 172 and 16 <= ip_parts[1] <= 31:
                return True  # 172.16.0.0/12 range
            elif ip_parts[0] == 192 and ip_parts[1] == 168:
                return True  # 192.168.0.0/16 range
            elif ip_addr == "127.0.0.1":
                return True  # Loopback address
            elif ip_parts[0] == 169 and ip_parts[1] == 254:
                return True  # Link-local address 169.254.0.0/16
            else:
                return False  # Not a private IPv4 address
        else:  # IPv6 address
            ip_addr = ip_addr.lower()
            if ip_addr.startswith("fc00:") or ip_addr.startswith("fd00:"):
                return True  # Unique Local Addresses (FC00::/7)
            elif ip_addr == "::1":
                return True  # Loopback address
            elif ip_addr.startswith("fe80:"):
                return True  # Link-local unicast addresses (FE80::/10)
            else:
                return False  # Not a private IPv6 address

    except (ValueError, IndexError):
        return False  # IP address format error or insufficient segments


def get_ip_info(ip_addr, logger):
    try:
        # Import bộ quản lý cache toàn cục
        from core.utils.cache.manager import cache_manager, CacheType

        # Lấy từ cache trước
        cached_ip_info = cache_manager.get(CacheType.IP_INFO, ip_addr)
        if cached_ip_info is not None:
            return cached_ip_info

        # Cache miss, gọi API
        if is_private_ip(ip_addr):
            ip_addr = ""
        url = f"https://whois.pconline.com.cn/ipJson.jsp?json=true&ip={ip_addr}"
        resp = requests.get(url).json()
        ip_info = {"city": resp.get("city")}

        # Lưu vào cache
        cache_manager.set(CacheType.IP_INFO, ip_addr, ip_info)
        return ip_info
    except Exception as e:
        logger.bind(tag=TAG).error(f"Error getting client ip info: {e}")
        return {}


def write_json_file(file_path, data):
    """Ghi dữ liệu ra file JSON"""
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)


def remove_punctuation_and_length(text):
    # Phạm vi Unicode của ký tự full-width và half-width
    full_width_punctuations = (
        "！＂＃＄％＆＇（）＊＋，－。／：；＜＝＞？＠［＼］＾＿｀｛｜｝～"
    )
    half_width_punctuations = r'!"#$%&\'()*+,-./:;<=>?@[\]^_`{|}~'
    space = " "  # Dấu cách half-width
    full_width_space = "　"  # Dấu cách full-width

    # Loại bỏ ký tự full-width/half-width và dấu cách
    result = "".join(
        [
            char
            for char in text
            if char not in full_width_punctuations
            and char not in half_width_punctuations
            and char not in space
            and char not in full_width_space
        ]
    )

    if result == "Yeah":
        return 0, ""
    return len(result), result


def check_model_key(modelType, modelKey):
    if "你" in modelKey:
        return f"Lỗi cấu hình: API key của {modelType} chưa được đặt, giá trị hiện tại: {modelKey}"
    return None


def parse_string_to_list(value, separator=";"):
    """
    Chuyển giá trị đầu vào thành danh sách
    Args:
        value: Giá trị đầu vào, có thể là None, chuỗi hoặc danh sách
        separator: Dấu phân tách, mặc định là dấu chấm phẩy
    Returns:
        list: Danh sách sau xử lý
    """
    if value is None or value == "":
        return []
    elif isinstance(value, str):
        return [item.strip() for item in value.split(separator) if item.strip()]
    elif isinstance(value, list):
        return value
    return []


def check_ffmpeg_installed() -> bool:
    """
    Kiểm tra ffmpeg đã được cài đúng và có thể thực thi trong môi trường hiện tại chưa.

    Returns:
        bool: Nếu ffmpeg dùng bình thường thì trả về True; ngược lại ném ngoại lệ ValueError.

    Raises:
        ValueError: Khi phát hiện ffmpeg chưa cài hoặc thiếu phụ thuộc thì ném thông tin nhắc chi tiết.
    """
    try:
        # Thử thực thi lệnh ffmpeg
        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,  # Mã thoát khác 0 sẽ kích hoạt CalledProcessError
        )

        output = (result.stdout + result.stderr).lower()
        if "ffmpeg version" in output:
            return True

        # Nếu không phát hiện thông tin phiên bản thì cũng coi là trường hợp bất thường
        raise ValueError("Không phát hiện output phiên bản ffmpeg hợp lệ.")

    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        # Trích xuất output lỗi
        stderr_output = ""
        if isinstance(e, subprocess.CalledProcessError):
            stderr_output = (e.stderr or "").strip()
        else:
            stderr_output = str(e).strip()

        # Dựng thông báo lỗi cơ sở
        error_msg = [
            "❌ Phát hiện ffmpeg không chạy bình thường.\n",
            "Bạn nên:",
            "1. Xác nhận đã kích hoạt đúng môi trường conda;",
            "2. Xem tài liệu cài đặt dự án để biết cách cài ffmpeg trong môi trường conda.\n",
        ]

        # 🎯 Gợi ý thêm cho từng thông báo lỗi cụ thể
        if "libiconv.so.2" in stderr_output:
            error_msg.append("⚠️ Phát hiện thiếu thư viện phụ thuộc: libiconv.so.2")
            error_msg.append("Cách khắc phục: thực thi trong môi trường conda hiện tại:")
            error_msg.append("   conda install -c conda-forge libiconv\n")
        elif (
            "no such file or directory" in stderr_output
            and "ffmpeg" in stderr_output.lower()
        ):
            error_msg.append("⚠️ Hệ thống không tìm thấy file thực thi ffmpeg.")
            error_msg.append("Cách khắc phục: thực thi trong môi trường conda hiện tại:")
            error_msg.append("   conda install -c conda-forge ffmpeg\n")
        else:
            error_msg.append("Chi tiết lỗi:")
            error_msg.append(stderr_output or "Lỗi không xác định.")

        # Ném thông tin ngoại lệ chi tiết
        raise ValueError("\n".join(error_msg)) from e


def extract_json_from_string(input_string):
    """Trích xuất phần JSON trong chuỗi"""
    pattern = r"(\{.*\})"
    match = re.search(pattern, input_string, re.DOTALL)  # Thêm re.DOTALL
    if match:
        return match.group(1)  # Trả về chuỗi JSON đã trích xuất
    return None


def audio_to_data_stream(
    audio_file_path, is_opus=True, callback: Callable[[Any], Any] = None, sample_rate=16000, opus_encoder=None
) -> None:
    with _ffmpeg_path_override():
        _audio_to_data_stream_inner(
            audio_file_path, is_opus, callback, sample_rate, opus_encoder
        )


def _audio_to_data_stream_inner(
    audio_file_path, is_opus, callback, sample_rate, opus_encoder
):
    file_type = os.path.splitext(audio_file_path)[1]
    if file_type:
        file_type = file_type.lstrip(".")
    # -nostdin: tránh ffmpeg chặn chờ stdin
    audio = AudioSegment.from_file(
        audio_file_path, format=file_type, parameters=["-nostdin"]
    )
    audio = audio.set_channels(1).set_frame_rate(sample_rate).set_sample_width(2)
    pcm_to_data_stream(audio.raw_data, is_opus, callback, sample_rate, opus_encoder)


async def audio_to_data(
    audio_file_path: str, is_opus: bool = True, use_cache: bool = True
) -> list[bytes]:
    """
    Chuyển file audio thành danh sách frame mã hoá Opus/PCM
    Args:
        audio_file_path: Đường dẫn file audio
        is_opus: Có mã hoá Opus không
        use_cache: Có dùng cache không
    """
    from core.utils.cache.manager import cache_manager
    from core.utils.cache.config import CacheType

    # Sinh cache key, gồm đường dẫn file và loại mã hoá
    cache_key = f"{audio_file_path}:{is_opus}"

    # Thử lấy kết quả từ cache
    if use_cache:
        cached_result = cache_manager.get(CacheType.AUDIO_DATA, cache_key)
        if cached_result is not None:
            return cached_result

    def _sync_audio_to_data():
        # Lấy phần mở rộng file
        file_type = os.path.splitext(audio_file_path)[1]
        if file_type:
            file_type = file_type.lstrip(".")
        # Đọc file audio, tham số -nostdin: không đọc dữ liệu từ stdin, nếu không FFmpeg sẽ chặn
        with _ffmpeg_path_override():
            audio = AudioSegment.from_file(
                audio_file_path, format=file_type, parameters=["-nostdin"]
            )

        # Chuyển thành mono/tần số lấy mẫu 16kHz/mã hoá little-endian 16-bit (đảm bảo khớp với bộ mã hoá)
        audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)

        # Lấy dữ liệu PCM gốc (little-endian 16-bit)
        raw_data = audio.raw_data

        # Khởi tạo bộ mã hoá Opus
        encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)

        # Tham số mã hoá
        frame_duration = 60  # 60ms per frame
        frame_size = int(16000 * frame_duration / 1000)  # 960 samples/frame

        datas = []
        # Xử lý mọi dữ liệu audio theo frame (kể cả frame cuối có thể bù 0)
        for i in range(0, len(raw_data), frame_size * 2):  # 16bit=2bytes/sample
            # Lấy dữ liệu nhị phân của frame hiện tại
            chunk = raw_data[i : i + frame_size * 2]

            # Nếu frame cuối không đủ thì bù 0
            if len(chunk) < frame_size * 2:
                chunk += b"\x00" * (frame_size * 2 - len(chunk))

            if is_opus:
                # Chuyển thành mảng numpy để xử lý
                np_frame = np.frombuffer(chunk, dtype=np.int16)
                # Mã hoá dữ liệu Opus
                frame_data = encoder.encode(np_frame.tobytes(), frame_size)
            else:
                frame_data = chunk if isinstance(chunk, bytes) else bytes(chunk)

            datas.append(frame_data)

        return datas

    loop = asyncio.get_running_loop()
    # Thực thi thao tác xử lý audio đồng bộ trong thread riêng
    result = await loop.run_in_executor(None, _sync_audio_to_data)

    # Lưu kết quả vào cache, dùng TTL định nghĩa trong cấu hình (10 phút)
    if use_cache:
        cache_manager.set(CacheType.AUDIO_DATA, cache_key, result)

    return result


def audio_bytes_to_data_stream(
    audio_bytes, file_type, is_opus, callback: Callable[[Any], Any], sample_rate=16000, opus_encoder=None
) -> None:
    """
    Dùng trực tiếp dữ liệu nhị phân audio chuyển thành dữ liệu opus/pcm, hỗ trợ wav, mp3, p3
    """
    if file_type == "p3":
        # Giải mã trực tiếp bằng p3
        return p3.decode_opus_from_bytes_stream(audio_bytes, callback)
    else:
        # Định dạng khác dùng pydub
        with _ffmpeg_path_override():
            audio = AudioSegment.from_file(
                BytesIO(audio_bytes), format=file_type, parameters=["-nostdin"]
            )
        audio = audio.set_channels(1).set_frame_rate(sample_rate).set_sample_width(2)
        raw_data = audio.raw_data
        pcm_to_data_stream(raw_data, is_opus, callback, sample_rate, opus_encoder)


def pcm_to_data_stream(raw_data, is_opus=True, callback: Callable[[Any], Any] = None, sample_rate=16000, opus_encoder=None):
    """
    Mã hoá streaming dữ liệu PCM thành Opus hoặc xuất trực tiếp PCM

    Args:
        raw_data: Dữ liệu PCM gốc
        is_opus: Có mã hoá thành Opus không
        callback: Hàm callback
        sample_rate: Tần số lấy mẫu
        opus_encoder: Đối tượng OpusEncoderUtils (nên cung cấp để giữ trạng thái bộ mã hoá liên tục)
    """
    using_temp_encoder = False
    if is_opus and opus_encoder is None:
        encoder = opuslib_next.Encoder(sample_rate, 1, opuslib_next.APPLICATION_AUDIO)
        using_temp_encoder = True

    # Tham số mã hoá
    frame_duration = 60  # 60ms per frame
    frame_size = int(sample_rate * frame_duration / 1000)  # samples/frame

    # Xử lý mọi dữ liệu audio theo frame (kể cả frame cuối có thể bù 0)
    for i in range(0, len(raw_data), frame_size * 2):  # 16bit=2bytes/sample
        # Lấy dữ liệu nhị phân của frame hiện tại
        chunk = raw_data[i : i + frame_size * 2]

        # Nếu frame cuối không đủ thì bù 0
        if len(chunk) < frame_size * 2:
            chunk += b"\x00" * (frame_size * 2 - len(chunk))

        if is_opus:
            if using_temp_encoder:
                # Dùng bộ mã hoá tạm (chỉ cho cảnh audio độc lập)
                np_frame = np.frombuffer(chunk, dtype=np.int16)
                frame_data = encoder.encode(np_frame.tobytes(), frame_size)
                callback(frame_data)
            else:
                # Dùng bộ mã hoá ngoài (cảnh streaming TTS, giữ trạng thái liên tục)
                is_last = (i + frame_size * 2 >= len(raw_data))
                opus_encoder.encode_pcm_to_opus_stream(chunk, end_of_stream=is_last, callback=callback)
        else:
            # Chế độ PCM, xuất trực tiếp
            frame_data = chunk if isinstance(chunk, bytes) else bytes(chunk)
            callback(frame_data)


def opus_datas_to_wav_bytes(opus_datas, sample_rate=16000, channels=1):
    """
    Giải mã danh sách frame opus thành luồng byte wav
    """
    decoder = opuslib_next.Decoder(sample_rate, channels)
    try:
        pcm_datas = []

        frame_duration = 60  # ms
        frame_size = int(sample_rate * frame_duration / 1000)  # 960

        for opus_frame in opus_datas:
            # Giải mã thành PCM (trả về bytes, 2 byte/điểm mẫu)
            pcm = decoder.decode(opus_frame, frame_size)
            pcm_datas.append(pcm)

        pcm_bytes = b"".join(pcm_datas)

        # Ghi luồng byte wav
        wav_buffer = BytesIO()
        with wave.open(wav_buffer, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)  # 16bit
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        return wav_buffer.getvalue()
    finally:
        if decoder is not None:
            try:
                del decoder
            except Exception:
                pass


def check_vad_update(before_config, new_config):
    if (
        new_config.get("selected_module") is None
        or new_config["selected_module"].get("VAD") is None
    ):
        return False
    update_vad = False
    current_vad_module = before_config["selected_module"]["VAD"]
    new_vad_module = new_config["selected_module"]["VAD"]
    current_vad_type = (
        current_vad_module
        if "type" not in before_config["VAD"][current_vad_module]
        else before_config["VAD"][current_vad_module]["type"]
    )
    new_vad_type = (
        new_vad_module
        if "type" not in new_config["VAD"][new_vad_module]
        else new_config["VAD"][new_vad_module]["type"]
    )
    update_vad = current_vad_type != new_vad_type
    return update_vad


def check_asr_update(before_config, new_config):
    if (
        new_config.get("selected_module") is None
        or new_config["selected_module"].get("ASR") is None
    ):
        return False
    update_asr = False
    current_asr_module = before_config["selected_module"]["ASR"]
    new_asr_module = new_config["selected_module"]["ASR"]

    # Nếu tên module khác nhau thì cần cập nhật
    if current_asr_module != new_asr_module:
        return True

    # Nếu tên module giống nhau thì so sánh tiếp loại
    current_asr_type = (
        current_asr_module
        if "type" not in before_config["ASR"][current_asr_module]
        else before_config["ASR"][current_asr_module]["type"]
    )
    new_asr_type = (
        new_asr_module
        if "type" not in new_config["ASR"][new_asr_module]
        else new_config["ASR"][new_asr_module]["type"]
    )
    update_asr = current_asr_type != new_asr_type
    return update_asr


def filter_sensitive_info(config: dict) -> dict:
    """
    Lọc thông tin nhạy cảm trong cấu hình
    Args:
        config: Dict cấu hình gốc
    Returns:
        Dict cấu hình sau lọc
    """
    sensitive_keys = [
        "api_key",
        "personal_access_token",
        "access_token",
        "token",
        "secret",
        "access_key_secret",
        "secret_key",
    ]

    def _filter_dict(d: dict) -> dict:
        filtered = {}
        for k, v in d.items():
            if any(sensitive in k.lower() for sensitive in sensitive_keys):
                filtered[k] = "***"
            elif isinstance(v, dict):
                filtered[k] = _filter_dict(v)
            elif isinstance(v, list):
                filtered[k] = [_filter_dict(i) if isinstance(i, dict) else i for i in v]
            elif isinstance(v, str):
                try:
                    json_data = json.loads(v)
                    if isinstance(json_data, dict):
                        filtered[k] = json.dumps(
                            _filter_dict(json_data), ensure_ascii=False
                        )
                    else:
                        filtered[k] = v
                except (json.JSONDecodeError, TypeError):
                    filtered[k] = v
            else:
                filtered[k] = v
        return filtered

    return _filter_dict(copy.deepcopy(config))


def get_vision_url(config: dict) -> str:
    """Lấy vision URL

    Args:
        config: Dict cấu hình

    Returns:
        str: vision URL
    """
    server_config = config["server"]
    vision_explain = server_config.get("vision_explain", "")
    if "你的" in vision_explain:
        local_ip = get_local_ip()
        port = int(server_config.get("http_port", 8003))
        vision_explain = f"http://{local_ip}:{port}/mcp/vision/explain"
    return vision_explain


def is_valid_image_file(file_data: bytes) -> bool:
    """
    Kiểm tra dữ liệu file có phải định dạng hình ảnh hợp lệ

    Args:
        file_data: Dữ liệu nhị phân của file

    Returns:
        bool: Trả về True nếu là định dạng hình ảnh hợp lệ, ngược lại False
    """
    # Magic number (header file) của các định dạng hình ảnh phổ biến
    image_signatures = {
        b"\xff\xd8\xff": "JPEG",
        b"\x89PNG\r\n\x1a\n": "PNG",
        b"GIF87a": "GIF",
        b"GIF89a": "GIF",
        b"BM": "BMP",
        b"II*\x00": "TIFF",
        b"MM\x00*": "TIFF",
        b"RIFF": "WEBP",
    }

    # Kiểm tra header file có khớp định dạng hình ảnh đã biết nào
    for signature in image_signatures:
        if file_data.startswith(signature):
            return True

    return False


def sanitize_tool_name(name: str) -> str:
    """Sanitize tool names for OpenAI compatibility."""
    # Hỗ trợ tiếng Trung, chữ cái tiếng Anh, chữ số, gạch dưới và gạch nối
    return re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]", "_", name)


def validate_mcp_endpoint(mcp_endpoint: str) -> bool:
    """
    Kiểm tra định dạng điểm truy cập MCP

    Args:
        mcp_endpoint: Chuỗi điểm truy cập MCP

    Returns:
        bool: Có hợp lệ không
    """
    # 1. Kiểm tra có bắt đầu bằng ws không
    if not mcp_endpoint.startswith("ws"):
        return False

    # 2. Kiểm tra có chứa chữ key, call không
    if "key" in mcp_endpoint.lower() or "call" in mcp_endpoint.lower():
        return False

    # 3. Kiểm tra có chứa chuỗi /mcp/ không
    if "/mcp/" not in mcp_endpoint:
        return False

    return True

def get_system_error_response(config: dict) -> str:
    """Lấy câu trả lời khi hệ thống lỗi

    Args:
        config: Dict cấu hình

    Returns:
        str: Câu trả lời khi hệ thống lỗi
    """
    return config.get("system_error_response", "Chủ nhân ơi, Tiểu Trí giờ hơi bận, mình thử lại sau nhé.")