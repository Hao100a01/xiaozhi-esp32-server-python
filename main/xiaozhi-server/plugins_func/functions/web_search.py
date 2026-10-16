import httpx
from config.logger import setup_logging
from plugins_func.register import (
    register_function,
    ToolType,
    ActionResponse,
    Action,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

_DEFAULT_DESCRIPTION = (
    "Công cụ tìm kiếm trên Internet. Dùng tool này khi người dùng cần tìm kiếm thông tin trên mạng một cách rõ ràng."
)

WEB_SEARCH_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": _DEFAULT_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Từ khóa hoặc câu hỏi tìm kiếm",
                }
            },
            "required": ["query"],
        },
    },
}


async def _search_metaso(api_key: str, query: str, max_results: int) -> str:
    """Gọi API tìm kiếm Metaso"""
    url = "https://metaso.cn/api/v1/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "size": max_results,
        "stream": False,
        "scope": "webpage",
        "includeSummary": True,
        "includeRawContent": False,
        "conciseSnippet": False,
    }
    logger.bind(tag=TAG).debug(f"Yêu cầu tìm kiếm Metaso | URL: {url} | payload: {payload}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"Response tìm kiếm Metaso | status: {response.status_code}")

    webpages = data.get("webpages", [])
    if not webpages:
        return "Không tìm thấy kết quả tìm kiếm liên quan."

    lines = ["【Kết quả tìm kiếm trên mạng】"]
    for i, item in enumerate(webpages, 1):
        title = item.get("title", "Không có tiêu đề")
        snippet = item.get("summary", "")
        date = item.get("date", "")
        lines.append(f"{i}. Tiêu đề: {title}")
        if date:
            lines.append(f"   Ngày: {date}")
        if snippet:
            lines.append(f"   Tóm tắt: {snippet}")

    return "\n".join(lines)


async def _search_tavily(api_key: str, query: str, max_results: int) -> str:
    """Gọi API tìm kiếm Tavily"""
    url = "https://api.tavily.com/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_answer": "advanced",
    }
    logger.bind(tag=TAG).debug(f"Yêu cầu tìm kiếm Tavily | URL: {url} | payload: {payload}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"Response tìm kiếm Tavily | status: {response.status_code} | data: {data}")

    results = data.get("results", [])
    if not results:
        return "Không tìm thấy kết quả tìm kiếm liên quan."

    answer = data.get("answer", "")
    lines = [f"【Kết quả tìm kiếm trên mạng】\nTóm tắt: {answer}"]
    # for i, item in enumerate(results, 1):
    #     title = item.get("title", "Không có tiêu đề")
    #     summary = item.get("content", "")
    #     lines.append(f"{i}. Tiêu đề: {title}")
    #     if summary:
    #         lines.append(f"   Tóm tắt: {summary}")

    return "\n".join(lines)


async def _search_serply(api_key: str, query: str, max_results: int) -> str:
    """Gọi API tìm kiếm Serply"""
    url = "https://api.serply.io/v1/search"
    headers = {
        "X-Api-Key": api_key,
        "User-Agent": "xiaozhi-esp32-server",
    }
    # Serply mỗi trang trả tối đa 10 kết quả, số lượng trả về có thể hơi nhiều hơn num, cần cắt bớt lại ở local
    params = {"q": query, "num": min(max_results, 10)}
    logger.bind(tag=TAG).debug(f"Yêu cầu tìm kiếm Serply | URL: {url} | params: {params}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.get(url, params=params, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"Response tìm kiếm Serply | status: {response.status_code}")

    if response.status_code != 200:
        detail = data.get("detail", "") if isinstance(data, dict) else ""
        logger.bind(tag=TAG).error(
            f"Tìm kiếm Serply thất bại | status: {response.status_code} | detail: {detail}"
        )
        return "Yêu cầu tìm kiếm trên mạng thất bại, vui lòng kiểm tra API Key có đúng không."

    results = data.get("results", [])
    if not results:
        return "Không tìm thấy kết quả tìm kiếm liên quan."

    lines = ["【Kết quả tìm kiếm trên mạng】"]
    for i, item in enumerate(results[:max_results], 1):
        title = item.get("title", "Không có tiêu đề")
        snippet = item.get("description", "")
        lines.append(f"{i}. Tiêu đề: {title}")
        if snippet:
            lines.append(f"   Tóm tắt: {snippet}")

    return "\n".join(lines)


@register_function("web_search", WEB_SEARCH_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def web_search(conn: "ConnectionHandler", query: str = None):
    logger.bind(tag=TAG).info(f"web_search được gọi | query={query}")
    if not query:
        return ActionResponse(Action.REQLLM, "Vui lòng cung cấp từ khóa tìm kiếm.", None)

    web_search_config = conn.config.get("plugins", {}).get("web_search", {})
    provider = web_search_config.get("provider", "").lower()
    max_results = int(web_search_config.get("max_results", 3))
    logger.bind(tag=TAG).info(f"Cấu hình web_search | provider={provider} | max_results={max_results} | config_keys={list(web_search_config.keys())}")

    api_key = web_search_config.get("api_key", "")
    if not api_key:
        return ActionResponse(
            Action.REQLLM,
            "Chức năng tìm kiếm trên mạng chưa được cấu hình API Key, vui lòng điền trong file cấu hình.",
            None,
        )

    try:
        if provider == "metaso":
            result_text = await _search_metaso(api_key, query, max_results)
        elif provider == "tavily":
            result_text = await _search_tavily(api_key, query, max_results)
        elif provider == "serply":
            result_text = await _search_serply(api_key, query, max_results)
        else:
            return ActionResponse(
                Action.REQLLM,
                f"Chức năng tìm kiếm trên mạng chưa được cấu hình hoặc nguồn tìm kiếm cấu hình không hợp lệ (hiện tại: {provider}), vui lòng kiểm tra cấu hình.",
                None,
            )
        logger.bind(tag=TAG).info(f"Đã ghép xong kết quả tìm kiếm:\n{result_text}")
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error("Yêu cầu tìm kiếm trên mạng bị timeout")
        result_text = "Yêu cầu tìm kiếm trên mạng bị timeout, vui lòng thử lại sau."
    except httpx.HTTPStatusError as e:
        logger.bind(tag=TAG).error(f"Yêu cầu tìm kiếm trên mạng thất bại: {e}")
        result_text = "Yêu cầu tìm kiếm trên mạng thất bại, vui lòng thử lại sau."
    except Exception as e:
        logger.bind(tag=TAG).error(f"Ngoại lệ tìm kiếm trên mạng: {e}")
        result_text = "Tìm kiếm trên mạng gặp ngoại lệ, vui lòng thử lại sau."

    return ActionResponse(Action.REQLLM, result_text, None)
