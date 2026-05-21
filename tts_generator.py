"""
TTS Generator - 使用 Microsoft Edge TTS 生成中文播客音频
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime

import edge_tts

from config import (
    TTS_VOICE, TTS_FALLBACK_VOICE, TTS_RATE,
    EPISODE_INTRO, EPISODE_OUTRO, CATEGORIES,
)

logger = logging.getLogger(__name__)


def build_episode_script(summaries: dict) -> str:
    """
    从摘要 JSON 构建完整播客脚本。
    
    每篇研报先报：名称、发布机构、发布日期，再读摘要。
    不删减摘要内容。
    
    Args:
        summaries: 包含 categories 键的摘要字典
        
    Returns:
        完整播客脚本文本（中文）
    """
    # 检查是否有预生成的完整脚本
    if summaries.get("full_script"):
        return summaries["full_script"]
    
    parts = [EPISODE_INTRO]
    
    categories = summaries.get("categories", {})
    for cat_key in CATEGORIES:
        cat_data = categories.get(cat_key, {})
        if not cat_data:
            continue
        
        header = cat_data.get("header", CATEGORIES[cat_key]["header"])
        parts.append(f"\n{header}。\n")
        
        reports = cat_data.get("reports", [])
        for i, report in enumerate(reports, 1):
            title = report.get("title", "")
            source = report.get("source", "")
            date = report.get("date", "")
            summary = report.get("summary", "")
            
            # 先报研报信息：名称、机构、日期
            parts.append(
                f"第{i}篇。{title}。{source}，{date}发布。\n"
            )
            # 再读摘要（原文，不删减）
            parts.append(summary)
            parts.append("\n")
    
    parts.append(EPISODE_OUTRO)
    
    return "\n".join(parts)


async def _text_to_speech(text: str, voice: str, output_path: str) -> None:
    """
    使用 edge-tts 将文本转为语音 MP3。
    
    Args:
        text: 要转换的中文文本
        voice: Microsoft Edge TTS 语音名称
        output_path: 输出 MP3 文件路径
    """
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=TTS_RATE,
    )
    await communicate.save(output_path)


def _get_file_size(file_path: str) -> int:
    """获取文件大小的字节数"""
    return os.path.getsize(file_path)


def _estimate_duration(file_path: str) -> float:
    """
    估算音频时长（秒）。
    使用近似公式: 文件大小(字节) / (比特率 * 1024 / 8)
    edge-tts 默认约 48kbps mono MP3
    """
    size_bytes = _get_file_size(file_path)
    bitrate_kbps = 48  # edge-tts 默认比特率
    duration_sec = size_bytes / (bitrate_kbps * 1024 / 8)
    return round(duration_sec, 1)


def generate_episode_audio(
    summaries: dict,
    output_path: str,
    voice: str = None,
) -> tuple:
    """
    从摘要生成单集 MP3 音频。
    
    Args:
        summaries: 摘要 JSON 数据
        output_path: 输出 MP3 文件路径
        voice: 可选覆盖语音
        
    Returns:
        (output_path, file_size_bytes, duration_seconds)
        
    Raises:
        RuntimeError: TTS 生成失败
    """
    voice = voice or TTS_VOICE
    
    # 构建脚本
    script = build_episode_script(summaries)
    logger.info(f"Episode script length: {len(script)} chars")
    
    # 如果文本太长，分段处理
    max_chars = 3000  # edge-tts 单次建议上限
    if len(script) <= max_chars:
        # 直接生成
        asyncio.run(_text_to_speech(script, voice, output_path))
    else:
        # 分段生成后拼接
        logger.info(f"Script too long ({len(script)} chars), splitting...")
        _generate_long_audio(script, voice, output_path, max_chars)
    
    file_size = _get_file_size(output_path)
    duration = _estimate_duration(output_path)
    
    logger.info(
        f"Audio generated: {output_path} "
        f"({file_size} bytes, ~{duration}s)"
    )
    
    return (output_path, file_size, duration)


def _generate_long_audio(
    script: str,
    voice: str,
    output_path: str,
    max_chars: int = 3000,
) -> None:
    """
    分段生成音频，然后拼接。
    按段落边界分割，避免在句子中间断开。
    """
    import tempfile
    
    # 按段落分割
    paragraphs = script.split("\n\n")
    segments = []
    current = ""
    
    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_chars:
            current += para + "\n\n" if current else para
        else:
            if current:
                segments.append(current.strip())
            current = para
    
    if current.strip():
        segments.append(current.strip())
    
    logger.info(f"Split into {len(segments)} segments")
    
    temp_files = []
    temp_dir = tempfile.mkdtemp()
    
    try:
        # 逐段生成
        for i, segment in enumerate(segments):
            temp_path = os.path.join(temp_dir, f"seg_{i:03d}.mp3")
            logger.info(f"Generating segment {i+1}/{len(segments)} ({len(segment)} chars)")
            asyncio.run(_text_to_speech(segment, voice, temp_path))
            temp_files.append(temp_path)
        
        # 用简单的二进制拼接（MP3 帧可以直接拼接）
        with open(output_path, "wb") as out:
            for tf in temp_files:
                with open(tf, "rb") as inf:
                    out.write(inf.read())
        
        logger.info(f"Concatenated {len(temp_files)} segments -> {output_path}")
    
    finally:
        # 清理临时文件
        for tf in temp_files:
            try:
                os.remove(tf)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass


def format_duration(seconds: float) -> str:
    """将秒数格式化为 HH:MM:SS 或 MM:SS"""
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
