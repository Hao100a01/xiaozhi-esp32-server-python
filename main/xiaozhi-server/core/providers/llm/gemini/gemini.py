import os, json, uuid
from types import SimpleNamespace
from typing import Any, Dict, List

import requests
from google import generativeai as genai
from google.generativeai import types, GenerationConfig

from core.providers.llm.base import LLMProviderBase
from core.utils.util import check_model_key
from config.logger import setup_logging
from google.generativeai.types import GenerateContentResponse
from requests import RequestException

log = setup_logging()
TAG = __name__


def test_proxy(proxy_url: str, test_url: str) -> bool:
    try:
        resp = requests.get(test_url, proxies={"http": proxy_url, "https": proxy_url})
        return 200 <= resp.status_code < 400
    except RequestException:
        return False


def setup_proxy_env(http_proxy: str | None, https_proxy: str | None):
    """
    Kiểm tra riêng proxy HTTP và HTTPS có khả dụng không, rồi đặt biến môi trường.
    Nếu proxy HTTPS không khả dụng nhưng HTTP khả dụng, sẽ cho HTTPS_PROXY cũng trỏ tới HTTP.
    """
    test_http_url = "http://www.google.com"
    test_https_url = "https://www.google.com"

    ok_http = ok_https = False

    if http_proxy:
        ok_http = test_proxy(http_proxy, test_http_url)
        if ok_http:
            os.environ["HTTP_PROXY"] = http_proxy
            log.bind(tag=TAG).info(f"Proxy HTTPS Gemini do cấu hình cung cấp kết nối thành công: {http_proxy}")
        else:
            log.bind(tag=TAG).warning(f"Proxy HTTP Gemini do cấu hình cung cấp không khả dụng: {http_proxy}")

    if https_proxy:
        ok_https = test_proxy(https_proxy, test_https_url)
        if ok_https:
            os.environ["HTTPS_PROXY"] = https_proxy
            log.bind(tag=TAG).info(f"Proxy HTTPS Gemini do cấu hình cung cấp kết nối thành công: {https_proxy}")
        else:
            log.bind(tag=TAG).warning(
                f"Proxy HTTPS Gemini do cấu hình cung cấp không khả dụng: {https_proxy}"
            )

    # Nếu https_proxy không khả dụng, nhưng http_proxy khả dụng và đi được https, thì tái dùng http_proxy làm https_proxy
    if ok_http and not ok_https:
        if test_proxy(http_proxy, test_https_url):
            os.environ["HTTPS_PROXY"] = http_proxy
            ok_https = True
            log.bind(tag=TAG).info(f"Tái dùng proxy HTTP làm proxy HTTPS: {http_proxy}")

    if not ok_http and not ok_https:
        log.bind(tag=TAG).error(
            f"Thiết lập proxy Gemini thất bại: cả proxy HTTP và HTTPS đều không khả dụng, vui lòng kiểm tra cấu hình"
        )
        raise RuntimeError("Cả proxy HTTP và HTTPS đều không khả dụng, vui lòng kiểm tra cấu hình")


class LLMProvider(LLMProviderBase):
    def __init__(self, cfg: Dict[str, Any]):
        self.model_name = cfg.get("model_name", "gemini-2.0-flash")
        self.api_key = cfg["api_key"]
        http_proxy = cfg.get("http_proxy")
        https_proxy = cfg.get("https_proxy")

        model_key_msg = check_model_key("LLM", self.api_key)
        if model_key_msg:
            log.bind(tag=TAG).error(model_key_msg)

        if http_proxy or https_proxy:
            log.bind(tag=TAG).info(
                f"Đã phát hiện cấu hình proxy Gemini, bắt đầu kiểm tra kết nối proxy và thiết lập môi trường proxy..."
            )
            setup_proxy_env(http_proxy, https_proxy)
            log.bind(tag=TAG).info(
                f"Thiết lập proxy Gemini thành công - HTTP: {http_proxy}, HTTPS: {https_proxy}"
            )
        # Cấu hình khoá API
        genai.configure(api_key=self.api_key)

        # Đặt thời gian chờ yêu cầu (giây)
        self.timeout = cfg.get("timeout", 120)  # Mặc định 120 giây

        # Tạo instance mô hình
        self.model = genai.GenerativeModel(self.model_name)

        self.gen_cfg = GenerationConfig(
            temperature=0.7,
            top_p=0.9,
            top_k=40,
            max_output_tokens=2048,
        )

    @staticmethod
    def _build_tools(funcs: List[Dict[str, Any]] | None):
        if not funcs:
            return None
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=f["function"]["name"],
                        description=f["function"]["description"],
                        parameters=f["function"]["parameters"],
                    )
                    for f in funcs
                ]
            )
        ]

    # Tài liệu Gemini nói rằng không cần duy trì session-id, dùng trực tiếp dialogue nối lại
    def response(self, session_id, dialogue, **kwargs):
        yield from self._generate(dialogue, None)

    def response_with_functions(self, session_id, dialogue, functions=None):
        yield from self._generate(dialogue, self._build_tools(functions))

    def _generate(self, dialogue, tools):
        role_map = {"assistant": "model", "user": "user"}
        contents: list = []
        # Nối hội thoại
        for m in dialogue:
            r = m["role"]

            if r == "assistant" and "tool_calls" in m:
                tc = m["tool_calls"][0]
                contents.append(
                    {
                        "role": "model",
                        "parts": [
                            {
                                "function_call": {
                                    "name": tc["function"]["name"],
                                    "args": json.loads(tc["function"]["arguments"]),
                                }
                            }
                        ],
                    }
                )
                continue

            if r == "tool":
                contents.append(
                    {
                        "role": "model",
                        "parts": [{"text": str(m.get("content", ""))}],
                    }
                )
                continue

            contents.append(
                {
                    "role": role_map.get(r, "user"),
                    "parts": [{"text": str(m.get("content", ""))}],
                }
            )

        stream: GenerateContentResponse = self.model.generate_content(
            contents=contents,
            generation_config=self.gen_cfg,
            tools=tools,
            stream=True,
            timeout=self.timeout,
        )

        try:
            for chunk in stream:
                cand = chunk.candidates[0]
                for part in cand.content.parts:
                    # a) Gọi hàm - thường đoạn cuối cùng mới là gọi hàm
                    if getattr(part, "function_call", None):
                        fc = part.function_call
                        yield None, [
                            SimpleNamespace(
                                id=uuid.uuid4().hex,
                                type="function",
                                function=SimpleNamespace(
                                    name=fc.name,
                                    arguments=json.dumps(
                                        dict(fc.args), ensure_ascii=False
                                    ),
                                ),
                            )
                        ]
                        return
                    # b) Văn bản thường
                    if getattr(part, "text", None):
                        yield part.text if tools is None else (part.text, None)

        finally:
            if tools is not None:
                yield None, None  # function-mode kết thúc, trả về gói rỗng

    # Đóng stream, dành sẵn phương thức cho chức năng ngắt hội thoại sau này; tài liệu chính thức khuyến nghị khi ngắt hội thoại phải đóng stream trước đó, giúp giảm đáng kể tính phí hạn ngạch và chiếm dụng tài nguyên
    @staticmethod
    def _safe_finish_stream(stream: GenerateContentResponse):
        if hasattr(stream, "resolve"):
            stream.resolve()  # Gemini SDK version ≥ 0.5.0
        elif hasattr(stream, "close"):
            stream.close()  # Gemini SDK version < 0.5.0
        else:
            for _ in stream:  # Dùng hết để dự phòng
                pass
