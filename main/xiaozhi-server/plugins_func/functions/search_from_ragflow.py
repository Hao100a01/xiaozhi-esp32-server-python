import json
import httpx
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# Định nghĩa template mô tả hàm cơ bản
SEARCH_FROM_RAGFLOW_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "search_from_ragflow",
        "description": "Tra cứu thông tin từ knowledge base",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "Câu hỏi cần tra cứu"}},
            "required": ["question"],
        },
    },
}


@register_function(
    "search_from_ragflow", SEARCH_FROM_RAGFLOW_FUNCTION_DESC, ToolType.SYSTEM_CTL
)
async def search_from_ragflow(conn: "ConnectionHandler", question=None):
    # Đảm bảo tham số chuỗi được xử lý encoding đúng
    if question and isinstance(question, str):
        # Đảm bảo tham số câu hỏi là chuỗi encoding UTF-8
        pass
    else:
        question = str(question) if question is not None else ""

    ragflow_config = conn.config.get("plugins", {}).get("search_from_ragflow", {})
    base_url = ragflow_config.get("base_url", "")
    api_key = ragflow_config.get("api_key", "")
    dataset_ids = ragflow_config.get("dataset_ids", [])

    url = base_url + "/api/v1/retrieval"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # Đảm bảo các chuỗi trong payload đều là UTF-8
    payload = {"question": question, "dataset_ids": dataset_ids}

    try:
        # Dùng ensure_ascii=False để khi JSON serialize xử lý đúng ký tự tiếng Trung
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0), verify=False) as client:
            response = await client.post(url, json=payload, headers=headers)

        # Đặt rõ encoding của response là utf-8
        response.encoding = "utf-8"

        response.raise_for_status()

        # Lấy nội dung text trước, rồi tự xử lý JSON decode
        response_text = response.text

        result = json.loads(response_text)

        if result.get("code") != 0:
            error_detail = result.get("error", {}).get("detail", "Lỗi không xác định")
            error_message = result.get("error", {}).get("message", "")
            error_code = result.get("code", "")

            # Ghi log thông tin lỗi một cách an toàn
            logger.bind(tag=TAG).error(
                f"Gọi RAGFlow API thất bại, mã response: {error_code}, chi tiết lỗi: {error_detail}, response đầy đủ: {result}"
            )

            # Xây dựng response lỗi chi tiết
            error_response = f"RAG API trả về bất thường (mã lỗi: {error_code})"

            if error_message:
                error_response += f": {error_message}"
            if error_detail:
                error_response += f"\nChi tiết: {error_detail}"

            return ActionResponse(Action.RESPONSE, None, error_response)

        chunks = result.get("data", {}).get("chunks", [])
        contents = []
        for chunk in chunks:
            content = chunk.get("content", "")
            if content:
                # Xử lý chuỗi nội dung một cách an toàn
                if isinstance(content, str):
                    contents.append(content)
                elif isinstance(content, bytes):
                    contents.append(content.decode("utf-8", errors="replace"))
                else:
                    contents.append(str(content))

        if contents:
            # Tổ chức nội dung knowledge base theo dạng trích dẫn
            context_text = f"# Về câu hỏi [{question}], tra được knowledge base như sau\n"
            context_text += "```\n\n\n".join(contents[:5])
            context_text += "\n```"
        else:
            context_text = "Theo kết quả tra cứu knowledge base, không có thông tin liên quan."
        return ActionResponse(Action.REQLLM, context_text, None)

    except httpx.TimeoutException as e:
        error_response = "Yêu cầu RAG API bị timeout"
        error_response += "\nNguyên nhân có thể: dịch vụ RAGflow phản hồi chậm hoặc trễ mạng"
        error_response += "\nGiải pháp: vui lòng thử lại sau hoặc kiểm tra hiệu năng dịch vụ RAGflow"
        return ActionResponse(Action.RESPONSE, None, error_response)

    except httpx.HTTPStatusError as e:
        if hasattr(e.response, "status_code"):
            status_code = e.response.status_code
            error_response = f"Lỗi HTTP RAG API (mã trạng thái: {status_code})"
            try:
                error_detail = e.response.json().get("error", {}).get("message", "")
                if error_detail:
                    error_response += f"\nChi tiết lỗi: {error_detail}"
            except:
                pass
        else:
            error_response = f"Ngoại lệ HTTP RAG API: {str(e)}"
        return ActionResponse(Action.RESPONSE, None, error_response)

    except httpx.HTTPError as e:
        error_response = "Không thể kết nối đến RAG API"
        error_response += "\nNguyên nhân có thể: địa chỉ dịch vụ RAGflow sai hoặc dịch vụ chưa chạy"
        error_response += "\nGiải pháp: vui lòng kiểm tra cấu hình địa chỉ và trạng thái dịch vụ RAGflow"
        return ActionResponse(Action.RESPONSE, None, error_response)

    except Exception as e:
        # Các exception khác
        error_type = type(e).__name__
        logger.bind(tag=TAG).error(
            f"Ngoại lệ xử lý RAGflow, loại ngoại lệ: {error_type}, chi tiết: {str(e)}"
        )

        # Cung cấp thông tin lỗi chi tiết
        error_response = f"Ngoại lệ xử lý RAG API ({error_type}): {str(e)}"
        return ActionResponse(Action.RESPONSE, None, error_response)
