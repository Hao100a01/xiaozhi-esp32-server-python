from typing import Dict, Any
from config.logger import setup_logging
from core.utils import tts, llm, intent, memory, vad, asr

TAG = __name__
logger = setup_logging()


def initialize_modules(
    logger,
    config: Dict[str, Any],
    init_vad=False,
    init_asr=False,
    init_llm=False,
    init_tts=False,
    init_memory=False,
    init_intent=False,
) -> Dict[str, Any]:
    """
    Khởi tạo mọi module thành phần

    Args:
        config: Dict cấu hình

    Returns:
        Dict[str, Any]: Dict chứa mọi module đã khởi tạo
    """
    modules = {}

    # Khởi tạo module TTS
    if init_tts:
        select_tts_module = config["selected_module"]["TTS"]
        modules["tts"] = initialize_tts(config)
        logger.bind(tag=TAG).info(f"Khởi tạo thành phần: tts thành công {select_tts_module}")

    # Khởi tạo module LLM
    if init_llm:
        select_llm_module = config["selected_module"]["LLM"]
        llm_type = (
            select_llm_module
            if "type" not in config["LLM"][select_llm_module]
            else config["LLM"][select_llm_module]["type"]
        )
        modules["llm"] = llm.create_instance(
            llm_type,
            config["LLM"][select_llm_module],
        )
        logger.bind(tag=TAG).info(f"Khởi tạo thành phần: llm thành công {select_llm_module}")

    # Khởi tạo module Intent
    if init_intent:
        select_intent_module = config["selected_module"]["Intent"]
        intent_type = (
            select_intent_module
            if "type" not in config["Intent"][select_intent_module]
            else config["Intent"][select_intent_module]["type"]
        )
        modules["intent"] = intent.create_instance(
            intent_type,
            config["Intent"][select_intent_module],
        )
        logger.bind(tag=TAG).info(f"Khởi tạo thành phần: intent thành công {select_intent_module}")

    # Khởi tạo module Memory
    if init_memory:
        select_memory_module = config["selected_module"]["Memory"]
        memory_type = (
            select_memory_module
            if "type" not in config["Memory"][select_memory_module]
            else config["Memory"][select_memory_module]["type"]
        )
        modules["memory"] = memory.create_instance(
            memory_type,
            config["Memory"][select_memory_module],
            config.get("summaryMemory", None),
        )
        logger.bind(tag=TAG).info(f"Khởi tạo thành phần: memory thành công {select_memory_module}")

    # Khởi tạo module VAD
    if init_vad:
        select_vad_module = config["selected_module"]["VAD"]
        vad_type = (
            select_vad_module
            if "type" not in config["VAD"][select_vad_module]
            else config["VAD"][select_vad_module]["type"]
        )
        modules["vad"] = vad.create_instance(
            vad_type,
            config["VAD"][select_vad_module],
        )
        logger.bind(tag=TAG).info(f"Khởi tạo thành phần: vad thành công {select_vad_module}")

    # Khởi tạo module ASR
    if init_asr:
        select_asr_module = config["selected_module"]["ASR"]
        modules["asr"] = initialize_asr(config)
        logger.bind(tag=TAG).info(f"Khởi tạo thành phần: asr thành công {select_asr_module}")
    return modules


def initialize_tts(config):
    select_tts_module = config["selected_module"]["TTS"]
    tts_config = config["TTS"][select_tts_module].copy()
    tts_config.setdefault("tts_timeout", config.get("tts_timeout", 15))
    tts_type = (
        select_tts_module
        if "type" not in tts_config
        else tts_config["type"]
    )
    new_tts = tts.create_instance(
        tts_type,
        tts_config,
        str(config.get("delete_audio", True)).lower() in ("true", "1", "yes"),
    )
    return new_tts


def initialize_asr(config):
    select_asr_module = config["selected_module"]["ASR"]
    asr_type = (
        select_asr_module
        if "type" not in config["ASR"][select_asr_module]
        else config["ASR"][select_asr_module]["type"]
    )
    new_asr = asr.create_instance(
        asr_type,
        config["ASR"][select_asr_module],
        str(config.get("delete_audio", True)).lower() in ("true", "1", "yes"),
    )
    logger.bind(tag=TAG).info("Khởi tạo module ASR hoàn tất")
    return new_asr


def initialize_voiceprint(asr_instance, config):
    """Khởi tạo chức năng nhận diện voiceprint"""
    voiceprint_config = config.get("voiceprint")
    if not voiceprint_config:
        return False  

    # Áp dụng cấu hình
    if not voiceprint_config.get("url") or not voiceprint_config.get("speakers"):
        logger.bind(tag=TAG).warning("Cấu hình nhận diện voiceprint chưa đầy đủ")
        return False
        
    try:
        asr_instance.init_voiceprint(voiceprint_config)
        logger.bind(tag=TAG).info("Chức năng nhận diện voiceprint của module ASR đã được bật động")
        logger.bind(tag=TAG).info(f"Số lượng người nói đã cấu hình: {len(voiceprint_config['speakers'])}")
        return True
    except Exception as e:
        logger.bind(tag=TAG).error(f"Khởi tạo động chức năng nhận diện voiceprint thất bại: {str(e)}")
        return False
