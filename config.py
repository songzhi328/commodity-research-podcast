"""
Commodity Research Podcast Pipeline - Configuration
所有配置常量集中管理
"""

# ============================================================
# GitHub 配置
# ============================================================
GITHUB_USER = "songzhi328"
GITHUB_REPO = "commodity-research-podcast"
GITHUB_REPO_URL = f"git@github.com:{GITHUB_USER}/{GITHUB_REPO}.git"

# ============================================================
# 本地路径
# ============================================================
import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = BASE_DIR  # 脚本就在仓库目录中
DATA_DIR = os.path.join(BASE_DIR, "data")
AUDIO_DIR = os.path.join(BASE_DIR, "audio")
RSS_FILE = os.path.join(BASE_DIR, "rss.xml")

# ============================================================
# edge-tts 配置
# ============================================================
TTS_VOICE = "zh-CN-XiaoxiaoNeural"   # 微软晓晓 - 女性自然中文语音
TTS_FALLBACK_VOICE = "zh-CN-YunxiNeural"  # 备用 - 男性中文语音
TTS_RATE = "+0%"                      # 正常语速

# ============================================================
# 播客元数据
# ============================================================
PODCAST_TITLE = "大宗商品研究周报播客"
PODCAST_DESCRIPTION = (
    "每周大宗商品研究报告摘要播客，覆盖煤炭、石油、有色金属、"
    "农产品、贵金属五大板块。由豆豆爸制作，AI技术自动生成。"
)
PODCAST_AUTHOR = "豆豆爸"
PODCAST_AUTHOR_EMAIL = "songzhi328@hotmail.com"
PODCAST_LANGUAGE = "zh-cn"
PODCAST_CATEGORY = "Business"
PODCAST_SUBCATEGORY = "Investing"
PODCAST_EXPLICIT = "no"
PODCAST_TYPE = "episodic"

# ============================================================
# RSS / CDN URL
# ============================================================
RSS_BASE_URL = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}"
AUDIO_CDN_BASE = f"https://cdn.jsdelivr.net/gh/{GITHUB_USER}/{GITHUB_REPO}@main"

# 封面图 URL (jsDelivr CDN)
PODCAST_IMAGE_URL = f"{AUDIO_CDN_BASE}/cover.jpg"

# ============================================================
# 大宗商品品类定义
# ============================================================
CATEGORIES = {
    "coal": {
        "header": "煤炭板块",
        "keywords": "动力煤、焦煤、焦炭",
    },
    "oil": {
        "header": "石油原油板块",
        "keywords": "布伦特、WTI、国内原油",
    },
    "nonferrous": {
        "header": "有色金属板块",
        "keywords": "铜、铝、锌、镍",
    },
    "agriculture": {
        "header": "农产品板块",
        "keywords": "大豆、玉米、棕榈油",
    },
    "precious": {
        "header": "贵金属板块",
        "keywords": "黄金、白银",
    },
}

# ============================================================
# 播客脚本模板
# ============================================================
EPISODE_INTRO = (
    "各位听众好，欢迎收听本周的大宗商品研究周报播客。"
    "我是豆豆爸。本期我们继续覆盖煤炭、石油、有色金属、"
    "农产品和贵金属五大板块的最新研究观点。\n\n"
)

EPISODE_OUTRO = (
    "\n以上就是本周的大宗商品研究摘要。"
    "感谢收听，我们下周再见。\n"
)
