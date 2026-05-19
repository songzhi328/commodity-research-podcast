#!/usr/bin/env python3
"""
Commodity Research Podcast Pipeline
===================================
从 data/ 目录读取研报摘要 JSON，生成 TTS 音频播客，
更新 RSS Feed，推送到 GitHub。
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config
from tts_generator import generate_episode_audio, format_duration
from rss_generator import create_or_update_rss, get_episode_count_from_rss
from git_manager import ensure_repo_cloned, commit_and_push_episode, get_episode_count

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ============================================================
# 工具函数
# ============================================================

def find_latest_summaries(data_dir: Path) -> dict:
    """
    在 data/ 目录查找最新的摘要 JSON 文件。
    
    Args:
        data_dir: data 目录路径
        
    Returns:
        解析后的摘要字典，无文件时返回 None
    """
    if not data_dir.exists():
        logger.error(f"Data directory not found: {data_dir}")
        return None
    
    json_files = sorted(data_dir.glob("summaries_*.json"), reverse=True)
    
    if not json_files:
        logger.error(f"No summaries_*.json found in {data_dir}")
        return None
    
    latest_file = json_files[0]
    logger.info(f"Loading summaries from: {latest_file}")
    
    with open(latest_file, "r", encoding="utf-8") as f:
        return json.load(f)


def build_html_description(summaries: dict) -> str:
    """从摘要构建播客单集 HTML 描述"""
    parts = ["<p>本周大宗商品研究报告摘要：</p>"]
    
    categories = summaries.get("categories", {})
    for cat_key in ["coal", "oil", "nonferrous", "agriculture", "precious"]:
        cat_data = categories.get(cat_key, {})
        if not cat_data:
            continue
        
        header = cat_data.get("header", cat_key)
        parts.append(f"<h3>{header}</h3>")
        
        reports = cat_data.get("reports", [])
        for report in reports:
            title = report.get("title", "")
            source = report.get("source", "")
            date = report.get("date", "")
            summary_text = report.get("summary", "")
            
            src = f" ({source}, {date})" if source else ""
            parts.append(f"<p><strong>{title}</strong>{src}<br/>{summary_text}</p>")
    
    parts.append("<p><em>AI 生成，仅供参考，不构成投资建议。</em></p>")
    return "\n".join(parts)


def format_pub_date(date_str: str) -> str:
    """将 YYYY-MM-DD 转为 RFC 2822 格式"""
    # 使用北京时间 21:30（自动化运行时间）
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    dt = dt.replace(hour=21, minute=30, second=0, tzinfo=timezone(timedelta(hours=8)))
    # RFC 2822 格式
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0800")


def format_episode_title(date_str: str, summaries: dict) -> str:
    """生成播客单集标题"""
    week_num = summaries.get("week_number", "")
    if week_num:
        return f"第{week_num}周（{date_str}）：大宗商品研究周报"
    return f"{date_str} 大宗商品研究周报"


# ============================================================
# 主流程
# ============================================================

def main(date_override: str = None, summaries_path: str = None):
    """主流水线执行函数"""
    start_time = datetime.now()
    logger.info("=" * 60)
    logger.info("Commodity Research Podcast Pipeline - START")
    logger.info("=" * 60)
    
    # 确定日期
    episode_date = date_override or datetime.now().strftime("%Y-%m-%d")
    logger.info(f"Episode date: {episode_date}")
    
    # ---- Step 1: 加载摘要 ----
    logger.info("Step 1/4: Loading summaries...")
    
    if summaries_path:
        summaries_file = Path(summaries_path)
        if not summaries_file.exists():
            logger.error(f"Summaries file not found: {summaries_path}")
            sys.exit(1)
        with open(summaries_file, "r", encoding="utf-8") as f:
            summaries = json.load(f)
        logger.info(f"Loaded summaries from: {summaries_path}")
    else:
        summaries = find_latest_summaries(Path(config.DATA_DIR))
    
    if not summaries:
        logger.error("No summaries found. Agent must generate summaries first.")
        logger.error("Expected file: data/summaries_YYYY-MM-DD.json")
        sys.exit(1)
    
    # ---- Step 2: 同步 Git Repo ----
    logger.info("Step 2/4: Syncing GitHub repository...")
    
    try:
        repo = ensure_repo_cloned(config.REPO_DIR)
        logger.info("Repository ready")
    except Exception as e:
        logger.error(f"Git sync failed: {e}")
        logger.error(
            "Please create the repository first: "
            f"https://github.com/new?name={config.GITHUB_REPO}"
        )
        sys.exit(1)
    
    # ---- Step 3: 生成 TTS 音频 ----
    logger.info("Step 3/4: Generating TTS audio...")
    
    audio_filename = f"ep_{episode_date}.mp3"
    audio_path = os.path.join(config.AUDIO_DIR, audio_filename)
    
    try:
        output_path, file_size, duration = generate_episode_audio(
            summaries, audio_path
        )
        logger.info(
            f"Audio: {output_path} "
            f"({file_size/1024:.0f} KB, ~{duration}s)"
        )
    except Exception as e:
        logger.error(f"TTS generation failed: {e}")
        sys.exit(1)
    
    # ---- Step 4: 更新 RSS + Git Push ----
    logger.info("Step 4/4: Updating RSS and pushing to GitHub...")
    
    # 获取已有单集数，新单集序号 +1
    existing_count = get_episode_count_from_rss(config.RSS_FILE)
    episode_number = existing_count + 1
    
    # 构建单集元数据
    episode_meta = {
        "guid": f"ep_{episode_date}",
        "title": format_episode_title(episode_date, summaries),
        "description": build_html_description(summaries),
        "audio_url": f"{config.AUDIO_CDN_BASE}/audio/{audio_filename}",
        "audio_length": file_size,
        "audio_duration": format_duration(duration),
        "pub_date": format_pub_date(episode_date),
        "episode_number": episode_number,
    }
    
    try:
        create_or_update_rss(config.RSS_FILE, episode_meta)
        logger.info("RSS updated")
    except Exception as e:
        logger.error(f"RSS update failed: {e}")
        sys.exit(1)
    
    try:
        commit_and_push_episode(repo, audio_path, config.RSS_FILE, episode_date)
        logger.info("Git push successful")
    except Exception as e:
        logger.error(f"Git push failed: {e}")
        sys.exit(1)
    
    # ---- 完成 ----
    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("=" * 60)
    logger.info("Pipeline completed successfully!")
    logger.info(f"Elapsed: {elapsed:.1f}s")
    logger.info(f"RSS URL: {config.RSS_BASE_URL}/rss.xml")
    logger.info(f"Audio CDN: {config.AUDIO_CDN_BASE}/audio/{audio_filename}")
    logger.info("=" * 60)
    
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="大宗商品研究播客自动生成流水线"
    )
    parser.add_argument(
        "--date",
        help="覆盖日期 (YYYY-MM-DD)，默认今天",
    )
    parser.add_argument(
        "--summaries",
        help="指定摘要 JSON 文件路径",
    )
    args = parser.parse_args()
    
    exit_code = main(
        date_override=args.date,
        summaries_path=args.summaries,
    )
    sys.exit(exit_code)
