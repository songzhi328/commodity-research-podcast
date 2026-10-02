#!/usr/bin/env python3
"""
Commodity Research API Researcher
==================================
用 API Key 方案替代 claude -p：
  Step A: 东方财富研报中心 API（主源，条条真研报）→ 5 板块候选；
          某板块候选不足时用 DuckDuckGo 搜索补充（硬过滤只留真研报；
          2026-10-03 起豆包搜索 key 失效，备用源改为 ddgs 库，Bing 兜底）
  Step B1: 逐板块 MiMo 精选（每板块 1 次小调用，失败降级为规则精选）
  Step B2: 逐篇 MiMo 摘要（每篇 1 次小调用，失败重试，不足 800 字触发扩写）
  Step C: 代码拼装 full_script / sections 并保存

架构说明：glm-5.3-flash 是推理模型且 thinking 无法关闭（实测 ark 返回
InvalidParameter），单次大 prompt 大输出必然撞死在超时上。因此把原
"一次调用完成筛选+10篇摘要+脚本"拆成 15 次独立小调用，单次调用
几十秒即可完成，单篇失败只影响自身并自动重试。
2026-10-01 起 LLM 从火山方舟 glm-5.3-flash 切换为 OpenCode Go
mimo-v2.6-flash（推理模型，reasoning_content 跳过逻辑不变）。
"""

import os
import re
import sys
import json
import time
import httpx
from datetime import datetime, timedelta
from pathlib import Path

# ============================================================
# 常量
# ============================================================
PROJECT_DIR = "/DouDouBa/WorkBuddy/commodity-podcast"
DATA_DIR = os.path.join(PROJECT_DIR, "data")
ARCHIVE_DIR = os.path.join(DATA_DIR, "reports_archive")

# LLM：OpenCode Go 网关（chat/completions 认 Bearer + 强制 x-opencode-session 头）
LLM_API_URL = "https://opencode.ai/zen/go/v1/chat/completions"
LLM_MODEL = "mimo-v2.6-flash"
LLM_SESSION_ID = "commodity-podcast"

# ── 东方财富研报中心（主源，2026-09-30 起）──
# 豆包搜索返回的多是新闻（今日头条/新浪财经等），真研报占比极低（2026-09-27 期 10 篇仅 2 篇）。
# 东财研报中心公开 API 保证候选都是真研报：qType=1 行业研报 + qType=2 策略报告（多资产/宏观商品周报多在此）。
# 列表 API 无摘要字段，报告摘要正文从详情页内嵌 var zwinfo JSON 的 notice_content 提取（两种 qType 通用）。
EM_REPORT_API = "https://reportapi.eastmoney.com/report/list"
EM_REPORT_PAGE = "https://data.eastmoney.com/report/zw_industry.jshtml?infocode={code}"
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/report/",
}
# 板块 → 东财匹配关键词（industryName 命中行业词，或标题命中品种词）。一篇研报可归入多个板块。
EM_CATEGORY_KEYWORDS = {
    "coal": {"industry": ["煤炭", "焦炭"], "title": ["煤炭", "动力煤", "焦煤", "焦炭", "煤价", "煤化工"]},
    "oil": {"industry": ["油气", "石油", "炼化"], "title": ["原油", "油价", "石油", "油气", "布伦特", "成品油", "炼化"]},
    "nonferrous": {"industry": ["工业金属", "能源金属", "小金属", "金属新材料"],
                   "title": ["有色金属", "工业金属", "能源金属", "小金属", "铜", "铝", "锌", "镍", "锂", "稀土"]},
    "agriculture": {"industry": ["农业", "种植", "养殖", "农产品", "饲料"],
                    "title": ["农林牧渔", "农业", "农产品", "大豆", "豆粕", "玉米", "棕榈", "油脂", "生猪", "猪价", "白糖", "棉花", "粮食"]},
    "precious": {"industry": ["贵金属"], "title": ["贵金属", "黄金", "白银", "金价"]},
}

# 豆包补充源硬过滤：真研报特征（站点白名单 或 非新闻站点且标题含研报特征词）
_REPORT_SITE_PAT = re.compile(r"hibor\.|迈博汇金|慧博|发现报告|fxbaogao|data\.eastmoney\.com/report", re.I)
_REPORT_TITLE_PAT = re.compile(r"研报|研究报告|行业周报|行业月报|行业点评|行业深度|行业跟踪|投资策略|专题报告|晨报")
_NEWS_SITE_PAT = re.compile(r"今日头条|百度|腾讯|网易|新浪|搜狐|雪球|知乎|微信公众号|澎湃|财联社|抖音|同花顺|东方财富|支付宝|百家号")

# 单次调用参数：小任务量 + 流式，避免非流式长连接超时
# ⚠️ glm-5.3-flash 是推理模型，max_tokens 是 thinking + 回答共享预算：
# 精选的正式输出仅两三百 token，但 thinking 可能消耗 2000+，
# 预算给小了会 content_len=0 / finish_reason=length（2026-09-21 实测），必须给足
CALL_MAX_TOKENS_SELECTION = 16000
CALL_MAX_TOKENS_SUMMARY = 16000
CALL_READ_TIMEOUT = 300  # 秒

# 精选兜底规则的权威机构加分（优先级从高到低）
AUTH_PRIORITY = ["中金", "紫金", "东方财富", "中信证券", "华泰", "国泰君安", "银河", "长江", "光大", "五矿", "海通"]

# 板块内品种多样性关键词：精选时尽量让 2 篇覆盖不同品种
VARIETY_KEYWORDS = {
    "coal": [["动力煤"], ["焦煤", "焦炭"]],
    "oil": [["原油", "布伦特", "WTI"], ["炼化", "成品油", "天然气"]],
    "nonferrous": [["铜"], ["铝"], ["锌", "镍"]],
    "agriculture": [["大豆", "豆粕", "油脂"], ["玉米"], ["棕榈"]],
    "precious": [["黄金"], ["白银"]],
}

CATEGORIES = {
    "coal": {
        "header": "煤炭板块",
        # queries 仅作为东财候选不足时的豆包搜索补充词，锚定研报站（迈博汇金/慧博/发现报告）
        "queries": [
            "迈博汇金 煤炭行业 研究报告",
            "慧博 hibor 煤炭 行业周报",
            "发现报告 动力煤 焦煤 研报",
        ],
    },
    "oil": {
        "header": "石油原油板块",
        "queries": [
            "迈博汇金 石油化工 原油 研究报告",
            "慧博 hibor 油气 行业周报",
            "发现报告 原油 石油 研报",
        ],
    },
    "nonferrous": {
        "header": "有色金属板块",
        "queries": [
            "迈博汇金 有色金属 研究报告",
            "慧博 hibor 有色金属 行业周报 铜 铝",
            "发现报告 铜 铝 锌 研报",
        ],
    },
    "agriculture": {
        "header": "农产品板块",
        "queries": [
            "迈博汇金 农林牧渔 研究报告",
            "慧博 hibor 农业 行业周报",
            "发现报告 大豆 玉米 棕榈油 研报",
        ],
    },
    "precious": {
        "header": "贵金属板块",
        "queries": [
            "迈博汇金 贵金属 黄金 研究报告",
            "慧博 hibor 黄金 白银 研报",
            "发现报告 贵金属 黄金 研报",
        ],
    },
}


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ============================================================
# Step A: DuckDuckGo 搜索（ddgs 库，豆包搜索 2026-10-03 起 key 失效后的替代；
#         Bing HTML 抓取作兜底——实测其 site:/引号 在本机被降级，仅救急）
# ============================================================

BING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _html_to_text(html_str):
    html_str = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_str or "")
    html_str = re.sub(r"<[^>]+>", " ", html_str)
    return re.sub(r"\s+", " ", html_str).strip()


def _cn_date_iso(text):
    """从摘要/标题提取中文日期 → 'YYYY-MM-DD'（ISO，fromisoformat 可解析）。"""
    if not text:
        return ""
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        year = datetime.now().year
        try:
            d = datetime(year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return ""
        if d - datetime.now() > timedelta(days=7):      # 12月读到1月 → 去年
            d = d.replace(year=year - 1)
        return d.strftime("%Y-%m-%d")
    return ""


def _search_web_bing(query, count=5):
    """Bing HTML 抓取（兜底）。实测 cn.bing.com 对 server 请求降级：
    ex1/ez5 过滤参数、site:、引号短语都会被打乱返回泛化结果。"""
    try:
        r = httpx.get(
            "https://cn.bing.com/search",
            params={"q": query, "setmkt": "zh-CN"},
            headers={"User-Agent": BING_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=25, follow_redirects=True)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        log(f"  Bing search failed: {e}")
        return []

    from urllib.parse import urlparse
    results, seen = [], set()
    for m in re.finditer(r'<h2[^>]*>\s*<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                         html, re.S):
        link, title_html = m.group(1), m.group(2)
        if "bing.com" in link or link in seen:
            continue
        seen.add(link)
        title = _html_to_text(title_html)
        if not title:
            continue
        tail = html[m.end():m.end() + 1500]
        sm = re.search(r"<p[^>]*>(.*?)</p>", tail, re.S)
        summary = _html_to_text(sm.group(1)) if sm else ""
        results.append({
            "title": title,
            "url": link,
            "site": urlparse(link).netloc.replace("www.", ""),
            "auth": "",
            "summary": summary,
            "publish_time": _cn_date_iso(summary) or _cn_date_iso(title),
        })
        if len(results) >= count:
            break
    return results


def search_web(query, count=5, time_range="OneWeek"):
    """DuckDuckGo 搜索（ddgs 库，与 vibe-trading web_search 同源同版本 9.14.4），
    返回与原豆包同字段结构的结果列表；失败时降级 Bing HTML 抓取。

    注意：系统 pip 需 --break-system-packages 安装，且**必须钉 9.14.4**
    （9.16.0 的 backend 路由会打到 startpage/yahoo，本机全部超时）。
    """
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as d:
            raw = list(d.text(query, max_results=count))
        results = []
        for r in raw:
            url = r.get("href") or ""
            if not url:
                continue
            from urllib.parse import urlparse
            results.append({
                "title": r.get("title") or "",
                "url": url,
                "site": urlparse(url).netloc.replace("www.", ""),
                "auth": "",
                "summary": r.get("body") or "",
                "publish_time": (r.get("date") or "")[:10],
            })
        if results:
            return results
        log("  DDGS returned empty, falling back to Bing")
    except Exception as e:
        log(f"  DDGS search failed: {e}, falling back to Bing")
    return _search_web_bing(query, count)


def _parse_publish_time(s):
    """解析搜索结果的 PublishTime（ISO 8601，如 2026-09-07T00:00:00+08:00），转本地 naive 时间。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


# ============================================================
# Step A: 东方财富研报中心（主源）+ 豆包搜索（补充）
# ============================================================

def _fetch_em_reports(days=8):
    """东方财富研报中心：近 N 天行业研报(qType=1) + 策略报告(qType=2)，按 infoCode 去重。失败返回空列表。"""
    end = datetime.now().strftime("%Y-%m-%d")
    begin = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    items = []
    for qtype in (1, 2):
        for page in range(1, 6):
            try:
                r = httpx.get(EM_REPORT_API, params={
                    "pageNo": page, "pageSize": 100, "qType": qtype,
                    "beginTime": begin, "endTime": end,
                    "industry": "*", "industryCode": "", "rating": "", "ratingChange": "",
                }, headers=EM_HEADERS, timeout=20)
                r.raise_for_status()
                batch = r.json().get("data") or []
            except Exception as e:
                log(f"  东财研报API请求失败(qType={qtype} p{page}): {e}")
                break
            for it in batch:
                it["_qtype"] = qtype
            items.extend(batch)
            if len(batch) < 100:
                break
            time.sleep(0.3)

    uniq, seen = [], set()
    for it in items:
        code = it.get("infoCode")
        if not code or code in seen:
            continue
        seen.add(code)
        uniq.append(it)
    if uniq:
        log(f"  东财研报中心近{days}天（行业+策略）: {len(uniq)} 篇")
    return uniq


def _match_em_category(cat_key, item):
    """东财研报按板块关键词归类：industryName 命中行业词，或标题命中品种词。"""
    kws = EM_CATEGORY_KEYWORDS.get(cat_key, {})
    industry = item.get("industryName") or ""
    title = item.get("title") or ""
    if any(k in industry for k in kws.get("industry", [])):
        return True
    return any(k in title for k in kws.get("title", []))


def _em_to_candidate(item):
    """东财研报条目 → 候选 dict（与豆包搜索结果字段对齐）。summary 在精选前统一拉取。"""
    return {
        "title": (item.get("title") or "").strip(),
        "url": EM_REPORT_PAGE.format(code=item.get("infoCode", "")),
        "site": (item.get("orgSName") or "").strip(),
        "auth": (item.get("emRatingName") or "").strip(),
        "summary": "",
        "publish_time": (item.get("publishDate") or "")[:10],
        "info_code": item.get("infoCode", ""),
        "source_type": "em",
        "industry": (item.get("industryName") or "").strip(),
    }


def _fetch_report_abstract(info_code, cache):
    """从研报详情页内嵌 var zwinfo JSON 提取报告摘要正文（notice_content），带缓存。"""
    if not info_code:
        return ""
    if info_code in cache:
        return cache[info_code]
    abstract = ""
    try:
        r = httpx.get(EM_REPORT_PAGE.format(code=info_code), headers=EM_HEADERS, timeout=20)
        r.raise_for_status()
        h = r.text
        i = h.find("var zwinfo = ")
        if i >= 0:
            d, _ = json.JSONDecoder().raw_decode(h[i + len("var zwinfo = "):])
            abstract = (d.get("notice_content") or "").strip()
    except Exception as e:
        log(f"    东财研报摘要获取失败({info_code}): {e}")
    cache[info_code] = abstract
    return abstract


def _is_report_result(r):
    """豆包补充源硬过滤：只放行真研报（站点白名单命中，或非新闻站点且标题含研报特征词）。"""
    text = (r.get("title") or "") + " " + (r.get("url") or "")
    if _REPORT_SITE_PAT.search(text):
        return True
    if _NEWS_SITE_PAT.search(r.get("site") or ""):
        return False
    return bool(_REPORT_TITLE_PAT.search(r.get("title") or ""))


def search_all_categories():
    """获取 5 个板块的研报候选，返回 {category_key: [candidates]}。

    主源：东方财富研报中心（候选均为真研报，附报告摘要正文）。
    补充：某板块东财候选不足 3 条时用 Bing 网页搜索补充，硬过滤只留真研报；
    新闻类条目（今日头条/新浪财经等）一律丢弃。带发布日期且超 7 天的结果被丢弃。
    """
    now = datetime.now()
    em_items = _fetch_em_reports(days=8)
    all_results = {}
    dropped_stale = 0
    abstract_cache = {}

    for cat_key, cat_info in CATEGORIES.items():
        header = cat_info["header"]
        candidates = [_em_to_candidate(it) for it in em_items if _match_em_category(cat_key, it)]

        if len(candidates) < 3:
            seen_titles = {c["title"] for c in candidates}
            seen_urls = {c["url"] for c in candidates}
            n_supp = 0
            for query in cat_info["queries"]:
                for r in search_web(query, count=5, time_range="OneWeek"):
                    url = r.get("url", "")
                    if not url or url in seen_urls or r.get("title") in seen_titles:
                        continue
                    pt = _parse_publish_time(r.get("publish_time", ""))
                    if pt is not None and (now - pt).total_seconds() / 86400 > 7:
                        dropped_stale += 1
                        continue  # 丢弃发布超过 7 天的陈旧结果
                    if not _is_report_result(r):
                        continue  # 硬过滤：新闻一律不要
                    r["source_type"] = "bing"
                    seen_urls.add(url)
                    seen_titles.add(r.get("title", ""))
                    candidates.append(r)
                    n_supp += 1
                time.sleep(0.5)  # 避免触发限流
            if n_supp:
                log(f"  {header}: 东财候选不足，DDGS 搜索补充 {n_supp} 条真研报")

        # 最新在前；东财候选拉取报告摘要正文（精选与摘要共用的素材）
        candidates.sort(key=lambda c: c.get("publish_time") or "", reverse=True)
        for c in candidates[:12]:
            if c.get("source_type") == "em":
                c["summary"] = _fetch_report_abstract(c.get("info_code", ""), abstract_cache)
        candidates = candidates[:12]

        all_results[cat_key] = candidates
        log(f"    {header}: {len(candidates)} 条候选")

    if dropped_stale:
        log(f"  Filtered out {dropped_stale} stale results (>7 days)")
    return all_results


# ============================================================
# MiMo 流式调用（所有 LLM 调用的统一出口）
# ============================================================

def call_glm(prompt, api_key, max_tokens, tag=""):
    """流式调用 OpenCode Go mimo-v2.6-flash，返回 content 文本，失败返回 None。

    mimo-v2.6-flash 是推理模型，思考内容在 reasoning_content（跳过），
    最终回答在 content。Go 网关 chat/completions 认 Bearer，
    且强制要求 x-opencode-session 头。
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "x-opencode-session": LLM_SESSION_ID,
    }
    body = {
        "model": LLM_MODEL,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }

    label = f"[{tag}] " if tag else ""
    t0 = time.time()
    try:
        timeout = httpx.Timeout(30, read=CALL_READ_TIMEOUT, write=30, pool=30)
        with httpx.stream("POST", LLM_API_URL, json=body, headers=headers, timeout=timeout) as r:
            if r.status_code != 200:
                r.read()
                log(f"  {label}API error: HTTP {r.status_code}: {r.text[:300]}")
                return None

            content_parts = []
            finish_reason = None
            usage = {}
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                if choices[0].get("finish_reason"):
                    finish_reason = choices[0]["finish_reason"]
                if chunk.get("usage"):
                    usage = chunk["usage"]

        elapsed = time.time() - t0
        content = "".join(content_parts)
        log(f"  {label}OK: finish_reason={finish_reason}, "
            f"completion_tokens={usage.get('completion_tokens')}, "
            f"content_len={len(content)}, elapsed={elapsed:.0f}s")
        return content or None

    except httpx.TimeoutException:
        log(f"  {label}API timeout after {time.time()-t0:.0f}s")
        return None
    except Exception as e:
        log(f"  {label}API call failed: {e}")
        return None


def call_glm_with_retry(prompt, api_key, max_tokens, tag="", retries=1):
    """带重试的 LLM 调用。"""
    for attempt in range(1 + retries):
        if attempt > 0:
            log(f"  [{tag}] Retry {attempt}/{retries}...")
            time.sleep(5)
        content = call_glm(prompt, api_key, max_tokens, tag=tag)
        if content:
            return content
    return None


def extract_json(text):
    """从模型输出中提取 JSON 字符串（```json 块或首尾大括号）。"""
    match = re.search(r"```json\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return None


def _repair_unescaped_quotes(raw):
    """字符级修复 JSON 字符串值内未转义的 ASCII 双引号。

    glm-5.3-flash 即使 prompt 中明确要求用中文引号，仍会在 summary 正文中
    输出未转义的 " ，导致 json.loads 失败。规则：处于字符串内且后一个
    非空白字符不是 , : } ] 的 " 视为正文引号，转义处理。
    """
    result = []
    in_string = False
    escaped = False
    for i, ch in enumerate(raw):
        if escaped:
            result.append(ch)
            escaped = False
            continue
        if ch == "\\":
            result.append(ch)
            escaped = True
            continue
        if ch == '"':
            if in_string:
                rest = raw[i + 1:].lstrip()
                if rest and rest[0] in ",:}]":
                    in_string = False
                    result.append(ch)
                else:
                    result.append('\\"')
            else:
                in_string = True
                result.append(ch)
        else:
            result.append(ch)
    return "".join(result)


def parse_llm_json(text):
    """解析 LLM 输出的 JSON，先直接解析，失败走引号修复。修复失败返回 None。"""
    if not text:
        return None
    raw = extract_json(text)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_repair_unescaped_quotes(raw))
    except json.JSONDecodeError as e:
        log(f"  JSON parse failed even after repair: {e}")
        debug_file = os.path.join(DATA_DIR, f"debug_llm_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        try:
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(text)
            log(f"  Saved debug output to {debug_file}")
        except OSError:
            pass
        return None


# ============================================================
# Step B1: 逐板块精选
# ============================================================

def build_selection_prompt(cat_info, candidates):
    """构建单板块精选 prompt。candidates 为该板块候选结果列表（带序号）。"""
    lines = [f"以下是近一周{cat_info['header']}的研报候选（共 {len(candidates)} 条）：\n"]
    for c in candidates:
        lines.append(f"[{c['index']}] 标题: {c['title']}")
        lines.append(f"    发布时间: {c.get('publish_time') or '未知'}  |  来源机构: {c['site']}  |  行业: {c.get('industry') or '-'}")
        if c.get("summary"):
            lines.append(f"    内容摘要: {c['summary'][:500]}")
        lines.append("")

    lines.append(f"""请从中精选出最有价值的研报（至多 2 篇，宁缺毋滥），筛选标准按优先级排序：

1. 时效性：优先选择发布时间最新（越接近今天越好）的研报
2. 机构权威性：优先选择中金公司、紫金资产、东方财富等头部机构的研报
3. 品种覆盖多样性：2 篇研报应尽量覆盖{cat_info['header']}内不同的品种（如煤炭板块一篇动力煤、一篇焦煤焦炭），避免 2 篇都聚焦同一品种
4. 信息含量：优先选择包含具体价格数据、供需分析、库存指标的研报，而非纯观点文章

硬性要求：
- 新闻资讯、自媒体文章、活动宣传类条目一律不选
- 优质研报不足 2 篇时只选 1 篇，不要凑数

输出 JSON（用 ```json``` 包裹）：
{{
  "selected": [
    {{"index": 候选序号数字, "reason": "一句话理由"}},
    {{"index": 候选序号数字, "reason": "一句话理由"}}
  ]
}}

只输出 JSON，不要其他文字。""")
    return "\n".join(lines)


def _variety_group(cat_key, title):
    """返回标题命中的品种组序号（无命中返回 -1）。用于精选多样性判断。"""
    for gi, keywords in enumerate(VARIETY_KEYWORDS.get(cat_key, [])):
        if any(k in title for k in keywords):
            return gi
    return -1


def _heuristic_select(cat_key, candidates, pick=2):
    """精选降级兜底：权威机构加分 + 时效加分 + 品种多样性。返回选中的候选列表。"""
    def score(c):
        s = 0
        text = c["title"] + " " + (c.get("auth") or "") + " " + (c.get("site") or "")
        for i, org in enumerate(AUTH_PRIORITY):
            if org in text:
                s += max(1, len(AUTH_PRIORITY) - i)
                break
        pt = _parse_publish_time(c.get("publish_time", ""))
        if pt is not None:
            days_ago = (datetime.now() - pt).total_seconds() / 86400
            if days_ago < 2:
                s += 3
            elif days_ago < 4:
                s += 2
            elif days_ago <= 7:
                s += 1
        if c.get("auth"):
            s += 1
        return s

    ranked = sorted(candidates, key=lambda c: score(c), reverse=True)
    picked = [ranked[0]]
    # 第二篇优先选不同品种组的，否则取次高分
    first_group = _variety_group(cat_key, ranked[0]["title"])
    second = next((c for c in ranked[1:] if _variety_group(cat_key, c["title"]) != first_group),
                  ranked[1] if len(ranked) > 1 else None)
    if second is not None:
        picked.append(second)
    return picked


def select_reports(cat_key, cat_info, search_results, api_key):
    """单板块精选 2 篇。LLM 精选失败时降级为规则精选。返回选中的候选列表。"""
    header = cat_info["header"]
    if not search_results:
        log(f"  [{cat_key}] 无候选结果，跳过")
        return []

    candidates = []
    for i, r in enumerate(search_results[:10], 1):  # 每板块最多取 10 条候选
        c = dict(r)
        c["index"] = i
        candidates.append(c)

    if len(candidates) <= 2:
        log(f"  [{header}] 候选仅 {len(candidates)} 篇，直接全部选用")
        return candidates

    prompt = build_selection_prompt(cat_info, candidates)
    content = call_glm_with_retry(prompt, api_key, CALL_MAX_TOKENS_SELECTION, tag=f"select:{cat_key}")

    picked = []
    if content:
        data = parse_llm_json(content)
        if data and isinstance(data.get("selected"), list):
            seen_idx = set()
            for item in data["selected"]:
                try:
                    idx = int(item.get("index"))
                except (TypeError, ValueError):
                    continue
                if 1 <= idx <= len(candidates) and idx not in seen_idx:
                    seen_idx.add(idx)
                    picked.append(candidates[idx - 1])
                if len(picked) >= 2:
                    break

    if len(picked) < 2:
        if picked:
            log(f"  [{header}] LLM 精选只返回 {len(picked)} 篇，规则补足")
        else:
            log(f"  [{header}] LLM 精选失败或输出非法，使用规则精选兜底")
        heuristic = _heuristic_select(cat_key, candidates)
        for h in heuristic:
            if h not in picked:
                picked.append(h)
            if len(picked) >= 2:
                break

    for p in picked:
        log(f"    ✓ [{p['index']}] {p['title'][:45]}")
    return picked


# ============================================================
# Step B2: 逐篇摘要
# ============================================================

def build_summary_prompt(cat_info, report):
    """构建单篇研报摘要 prompt。"""
    return f"""你是大宗商品研报编辑。请基于以下材料撰写研报摘要。

板块：{cat_info['header']}
标题：{report['title']}
来源机构：{report['site']}
发布时间：{report.get('publish_time') or '未知'}
材料内容：
{report['summary'][:1500]}

请输出 JSON（用 ```json``` 包裹）：
{{
  "detailed_summary": "800-1000字的中文详细摘要",
  "short_summary": "100-200字的中文简短摘要"
}}

详细摘要必须涵盖（材料中有则写，没有的方面可省略）：
- 核心观点/评级
- 具体价格数据和变动幅度
- 供需分析要点
- 库存/产能/开工率等关键指标
- 机构推荐的标的和理由
- 短期展望和风险提示

⚠️ 材料内容有限时，可以基于行业公开常识做定性补充，但禁止编造材料中不存在的具体数字；
无法量化的内容用定性描述。JSON 字符串内不得使用未转义的 ASCII 双引号，请用中文引号（""）。
只输出 JSON，不要其他文字。"""


def _expand_summary(cat_info, report, current, api_key):
    """摘要不足 800 字时的扩写调用。"""
    prompt = f"""以下是研报《{report['title']}》（{cat_info['header']}，来源：{report['site']}）的摘要初稿：

{current}

该初稿不足 800 字。请在保持原有事实和数字完全不变的前提下扩写到 800-1000 字，
补充供需背景、行业逻辑、板块影响等定性分析。

输出 JSON（用 ```json``` 包裹）：
{{"detailed_summary": "扩写后的 800-1000 字摘要"}}

JSON 字符串内不得使用未转义的 ASCII 双引号，请用中文引号（""）。只输出 JSON。"""
    content = call_glm(prompt, api_key, CALL_MAX_TOKENS_SUMMARY, tag=f"expand")
    if not content:
        return None
    data = parse_llm_json(content)
    if data and len(data.get("detailed_summary", "")) >= 800:
        return data["detailed_summary"]
    return None


def summarize_report(cat_key, cat_info, report, api_key):
    """单篇研报生成详细摘要 + 简短摘要。返回 dict，失败返回 None。"""
    prompt = build_summary_prompt(cat_info, report)
    content = call_glm_with_retry(prompt, api_key, CALL_MAX_TOKENS_SUMMARY, tag=f"summary:{cat_key}")
    data = parse_llm_json(content)
    if not data or not data.get("detailed_summary"):
        log(f"    ✗ [{cat_key}] {report['title'][:40]}... 摘要生成失败")
        return None

    detailed = data["detailed_summary"].strip()
    short = (data.get("short_summary") or "").strip()

    if len(detailed) < 800:
        log(f"    ⚠ [{cat_key}] 摘要仅 {len(detailed)} 字，触发扩写")
        expanded = _expand_summary(cat_info, report, detailed, api_key)
        if expanded:
            detailed = expanded
            log(f"    ✓ 扩写后 {len(detailed)} 字")
        else:
            log(f"    ⚠ 扩写失败，保留 {len(detailed)} 字版本")

    if not short:
        short = detailed[:200]

    log(f"    ✓ [{cat_key}] {report['title'][:40]}... 详细={len(detailed)}字 简版={len(short)}字")
    return {"detailed": detailed, "short": short}


# ============================================================
# Step C: 拼装与保存
# ============================================================

def assemble_full_script(data, date_str):
    """代码拼装播客脚本：开场白 + 各板块（标题/机构/日期/详细摘要全文）+ 结束语。

    代码拼装保证脚本中的摘要与 categories 中逐字一致，
    不再让 LLM 重复输出全部摘要（原方案输出量翻倍的元凶）。
    """
    parts = ["各位听众好，欢迎收听本周的大宗商品研究周报播客。我是豆豆爸。", ""]
    for cat_key, cat_data in data["categories"].items():
        reports = cat_data.get("reports", [])
        if not reports:
            continue
        parts.append(f"接下来是{cat_data['header']}。")
        for r in reports:
            parts.append(f"本期研报：《{r['title']}》，来源：{r['source']}，发布日期：{r['date']}。")
            parts.append(r["summary"])
            parts.append("")
    parts.append("以上就是本周的大宗商品研究摘要。感谢收听，我们下周再见。")
    return "\n".join(parts)


def audit_freshness(data, date_str):
    """校验研报发布日期是否在执行日前 7 天内，超期/缺失的打日志告警。"""
    reports = []
    for cat_key, cat_data in data.get("categories", {}).items():
        for r in cat_data.get("reports", []):
            reports.append((cat_key, r))

    run_date = datetime.strptime(date_str, "%Y-%m-%d")
    stale, nodate = [], 0
    for cat_key, r in reports:
        d = (r.get("date") or "").strip()
        try:
            age = (run_date - datetime.strptime(d, "%Y-%m-%d")).days
        except ValueError:
            nodate += 1
            continue
        if age > 7:
            stale.append(f"[{cat_key}] {r.get('title', '')[:40]} (date={d}, {age}天前)")

    log(f"  时效校验: 共 {len(reports)} 篇, {len(reports) - len(stale) - nodate} 篇在 7 天内, "
        f"{len(stale)} 篇超期, {nodate} 篇缺/坏日期")
    for s in stale:
        log(f"    ⚠ 超期: {s}")
    if nodate:
        log(f"    ⚠ {nodate} 篇研报 date 字段缺失或格式非法")


def _normalize_date(publish_time):
    """把搜索结果的 ISO 发布时间转成 YYYY-MM-DD，无法解析返回空串。"""
    pt = _parse_publish_time(publish_time or "")
    return pt.strftime("%Y-%m-%d") if pt else ""


def save_result(data, date_str):
    """保存 summaries JSON 和 archive JSON。数据由代码拼装，直接 dump 保证合法。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    summary_file = os.path.join(DATA_DIR, f"summaries_{date_str}.json")
    archive_file = os.path.join(ARCHIVE_DIR, f"reports_{date_str}.json")

    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"  Saved: {summary_file}")

    archive_data = {
        "date": data.get("date", date_str),
        "categories": data.get("categories", {}),
    }
    with open(archive_file, "w", encoding="utf-8") as f:
        json.dump(archive_data, f, ensure_ascii=False, indent=2)
    log(f"  Saved: {archive_file}")


# ============================================================
# 主入口
# ============================================================

def run(date_str=None):
    """执行完整流程：搜索 → 精选 → 摘要 → 拼装保存。返回 (success: bool, summary_file: str)。"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    summary_file = os.path.join(DATA_DIR, f"summaries_{date_str}.json")

    log("=" * 50)
    log("Commodity Research API Researcher - START")
    log(f"Date: {date_str}")
    log("=" * 50)

    # 获取 API Key：
    #   llm_key   —— LLM 精选/摘要走 OpenCode Go（OPENCODE_API_KEY）
    #   搜索主源=东财公开 API（无 key），备用=Bing（无 key）；豆包搜索已弃用
    llm_key = os.environ.get("OPENCODE_API_KEY", "")

    if not llm_key:
        log("ERROR: OPENCODE_API_KEY not set in environment")
        return False, summary_file

    log(f"OPENCODE_API_KEY: {llm_key[:12]}...")

    # ── Step A: 搜索 ──
    log("Step A: Fetching research reports via 东方财富研报中心（Bing搜索备用）...")
    # 整体重试：搜索 API 偶发瞬时故障（如 2026-09-20 全部查询返回空），
    # 全空时隔 60s 再搜一轮，共 3 次机会
    SEARCH_ROUNDS = 3
    search_results = {}
    total_results = 0
    for attempt in range(1, SEARCH_ROUNDS + 1):
        try:
            search_results = search_all_categories()
            total_results = sum(len(v) for v in search_results.values())
        except Exception as e:
            log(f"ERROR: Search round {attempt} failed: {e}")
            total_results = 0
        log(f"  Round {attempt}/{SEARCH_ROUNDS}: {total_results} search results across 5 categories")
        if total_results > 0:
            break
        if attempt < SEARCH_ROUNDS:
            log(f"  搜索结果为空，60s 后重试（{attempt}/{SEARCH_ROUNDS}）...")
            time.sleep(60)
    if total_results == 0:
        log("ERROR: No search results found after all retries")
        return False, summary_file

    # ── Step B1: 逐板块精选（每板块 1 次小调用，失败规则兜底）──
    log("Step B1: Selecting top-2 reports per category via MiMo...")
    selected = {}  # cat_key -> [candidate dicts]
    for cat_key, cat_info in CATEGORIES.items():
        selected[cat_key] = select_reports(cat_key, cat_info, search_results.get(cat_key, []), llm_key)

    total_selected = sum(len(v) for v in selected.values())
    log(f"  Selected {total_selected} reports total")
    if total_selected == 0:
        log("ERROR: No reports selected")
        return False, summary_file

    # ── Step B2: 逐篇摘要（每篇 1 次小调用，失败重试+扩写）──
    log("Step B2: Generating per-report summaries via MiMo...")
    categories_out = {}
    sections = []
    failed = []
    for cat_key, cat_info in CATEGORIES.items():
        cat_reports = []
        for report in selected.get(cat_key, []):
            res = summarize_report(cat_key, cat_info, report, llm_key)
            if res is None:
                failed.append(f"[{cat_key}] {report['title'][:40]}")
                continue
            entry = {
                "title": report["title"],
                "source": report["site"],
                "date": _normalize_date(report.get("publish_time")),
                "summary": res["detailed"],
            }
            cat_reports.append(entry)
            sections.append({
                "title": report["title"],
                "source_org": report["site"],
                "date": entry["date"],
                "summary": res["short"],
            })
        categories_out[cat_key] = {"header": cat_info["header"], "reports": cat_reports}

    if failed:
        log(f"  ⚠ {len(failed)} 篇摘要彻底失败（已跳过）:")
        for f_ in failed:
            log(f"    ✗ {f_}")
    total_done = sum(len(c["reports"]) for c in categories_out.values())
    if total_done == 0:
        log("ERROR: All summaries failed")
        return False, summary_file

    # ── Step C: 代码拼装并保存 ──
    log("Step C: Assembling and saving JSON...")
    data = {
        "date": date_str,
        "full_script": "",
        "categories": categories_out,
        "sections": sections,
    }
    data["full_script"] = assemble_full_script(data, date_str)

    try:
        save_result(data, date_str)
        log(f"  Reports: {total_done} total, script length: {len(data['full_script'])} chars")
        audit_freshness(data, date_str)
    except Exception as e:
        log(f"ERROR: JSON save failed: {e}")
        return False, summary_file

    log("=" * 50)
    log("SUCCESS - API Researcher complete")
    log(f"Summary file: {summary_file}")
    log("=" * 50)
    return True, summary_file


if __name__ == "__main__":
    success, _ = run()
    sys.exit(0 if success else 1)
