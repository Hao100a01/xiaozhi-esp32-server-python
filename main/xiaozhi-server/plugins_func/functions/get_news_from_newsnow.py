import random
import httpx
from io import BytesIO
from markitdown import MarkItDown, StreamInfo
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler


TAG = __name__
logger = setup_logging()

CHANNEL_MAP = {
    "V2EX": "v2ex-share",
    "知乎": "zhihu",
    "微博": "weibo",
    "联合早报": "zaobao",
    "酷安": "coolapk",
    "MKTNews": "mktnews-flash",
    "华尔街见闻": "wallstreetcn-quick",
    "36氪": "36kr-quick",
    "抖音": "douyin",
    "虎扑": "hupu",
    "百度贴吧": "tieba",
    "今日头条": "toutiao",
    "IT之家": "ithome",
    "澎湃新闻": "thepaper",
    "卫星通讯社": "sputniknewscn",
    "参考消息": "cankaoxiaoxi",
    "远景论坛": "pcbeta-windows11",
    "财联社": "cls-depth",
    "雪球": "xueqiu-hotstock",
    "格隆汇": "gelonghui",
    "法布财经": "fastbull-express",
    "Solidot": "solidot",
    "Hacker News": "hackernews",
    "Product Hunt": "producthunt",
    "Github": "github-trending-today",
    "哔哩哔哩": "bilibili-hot-search",
    "快手": "kuaishou",
    "靠谱新闻": "kaopu",
    "金十数据": "jin10",
    "百度热搜": "baidu",
    "牛客": "nowcoder",
    "少数派": "sspai",
    "稀土掘金": "juejin",
    "凤凰网": "ifeng",
    "虫部落": "chongbuluo-latest",
}

# Dict nguồn tin mặc định, dùng khi cấu hình không chỉ định
DEFAULT_NEWS_SOURCES = "澎湃新闻;百度热搜;财联社"

def _get_newsnow_config(conn):
    # Lấy từ cấu hình kết nối
    plugins = conn.config.get("plugins", {})
    newsnow = plugins.get("get_news_from_newsnow", {})
    sources = newsnow.get("news_sources", "")
    if isinstance(sources, str) and sources.strip():
        return sources

    return ""

def get_news_sources_from_config(conn):
    """Lấy chuỗi nguồn tin từ cấu hình"""
    try:
        result = _get_newsnow_config(conn)
        if result:
            logger.bind(tag=TAG).debug(f"Dùng nguồn tin đã cấu hình: {result}")
            return result

        logger.bind(tag=TAG).debug("Không tìm thấy cấu hình nguồn tin, dùng cấu hình mặc định")
        return DEFAULT_NEWS_SOURCES

    except Exception as e:
        logger.bind(tag=TAG).error(f"Lấy cấu hình nguồn tin thất bại: {e}, dùng cấu hình mặc định")
        return DEFAULT_NEWS_SOURCES


# Lấy tên nguồn tin khả dụng từ cấu hình mặc định (lúc chạy do get_news_sources_from_config lấy động)
example_sources_str = DEFAULT_NEWS_SOURCES.replace(";","、")

GET_NEWS_FROM_NEWSNOW_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_news_from_newsnow",
        "description": "Khi người dùng yêu cầu xem hoặc nghe tin tức thì gọi (như 'cho một tin tức', 'hôm nay có tin gì').",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": f"Tên chuẩn của nguồn tin (tên tiếng Trung), ví dụ: {example_sources_str}... Tham số tùy chọn, không cung cấp thì dùng nguồn tin mặc định",
                },
                "detail": {
                    "type": "boolean",
                    "description": "Có lấy nội dung chi tiết không, mặc định false. Nếu true thì lấy nội dung chi tiết của tin tức trước đó",
                },
                "lang": {
                    "type": "string",
                    "description": "Mã ngôn ngữ (code) người dùng sử dụng, ví dụ: zh_CN/zh_HK/en_US/ja_JP..., mặc định zh_CN",
                },
            },
            "required": ["lang"],
        },
    },
}


async def fetch_news_from_api(conn: "ConnectionHandler", source="thepaper"):
    """Lấy danh sách tin tức từ API"""
    try:
        api_url = f"https://newsnow.busiyi.world/api/s?id={source}"

        news_config = conn.config.get("plugins", {}).get("get_news_from_newsnow", {})
        if news_config.get("url"):
            api_url = news_config["url"] + source

        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.get(api_url, headers=headers)

        data = response.json()

        if "items" in data:
            return data["items"]
        else:
            logger.bind(tag=TAG).error(f"Định dạng response API tin tức bị lỗi: {data}")
            return []

    except Exception as e:
        logger.bind(tag=TAG).error(f"Lấy API tin tức thất bại: {e}")
        return []


async def fetch_news_detail(url):
    """Lấy nội dung trang chi tiết tin tức và dùng MarkItDown để làm sạch HTML"""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.get(url, headers=headers)

        # Dùng MarkItDown làm sạch nội dung HTML
        md = MarkItDown(enable_plugins=False)
        result = md.convert_stream(
            BytesIO(response.content),
            stream_info=StreamInfo(
                mimetype="text/html",
                extension=".html",
                charset=response.encoding or "utf-8",
            ),
        )

        # Lấy nội dung text sau khi làm sạch
        clean_text = result.text_content

        # Nếu nội dung sau khi làm sạch rỗng, trả về thông tin gợi ý
        if not clean_text or len(clean_text.strip()) == 0:
            logger.bind(tag=TAG).warning(f"Nội dung tin tức sau khi làm sạch rỗng: {url}")
            return "Không phân tích được nội dung chi tiết tin tức, có thể cấu trúc website đặc biệt hoặc nội dung bị hạn chế."

        return clean_text
    except Exception as e:
        logger.bind(tag=TAG).error(f"Lấy chi tiết tin tức thất bại: {e}")
        return "无法获取详细内容"


@register_function(
    "get_news_from_newsnow",
    GET_NEWS_FROM_NEWSNOW_FUNCTION_DESC,
    ToolType.SYSTEM_CTL,
)
async def get_news_from_newsnow(
    conn: "ConnectionHandler",
    source: str = "澎湃新闻",
    detail: bool = False,
    lang: str = "zh_CN",
):
    """Lấy tin tức và chọn ngẫu nhiên một tin để đọc, hoặc lấy nội dung chi tiết của tin trước đó"""
    try:
        # Lấy nguồn tin đang cấu hình
        news_sources = get_news_sources_from_config(conn)

        # Nếu detail là True, lấy nội dung chi tiết của tin trước đó
        detail = str(detail).lower() == "true"
        if detail:
            if (
                not hasattr(conn, "last_newsnow_link")
                or not conn.last_newsnow_link
                or "url" not in conn.last_newsnow_link
            ):
                return ActionResponse(
                    Action.REQLLM,
                    "Xin lỗi, không tìm thấy tin tức đã tra gần đây, vui lòng lấy một tin trước.",
                    None,
                )

            url = conn.last_newsnow_link.get("url")
            title = conn.last_newsnow_link.get("title", "Tiêu đề không xác định")
            source_id = conn.last_newsnow_link.get("source_id", "thepaper")
            source_name = CHANNEL_MAP.get(source_id, "Nguồn không xác định")

            if not url or url == "#":
                return ActionResponse(
                    Action.REQLLM, "Xin lỗi, tin này không có link khả dụng để lấy nội dung chi tiết.", None
                )

            logger.bind(tag=TAG).debug(
                f"Lấy chi tiết tin tức: {title}, nguồn: {source_name}, URL={url}"
            )

            # Lấy chi tiết tin tức
            detail_content = await fetch_news_detail(url)

            if not detail_content or detail_content == "无法获取详细内容":
                return ActionResponse(
                    Action.REQLLM,
                    f"Xin lỗi, không lấy được nội dung chi tiết của «{title}», có thể link đã hết hạn hoặc cấu trúc website đã thay đổi.",
                    None,
                )

            # Dựng báo cáo chi tiết
            detail_report = (
                f"Dựa vào dữ liệu dưới đây, hãy trả lời yêu cầu chi tiết tin tức của người dùng bằng {lang}:\n\n"
                f"Tiêu đề tin tức: {title}\n"
                # f"Nguồn tin tức: {source_name}\n"
                f"Nội dung chi tiết: {detail_content}\n\n"
                f"(Hãy tóm tắt nội dung tin tức trên, trích xuất thông tin chính, đọc cho người dùng một cách tự nhiên, trôi chảy,"
                f"đừng nhắc đây là tóm tắt, cứ như đang kể một tin tức hoàn chỉnh)"
            )

            return ActionResponse(Action.REQLLM, detail_report, None)

        # Ngược lại, lấy danh sách tin tức và chọn ngẫu nhiên một tin
        # Chuyển tên tiếng Trung sang ID tiếng Anh
        english_source_id = None

        # Kiểm tra tên tiếng Trung nhập vào có trong nguồn tin đã cấu hình không
        news_sources_list = [
            name.strip() for name in news_sources.split(";") if name.strip()
        ]
        if source in news_sources_list:
            # Nếu tên tiếng Trung nhập vào có trong nguồn tin đã cấu hình, tìm ID tiếng Anh tương ứng trong CHANNEL_MAP
            english_source_id = CHANNEL_MAP.get(source)

        # Nếu không tìm thấy ID tiếng Anh tương ứng, dùng nguồn mặc định
        if not english_source_id:
            logger.bind(tag=TAG).warning(f"Nguồn tin không hợp lệ: {source}, dùng nguồn mặc định 澎湃新闻")
            english_source_id = "thepaper"
            source = "澎湃新闻"

        logger.bind(tag=TAG).info(f"Lấy tin tức: nguồn tin={source}({english_source_id})")

        # Lấy danh sách tin tức
        news_items = await fetch_news_from_api(conn, english_source_id)

        if not news_items:
            return ActionResponse(
                Action.REQLLM,
                f"Xin lỗi, không lấy được thông tin tin tức từ {source}, vui lòng thử lại sau hoặc thử nguồn tin khác.",
                None,
            )

        # Chọn ngẫu nhiên một tin tức
        selected_news = random.choice(news_items)

        # Lưu link tin hiện tại vào object kết nối để tra chi tiết sau
        if not hasattr(conn, "last_newsnow_link"):
            conn.last_newsnow_link = {}
        conn.last_newsnow_link = {
            "url": selected_news.get("url", "#"),
            "title": selected_news.get("title", "Tiêu đề không xác định"),
            "source_id": english_source_id,
        }

        # Dựng báo cáo tin tức
        news_report = (
            f"Dựa vào dữ liệu dưới đây, hãy trả lời yêu cầu tin tức của người dùng bằng {lang}:\n\n"
            f"Tiêu đề tin tức: {selected_news['title']}\n"
            # f"Nguồn tin tức: {source}\n"
            f"(Hãy đọc tiêu đề tin tức này cho người dùng một cách tự nhiên, trôi chảy,"
            f"gợi ý người dùng có thể yêu cầu lấy nội dung chi tiết, lúc đó sẽ lấy nội dung chi tiết của tin tức.)"
        )

        return ActionResponse(Action.REQLLM, news_report, None)

    except Exception as e:
        logger.bind(tag=TAG).error(f"Lấy tin tức bị lỗi: {e}")
        return ActionResponse(
            Action.REQLLM, "Xin lỗi, đã xảy ra lỗi khi lấy tin tức, vui lòng thử lại sau.", None
        )
