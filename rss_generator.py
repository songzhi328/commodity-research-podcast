"""
RSS Generator - 生成和维护播客 RSS Feed（RSS 2.0 + iTunes 命名空间）
"""

import logging
from datetime import datetime
from typing import Optional

from lxml import etree

from config import (
    PODCAST_TITLE, PODCAST_DESCRIPTION, PODCAST_AUTHOR,
    PODCAST_AUTHOR_EMAIL, PODCAST_LANGUAGE, PODCAST_CATEGORY,
    PODCAST_SUBCATEGORY, PODCAST_EXPLICIT, PODCAST_TYPE,
    PODCAST_IMAGE_URL, RSS_BASE_URL,
)

logger = logging.getLogger(__name__)

# XML 命名空间
NSMAP = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
}


def _qname(tag: str) -> str:
    """生成带命名空间的 etree QName"""
    if ":" in tag:
        prefix, local = tag.split(":", 1)
        ns = NSMAP.get(prefix, "")
        return f"{{{ns}}}{local}"
    return tag


def create_new_rss() -> etree.ElementTree:
    """
    创建全新的 RSS XML。
    
    Returns:
        lxml ElementTree 对象
    """
    root = etree.Element("rss", version="2.0", nsmap=NSMAP)
    channel = etree.SubElement(root, "channel")
    
    # 频道元数据
    etree.SubElement(channel, "title").text = PODCAST_TITLE
    etree.SubElement(channel, "link").text = RSS_BASE_URL + "/"
    etree.SubElement(channel, "description").text = PODCAST_DESCRIPTION
    etree.SubElement(channel, "language").text = PODCAST_LANGUAGE
    
    # iTunes 命名空间标签
    etree.SubElement(channel, _qname("itunes:author")).text = PODCAST_AUTHOR
    etree.SubElement(channel, _qname("itunes:summary")).text = PODCAST_DESCRIPTION
    
    # iTunes 分类
    cat1 = etree.SubElement(channel, _qname("itunes:category"), text=PODCAST_CATEGORY)
    etree.SubElement(cat1, _qname("itunes:category"), text=PODCAST_SUBCATEGORY)
    
    etree.SubElement(channel, _qname("itunes:explicit")).text = PODCAST_EXPLICIT
    etree.SubElement(channel, _qname("itunes:type")).text = PODCAST_TYPE
    
    if PODCAST_IMAGE_URL:
        etree.SubElement(channel, _qname("itunes:image"), href=PODCAST_IMAGE_URL)
    
    # iTunes owner
    owner = etree.SubElement(channel, _qname("itunes:owner"))
    etree.SubElement(owner, _qname("itunes:name")).text = PODCAST_AUTHOR
    etree.SubElement(owner, _qname("itunes:email")).text = PODCAST_AUTHOR_EMAIL
    
    # Atom self-link
    atom_link = etree.SubElement(channel, _qname("atom:link"))
    atom_link.set("href", f"{RSS_BASE_URL}/rss.xml")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")
    
    return etree.ElementTree(root)


def load_existing_rss(rss_path: str) -> Optional[etree.ElementTree]:
    """
    加载已有 RSS XML 文件。
    
    Args:
        rss_path: RSS XML 文件路径
        
    Returns:
        ElementTree 对象，文件不存在则返回 None
    """
    import os
    if not os.path.exists(rss_path):
        logger.info("No existing RSS file, will create new one")
        return None
    
    try:
        parser = etree.XMLParser(remove_blank_text=True)
        tree = etree.parse(rss_path, parser)
        logger.info(f"Loaded existing RSS: {rss_path}")
        return tree
    except Exception as e:
        logger.warning(f"Failed to parse existing RSS: {e}, creating new")
        return None


def _build_html_description(summaries: dict) -> str:
    """
    从摘要构建播客单集的 HTML 描述。
    
    Args:
        summaries: 包含 categories 的摘要字典
        
    Returns:
        HTML 格式的描述文本
    """
    parts = ["<p>本周大宗商品研究报告摘要，覆盖五大板块：</p>"]
    
    categories = summaries.get("categories", {})
    for cat_key in ["coal", "oil", "nonferrous", "agriculture", "precious"]:
        cat_data = categories.get(cat_key, {})
        if not cat_data:
            continue
        
        header = cat_data.get("header", cat_key)
        parts.append(f"<h3>{header}</h3>")
        
        reports = cat_data.get("reports", [])
        for report in reports:
            title = report.get("title", "研报")
            source = report.get("source", "")
            date = report.get("date", "")
            summary = report.get("summary", "")
            
            source_info = f" ({source}, {date})" if source else ""
            parts.append(f"<p><strong>{title}</strong>{source_info}</p>")
            parts.append(f"<p>{summary}</p>")
    
    parts.append("<p><em>本节目由AI自动生成，仅供参考，不构成投资建议。</em></p>")
    
    return "\n".join(parts)


def add_episode_item(
    rss_tree: etree.ElementTree,
    episode: dict,
) -> etree.ElementTree:
    """
    向 RSS feed 添加新单集。
    
    Args:
        rss_tree: 现有或新建的 RSS ElementTree
        episode: 单集数据字典，包含:
            - guid: 唯一标识符
            - title: 单集标题
            - description: HTML 描述
            - audio_url: 音频文件 CDN URL
            - audio_length: 音频文件大小(字节)
            - audio_duration: 时长字符串 "MM:SS" 或 "HH:MM:SS"
            - pub_date: RFC 2822 格式的发布日期
            - episode_number: 单集序号(可选)
        
    Returns:
        更新后的 ElementTree
    """
    root = rss_tree.getroot()
    channel = root.find("channel")
    
    if channel is None:
        raise ValueError("Invalid RSS: missing <channel> element")
    
    # 创建新 item
    item = etree.Element("item")
    
    # 标准 RSS 标签
    etree.SubElement(item, "title").text = episode["title"]
    etree.SubElement(item, "guid", isPermaLink="false").text = episode["guid"]
    etree.SubElement(item, "pubDate").text = episode["pub_date"]
    
    # 描述
    desc = etree.SubElement(item, "description")
    desc.text = etree.CDATA(episode["description"])
    
    encoded_desc = etree.SubElement(item, _qname("content:encoded"))
    encoded_desc.text = etree.CDATA(episode["description"])
    
    # 音频 enclosure
    enclosure = etree.SubElement(
        item, "enclosure",
        url=episode["audio_url"],
        length=str(episode["audio_length"]),
        type="audio/mpeg",
    )
    
    # iTunes 标签
    etree.SubElement(item, _qname("itunes:title")).text = episode["title"]
    # itunes:summary 应为纯文本，去除 HTML 标签
    import re
    plain_summary = re.sub(r'<[^>]+>', '', episode["description"])
    plain_summary = plain_summary.strip()[:4000]
    etree.SubElement(item, _qname("itunes:summary")).text = plain_summary
    etree.SubElement(item, _qname("itunes:duration")).text = episode["audio_duration"]
    etree.SubElement(item, _qname("itunes:explicit")).text = "no"
    
    if episode.get("episode_number"):
        etree.SubElement(
            item, _qname("itunes:episode")
        ).text = str(episode["episode_number"])
    
    # 插入到 channel 开头（最新单集在前）
    channel.insert(0, item)
    
    logger.info(f"Added episode to RSS: {episode['title']}")
    
    return rss_tree


def save_rss(rss_tree: etree.ElementTree, output_path: str) -> None:
    """
    将 RSS XML 写入文件，带格式化。
    
    Args:
        rss_tree: ElementTree 对象
        output_path: 输出文件路径
    """
    xml_str = etree.tostring(
        rss_tree,
        encoding="UTF-8",
        xml_declaration=True,
        pretty_print=True,
    )
    
    with open(output_path, "wb") as f:
        f.write(xml_str)
    
    logger.info(f"RSS saved to {output_path}")


def create_or_update_rss(
    rss_path: str,
    episode: dict,
) -> None:
    """
    一站式函数：加载或创建 RSS，添加单集，保存。
    
    Args:
        rss_path: RSS 文件路径
        episode: 单集数据字典
    """
    tree = load_existing_rss(rss_path)
    
    if tree is None:
        tree = create_new_rss()
        logger.info("Created new RSS feed")
    
    add_episode_item(tree, episode)
    save_rss(tree, rss_path)


def get_episode_count_from_rss(rss_path: str) -> int:
    """
    从 RSS 文件中统计已有单集数量。
    
    Args:
        rss_path: RSS 文件路径
        
    Returns:
        单集数
    """
    tree = load_existing_rss(rss_path)
    if tree is None:
        return 0
    
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        return 0
    
    items = channel.findall("item")
    return len(items)
