import asyncio
import time
import json
import uuid
import aiohttp
import websockets
import hmac
import base64
import hashlib
import asyncio
from urllib.parse import urlparse, urlencode
from tabulate import tabulate
from config.settings import load_config
from core.utils.alibl_endpoint import (
    build_ws_connect_options as build_alibl_ws_connect_options,
    resolve_ws_url as resolve_alibl_ws_url,
)

description = "Test độ trễ từ đầu tiên của tổng hợp giọng nói TTS streaming"
class StreamTTSPerformanceTester:
    def __init__(self, config):
        self.config = config
        self.test_texts = [
            "Xin chào, đây là một câu nói."
        ]
        self.results = []
    
    async def test_aliyun_tts(self, text=None, test_count=5):
        """Test độ trễ từ đầu tiên của Alibaba Cloud streaming TTS (test nhiều lần lấy trung bình)"""
        text = text or self.test_texts[0]
        latencies = []
        
        for i in range(test_count):
            try:
                tts_config = self.config["TTS"]["AliyunStreamTTS"]
                appkey = tts_config["appkey"]
                token = tts_config["token"]
                voice = tts_config["voice"]
                host = tts_config["host"]
                ws_url = f"wss://{host}/ws/v1"

                # Thống nhất điểm bấm giờ: bắt đầu bấm giờ trước khi thiết lập kết nối
                start_time = time.time()
                async with websockets.connect(ws_url, extra_headers={"X-NLS-Token": token}) as ws:
                    task_id = str(uuid.uuid4())
                    message_id = str(uuid.uuid4())

                    start_request = {
                        "header": {
                            "message_id": message_id,
                            "task_id": task_id,
                            "namespace": "FlowingSpeechSynthesizer",
                            "name": "StartSynthesis",
                            "appkey": appkey,
                        },
                        "payload": {
                            "voice": voice,
                            "format": "pcm",
                            "sample_rate": 16000,
                            "volume": 50,
                            "speech_rate": 0,
                            "pitch_rate": 0,
                            "enable_subtitle": True,
                        }
                    }
                    await ws.send(json.dumps(start_request))

                    start_response = json.loads(await ws.recv())
                    if start_response["header"]["name"] != "SynthesisStarted":
                        raise Exception("Khởi động tổng hợp thất bại")

                    run_request = {
                        "header": {
                            "message_id": str(uuid.uuid4()),
                            "task_id": task_id,
                            "namespace": "FlowingSpeechSynthesizer",
                            "name": "RunSynthesis",
                            "appkey": appkey,
                        },
                        "payload": {"text": text}
                    }
                    await ws.send(json.dumps(run_request))

                    while True:
                        response = await ws.recv()
                        if isinstance(response, bytes):
                            latency = time.time() - start_time
                            latencies.append(latency)
                            print(f"[Alibaba Cloud TTS] lần {i+1} độ trễ từ đầu: {latency:.3f}s")
                            break
                        elif isinstance(response, str):
                            data = json.loads(response)
                            if data["header"]["name"] == "TaskFailed":
                                raise Exception(f"Tổng hợp thất bại: {data['payload']['error_info']}")

            except Exception as e:
                print(f"[Alibaba Cloud TTS] lần {i+1} test thất bại: {str(e)}")
                latencies.append(None)
        
        return self._calculate_result("Alibaba Cloud TTS", latencies, test_count)

    async def test_alibl_tts(self, text=None, test_count=5):
        """Test độ trễ từ đầu tiên của Alibaba Cloud ModelStudio (CosyVoice) streaming TTS"""
        text = text or self.test_texts[0]
        latencies = []

        for i in range(test_count):
            try:
                tts_config = self.config["TTS"]["AliBLTTS"]
                api_key = tts_config["api_key"]
                model = tts_config.get("model", "cosyvoice-v2")
                voice = tts_config.get("voice", "longxiaochun_v2")
                format_type = tts_config.get("format", "pcm")
                sample_rate = int(tts_config.get("sample_rate", "24000"))
                connect_options = build_alibl_ws_connect_options(
                    float(self.config.get("tts_timeout", 15))
                )

                ws_url = resolve_alibl_ws_url(tts_config.get("ws_url"))
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "X-DashScope-DataInspection": "enable",
                }

                start_time = time.time()

                async with websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=10,
                    max_size=10 * 1024 * 1024,
                    **connect_options,
                ) as ws:
                    session_id = uuid.uuid4().hex

                    # 1. Gửi run-task (khởi động task)
                    run_task_message = {
                        "header": {
                            "action": "run-task",
                            "task_id": session_id,
                            "streaming": "duplex",
                        },
                        "payload": {
                            "task_group": "audio",
                            "task": "tts",
                            "function": "SpeechSynthesizer",
                            "model": model,
                            "parameters": {
                                "text_type": "PlainText",
                                "voice": voice,
                                "format": format_type,
                                "sample_rate": sample_rate,
                                "volume": 50,
                                "rate": 1.0,
                                "pitch": 1.0,
                            },
                            "input": {}
                        },
                    }
                    await ws.send(json.dumps(run_task_message))

                    # 2. Chờ event task-started (quan trọng! phải chờ cái này rồi mới gửi text)
                    task_started = False
                    while not task_started:
                        msg = await ws.recv()
                        if isinstance(msg, str):
                            data = json.loads(msg)
                            header = data.get("header", {})
                            event = header.get("event")
                            if event == "task-started":
                                task_started = True
                                print(f"[Alibaba Cloud ModelStudio TTS] lần {i+1} khởi động task thành công")
                            elif event == "task-failed":
                                raise Exception(f"Khởi động thất bại: {header.get('error_message', 'Lỗi không xác định')}")

                    # 3. Gửi continue-task (gửi text! đây là action đúng)
                    continue_task_message = {
                        "header": {
                            "action": "continue-task",  # Đổi lại thành continue-task
                            "task_id": session_id,
                            "streaming": "duplex",
                        },
                        "payload": {"input": {"text": text}},
                    }
                    await ws.send(json.dumps(continue_task_message))

                    # 4. Gửi finish-task (kết thúc task)
                    finish_task_message = {
                        "header": {
                            "action": "finish-task",
                            "task_id": session_id,
                            "streaming": "duplex",
                        },
                        "payload": {"input": {}}
                    }
                    await ws.send(json.dumps(finish_task_message))

                    # 5. Chờ khối dữ liệu audio đầu tiên
                    while True:
                        msg = await asyncio.wait_for(ws.recv(), timeout=15.0)
                        if isinstance(msg, (bytes, bytearray)) and len(msg) > 0:
                            latency = time.time() - start_time
                            print(f"[Alibaba Cloud ModelStudio TTS] lần {i+1} độ trễ từ đầu: {latency:.3f}s")
                            latencies.append(latency)
                            break
                        elif isinstance(msg, str):
                            data = json.loads(msg)
                            event = data.get("header", {}).get("event")
                            if event == "task-failed":
                                raise Exception(f"Tổng hợp thất bại: {data}")
                            elif event == "task-finished":
                                if not latencies or latencies[-1] is None:
                                    raise Exception("Task kết thúc nhưng chưa nhận được audio")

            except Exception as e:
                print(f"[Alibaba Cloud ModelStudio TTS] lần {i+1} thất bại: {str(e)}")
                latencies.append(None)

        return self._calculate_result("Alibaba Cloud ModelStudio TTS", latencies, test_count)

    async def test_doubao_tts(self, text=None, test_count=5):
        """Test độ trễ từ đầu tiên của Volcengine streaming TTS (test nhiều lần lấy trung bình)"""
        text = text or self.test_texts[0]
        latencies = []
        
        for i in range(test_count):
            try:
                tts_config = self.config["TTS"]["HuoshanDoubleStreamTTS"]
                ws_url = tts_config["ws_url"]
                app_id = tts_config["appid"]
                access_token = tts_config["access_token"]
                resource_id = tts_config["resource_id"]
                speaker = tts_config["speaker"]

                start_time = time.time()
                ws_header = {
                    "X-Api-App-Key": app_id,
                    "X-Api-Access-Key": access_token,
                    "X-Api-Resource-Id": resource_id,
                    "X-Api-Connect-Id": str(uuid.uuid4()),
                }
                async with websockets.connect(ws_url, additional_headers=ws_header, max_size=1000000000) as ws:
                    session_id = uuid.uuid4().hex

                    # Gửi yêu cầu khởi động session
                    header = bytes([
                        (0b0001 << 4) | 0b0001,  
                        0b0001 << 4 | 0b1011,     
                        0b0001 << 4 | 0b0000,
                    ])
                    optional = bytearray()
                    optional.extend((1).to_bytes(4, "big", signed=True))
                    session_id_bytes = session_id.encode()
                    optional.extend(len(session_id_bytes).to_bytes(4, "big", signed=True))
                    optional.extend(session_id_bytes)
                    payload = json.dumps({"speaker": speaker}).encode()
                    await ws.send(header + optional + len(payload).to_bytes(4, "big", signed=True) + payload)

                    # Gửi text
                    header = bytes([
                        (0b0001 << 4) | 0b0001,  
                        0b0001 << 4 | 0b1011,    
                        0b0001 << 4 | 0b0000,
                        0
                    ])
                    optional = bytearray()
                    optional.extend((200).to_bytes(4, "big", signed=True))
                    session_id_bytes = session_id.encode()
                    optional.extend(len(session_id_bytes).to_bytes(4, "big", signed=True))
                    optional.extend(session_id_bytes)
                    payload = json.dumps({"text": text, "speaker": speaker}).encode()
                    await ws.send(header + optional + len(payload).to_bytes(4, "big", signed=True) + payload)

                    first_chunk = await ws.recv()
                    latency = time.time() - start_time
                    latencies.append(latency)
                    print(f"[Volcengine TTS] lần {i+1} độ trễ từ đầu: {latency:.3f}s")

            except Exception as e:
                print(f"[Volcengine TTS] lần {i+1} test thất bại: {str(e)}")
                latencies.append(None)
        
        return self._calculate_result("Volcengine TTS", latencies, test_count)

    async def test_paddlespeech_tts(self, text=None, test_count=5):
        """Test độ trễ từ đầu tiên của PaddleSpeech streaming TTS (test nhiều lần lấy trung bình)"""
        text = text or self.test_texts[0]
        latencies = []
        
        for i in range(test_count):
            try:
                tts_config = self.config["TTS"]["PaddleSpeechTTS"]
                tts_url = tts_config["url"]
                spk_id = tts_config["spk_id"]
                speed = tts_config["speed"]
                volume = tts_config["volume"]

                start_time = time.time()
                async with websockets.connect(tts_url) as ws:
                    # Gửi yêu cầu bắt đầu
                    await ws.send(json.dumps({
                        "task": "tts",
                        "signal": "start"
                    }))
                    
                    start_response = json.loads(await ws.recv())
                    if start_response.get("status") != 0:
                        raise Exception("Kết nối thất bại")
                    
                    # Gửi dữ liệu text
                    await ws.send(json.dumps({
                        "text": text,
                        "spk_id": spk_id,
                        "speed": speed,
                        "volume": volume
                    }))
                    
                    # Nhận khối dữ liệu đầu tiên
                    first_chunk = await ws.recv()
                    latency = time.time() - start_time
                    latencies.append(latency)
                    print(f"[PaddleSpeech TTS] lần {i+1} độ trễ từ đầu: {latency:.3f}s")

                    # Gửi yêu cầu kết thúc
                    end_request = {
                        "task": "tts",
                        "signal": "end"
                    }
                    await ws.send(json.dumps(end_request))

                    # Đảm bảo kết nối đóng bình thường
                    try:
                        await ws.recv()
                    except websockets.exceptions.ConnectionClosedOK:
                        pass

            except Exception as e:
                print(f"[PaddleSpeech TTS] lần {i+1} test thất bại: {str(e)}")
                latencies.append(None)
        
        return self._calculate_result("PaddleSpeech TTS", latencies, test_count)
            
    async def test_indexstream_tts(self, text=None, test_count=5):
        """Test độ trễ từ đầu tiên của IndexStream streaming TTS (test nhiều lần lấy trung bình)"""
        text = text or self.test_texts[0]
        latencies = []
        
        for i in range(test_count):
            try:
                tts_config = self.config["TTS"]["IndexStreamTTS"]
                api_url = tts_config.get("api_url")
                voice = tts_config.get("voice")

                # Thống nhất điểm bấm giờ: bắt đầu bấm giờ trước khi thiết lập kết nối
                start_time = time.time()

                async with aiohttp.ClientSession() as session:
                    payload = {"text": text, "character": voice}
                    async with session.post(api_url, json=payload, timeout=10) as resp:
                        if resp.status != 200:
                            raise Exception(f"Yêu cầu thất bại: {resp.status}, {await resp.text()}")

                        async for chunk in resp.content.iter_any():
                            data = chunk[0] if isinstance(chunk, (list, tuple)) else chunk
                            if not data:
                                continue

                            latency = time.time() - start_time
                            latencies.append(latency)
                            print(f"[IndexStream TTS] lần {i+1} độ trễ từ đầu: {latency:.3f}s")
                            resp.close()
                            break
                        else:
                            latencies.append(None)

            except Exception as e:
                print(f"[IndexStream TTS] lần {i+1} test thất bại: {str(e)}")
                latencies.append(None)
        
        return self._calculate_result("IndexStream TTS", latencies, test_count)

    async def test_linkerai_tts(self, text=None, test_count=5):
        """Test độ trễ từ đầu tiên của Linkerai streaming TTS (test nhiều lần lấy trung bình)"""
        text = text or self.test_texts[0]
        latencies = []
        
        for i in range(test_count):
            try:
                tts_config = self.config["TTS"]["LinkeraiTTS"]
                api_url = tts_config["api_url"]
                access_token = tts_config["access_token"]
                voice = tts_config["voice"]

                # Thống nhất điểm bấm giờ: bắt đầu bấm giờ trước khi thiết lập kết nối
                start_time = time.time()
                async with aiohttp.ClientSession() as session:
                    params = {
                        "tts_text": text,
                        "spk_id": voice,
                        "frame_durition": 60,
                        "stream": "true",
                        "target_sr": 16000,
                        "audio_format": "pcm",
                        "instruct_text": "Vui lòng tạo một đoạn giọng nói tự nhiên, trôi chảy",
                    }
                    headers = {
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    }

                    async with session.get(api_url, params=params, headers=headers, timeout=10) as resp:
                        if resp.status != 200:
                            raise Exception(f"Yêu cầu thất bại: {resp.status}, {await resp.text()}")

                        # Nhận khối dữ liệu đầu tiên
                        async for _ in resp.content.iter_any():
                            latency = time.time() - start_time
                            latencies.append(latency)
                            print(f"[Linkerai TTS] lần {i+1} độ trễ từ đầu: {latency:.3f}s")
                            break
                        else:
                            latencies.append(None)

            except Exception as e:
                print(f"[Linkerai TTS] lần {i+1} test thất bại: {str(e)}")
                latencies.append(None)
        
        return self._calculate_result("Linkerai TTS", latencies, test_count)
    
    async def test_xunfei_tts(self, text=None, test_count=5):
        """Test độ trễ từ đầu tiên của iFlytek streaming TTS (test nhiều lần lấy trung bình)"""
        text = text or self.test_texts[0]
        latencies = []
        
        for i in range(test_count):
            try:
                # Sửa tên node cấu hình cho khớp với XunFeiTTS trong file cấu hình
                tts_config = self.config["TTS"]["XunFeiTTS"]
                app_id = tts_config["app_id"]
                api_key = tts_config["api_key"]
                api_secret = tts_config["api_secret"]
                api_url = tts_config.get("api_url", "wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6")
                voice = tts_config.get("voice", "x5_lingxiaoxuan_flow")
                # Tạo URL xác thực
                auth_url = self._create_xunfei_auth_url(api_key, api_secret, api_url)
                start_time = time.time()
                async with websockets.connect(
                    auth_url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=10,
                    max_size=1000000000
                ) as ws:
                    # Dựng request
                    request = self._build_xunfei_request(app_id, text, voice)
                    await ws.send(json.dumps(request))
                    # Chờ khối dữ liệu audio đầu tiên
                    first_audio_received = False
                    while not first_audio_received:
                        msg = await asyncio.wait_for(ws.recv(), timeout=10)
                        data = json.loads(msg)
                        header = data.get("header", {})
                        code = header.get("code")

                        if code != 0:
                            message = header.get("message", "Lỗi không xác định")
                            raise Exception(f"Tổng hợp thất bại: {code} - {message}")

                        payload = data.get("payload", {})
                        audio_payload = payload.get("audio", {})

                        if audio_payload:
                            status = audio_payload.get("status", 0)
                            audio_data = audio_payload.get("audio", "")
                            if status == 1 and audio_data:
                                # Đã nhận khối dữ liệu audio đầu tiên
                                latency = time.time() - start_time
                                latencies.append(latency)
                                print(f"[iFlytek TTS] lần {i+1} độ trễ từ đầu: {latency:.3f}s")
                                first_audio_received = True
                                break
            except Exception as e:
                print(f"[iFlytek TTS] lần {i+1} test thất bại: {str(e)}")
                latencies.append(None)
        
        return self._calculate_result("iFlytek TTS", latencies, test_count)
    
    def _create_xunfei_auth_url(self, api_key, api_secret, api_url):
        """Tạo URL xác thực WebSocket iFlytek"""
        parsed_url = urlparse(api_url)
        host = parsed_url.netloc
        path = parsed_url.path
        
        # Lấy giờ UTC, iFlytek yêu cầu dùng định dạng RFC1123
        now = time.gmtime()
        date = time.strftime('%a, %d %b %Y %H:%M:%S GMT', now)
        
        # Dựng chuỗi ký
        signature_origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
        
        # Tính chữ ký
        signature_sha = hmac.new(
            api_secret.encode('utf-8'),
            signature_origin.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()
        signature_sha_base64 = base64.b64encode(signature_sha).decode(encoding='utf-8')
        
        # Dựng authorization
        authorization_origin = f'api_key="{api_key}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature_sha_base64}"'
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode(encoding='utf-8')
        
        # Dựng URL WebSocket cuối cùng
        v = {
            "authorization": authorization,
            "date": date,
            "host": host
        }
        url = api_url + '?' + urlencode(v)
        return url
    
    def _build_xunfei_request(self, app_id, text, voice):
        """Xây dựng cấu trúc request TTS iFlytek"""
        return {
            "header": {
                "app_id": app_id,
                "status": 2,
            },
            "parameter": {
                "oral": {
                    "oral_level": "mid",
                    "spark_assist": 1,
                    "stop_split": 0,
                    "remain": 0
                },
                "tts": {
                    "vcn": voice,
                    "speed": 50,
                    "volume": 50,
                    "pitch": 50,
                    "bgs": 0,
                    "reg": 0,
                    "rdn": 0,
                    "rhy": 0,
                    "audio": {
                        "encoding": "raw",
                        "sample_rate": 24000,
                        "channels": 1,
                        "bit_depth": 16,
                        "frame_size": 0
                    }
                }
            },
            "payload": {
                "text": {
                    "encoding": "utf8",
                    "compress": "raw",
                    "format": "plain",
                    "status": 2,
                    "seq": 1,
                    "text": base64.b64encode(text.encode('utf-8')).decode('utf-8')
                }
            }
        }


    def _calculate_result(self, service_name, latencies, test_count):
        """Tính kết quả test (xử lý đúng giá trị None, loại test thất bại)"""
        # Loại test thất bại (giá trị None và độ trễ <=0), chỉ thống kê độ trễ hợp lệ
        valid_latencies = [l for l in latencies if l is not None and l > 0]
        if valid_latencies:
            avg_latency = sum(valid_latencies) / len(valid_latencies)
            status = f"Thành công ({len(valid_latencies)}/{test_count} lần hợp lệ)"
        else:
            avg_latency = 0
            status = "Thất bại: mọi test đều thất bại"
        return {"name": service_name, "latency": avg_latency, "status": status}

    def _print_results(self, test_text, test_count):
        """In kết quả test"""
        if not self.results:
            print("Không có kết quả test TTS hợp lệ")
            return

        print(f"\n{'='*60}")
        print("Kết quả test độ trễ từ đầu tiên của TTS streaming")
        print(f"{'='*60}")
        print(f"Text test: {test_text}")
        print(f"Số lần test: mỗi dịch vụ TTS test {test_count} lần")

        # Sắp xếp kết quả: thành công trước, tăng dần theo độ trễ
        success_results = sorted(
            [r for r in self.results if "Thành công" in r["status"]],
            key=lambda x: x["latency"]
        )
        failed_results = [r for r in self.results if "Thành công" not in r["status"]]

        table_data = [
            [r["name"], f"{r['latency']:.3f}", r["status"]]
            for r in success_results + failed_results
        ]

        print(tabulate(table_data, headers=["Dịch vụ TTS", "Độ trễ từ đầu (giây)", "Trạng thái"], tablefmt="grid"))
        print("\nGiải thích test: đo thời gian từ khi thiết lập kết nối đến khi nhận khối dữ liệu audio đầu tiên (gồm handshake, xác thực, gửi text), lấy trung bình nhiều lần test")
        print("- Điểm bấm giờ: trước khi thiết lập kết nối WebSocket/HTTP (thống nhất gồm toàn quy trình thiết lập kết nối mạng, handshake, gửi text)")
        print("- Kiểm soát timeout: mỗi request chờ tối đa 10 giây")
        print("- Xử lý lỗi: test thất bại không tính vào trung bình, chỉ thống kê độ trễ của test thành công")
        print("- Quy tắc sắp xếp: theo thời gian TB từ nhanh đến chậm")


    async def run(self, test_text=None, test_count=5):
        """Thực hiện test
        
        Args:
            test_text: Text muốn test, nếu None thì dùng text mặc định
            test_count: Số lần test của mỗi dịch vụ TTS
        """
        test_text = test_text or self.test_texts[0]
        print(f"Bắt đầu test độ trễ từ đầu tiên của TTS streaming...")
        print(f"Text test: {test_text}")
        print(f"Số lần test mỗi dịch vụ TTS: {test_count} lần")
        
        if not self.config.get("TTS"):
            print("Không tìm thấy cấu hình TTS trong file cấu hình")
            return
        
        # Test từng dịch vụ TTS
        self.results = []
        
        # Test Alibaba Cloud TTS
        result = await self.test_aliyun_tts(test_text, test_count)
        self.results.append(result)

        # Test Alibaba Cloud ModelStudio TTS
        if self.config.get("TTS", {}).get("AliBLTTS"):
            result = await self.test_alibl_tts(test_text, test_count)
            self.results.append(result)

        # Test Volcengine TTS
        result = await self.test_doubao_tts(test_text, test_count)
        self.results.append(result)
        
        # Test PaddleSpeech TTS
        result = await self.test_paddlespeech_tts(test_text, test_count)
        self.results.append(result)
        
        # Test Linkerai TTS
        result = await self.test_linkerai_tts(test_text, test_count)
        self.results.append(result)
        
        # Test IndexStream TTS
        result = await self.test_indexstream_tts(test_text, test_count)
        self.results.append(result)
        
        # Test iFlytek TTS
        if self.config.get("TTS", {}).get("XunFeiTTS"):
            result = await self.test_xunfei_tts(test_text, test_count)
            self.results.append(result)
        
        # In kết quả
        self._print_results(test_text, test_count)


async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Công cụ test độ trễ từ đầu tiên của TTS streaming")
    parser.add_argument("--text", help="Nội dung text muốn test")
    parser.add_argument("--count", type=int, default=5, help="Số lần test mỗi dịch vụ TTS")
    
    args = parser.parse_args()
    config = await load_config()
    await StreamTTSPerformanceTester(config).run(args.text, args.count)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
