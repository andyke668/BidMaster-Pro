"""数据抓取器抽象层 (借鉴 AIMedia spider_all.py + article-gen RSS 哲学)

提供统一接口 BaseFetcher,支持:
- RSSFetcher: 抓取 RSS 订阅源 (最稳定)
- APIFetcher: 抓取 GitHub 等 API 接口
- HTMLFetcher: 抓取 HTML 页面 (预留扩展点)
- BrowserFetcher: 浏览器自动化 (二期实现)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import List, Optional
from datetime import datetime, timedelta
from urllib.parse import urljoin
import asyncio
import re
import requests
import feedparser
from bs4 import BeautifulSoup

# 注意：requests 是同步库。单 worker + asyncio 部署下，直接在协程里调用会把
# 整条事件循环阻塞住（聚合期间全站无响应，其它接口的轮询也会一起卡死），
# 上层 asyncio.Semaphore(4) 的并发也形同虚设。
# 因此本文件所有 requests.get 一律用 asyncio.to_thread 放进线程池执行。

# 政务/招标站点普遍对非浏览器 UA 返回空页或 403，列表页抓取统一用浏览器 UA。
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _normalize_pub_date(raw: str) -> str:
    """把抓到的日期串归一成 ISO 格式。

    scoring._freshness 用 datetime.fromisoformat 解析 pub_date，
    而各站点写法五花八门（2026年09月07日 / 2026/9/7 / 2026.09.07），
    统一转换后才能正确算出时效性得分。
    """
    if not raw:
        return ""
    s = raw.strip()
    for a, b in (("年", "-"), ("月", "-"), ("日", ""), ("/", "-"), (".", "-")):
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s).strip().rstrip("-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return raw.strip()


@dataclass
class NewsItem:
    """统一抓取结果"""
    title: str = ""
    url: str = ""
    source: str = ""
    pub_date: str = ""
    content: str = ""
    source_code: str = ""
    industry_code: str = ""
    # 以下字段是 scoring.BusinessValueScorer 与 HotspotItem 落库真正消费的：
    # region 占评分 15%、amount 占 20%，owner_org/project_code 参与去重指纹。
    # 之前 dataclass 里没有它们，抓取器即使拿到也传不到下游，导致地域分恒为 0。
    region: str = ""
    owner_org: str = ""
    project_code: str = ""
    bid_deadline: str = ""
    amount: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v or k == "title"}


class FetchError(Exception):
    """抓取失败"""
    pass


class BaseFetcher(ABC):
    """抓取器基类"""
    @abstractmethod
    async def fetch(self, source_config: dict) -> List[NewsItem]:
        """根据源配置抓取,返回 NewsItem 列表"""
        raise NotImplementedError


class RSSFetcher(BaseFetcher):
    """RSS 抓取器 (借鉴 article-generation-skill)

    - 第一步:抓取 RSS feed 获取 title/url/pub_date/summary
    - 第二步 (可选):抓取详情页正文,丰富 content 字段
    """

    def __init__(self, max_age_hours: int = 168, max_items: int = 30, fetch_detail: bool = True):
        self.max_age_hours = max_age_hours
        self.max_items = max_items
        self.fetch_detail = fetch_detail

    async def fetch(self, source_config: dict) -> List[NewsItem]:
        url = source_config.get("url", "")
        if not url:
            return []

        try:
            resp = await asyncio.to_thread(
                requests.get,
                url,
                timeout=15,
                headers={"User-Agent": "BidMaster-Pro/1.0 (Tender Monitor)"},
            )
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            raise FetchError(f"RSS 抓取超时: {url}")
        except requests.exceptions.RequestException as e:
            raise FetchError(f"RSS 抓取失败: {e}")

        feed = feedparser.parse(resp.content)
        items: List[NewsItem] = []
        cutoff = datetime.now() - timedelta(hours=self.max_age_hours)

        for entry in feed.entries[: self.max_items]:
            published = entry.get("published_parsed")
            if published:
                pub_time = datetime(*published[:6])
            else:
                pub_time = datetime.now()

            if pub_time < cutoff:
                continue

            entry_url = entry.get("link", "").strip()
            summary = (entry.get("summary", "") or "")[:500]

            # 第一步:基础信息
            item = NewsItem(
                title=entry.get("title", "").strip(),
                url=entry_url,
                source=source_config.get("name", ""),
                pub_date=pub_time.isoformat(),
                content=summary,
                source_code=source_config.get("code", ""),
                industry_code=source_config.get("industry", ""),
                extra={"fetch_type": "rss"},
            )

            # 第二步:抓取详情页正文(借鉴 AIMedia spider_all.py)
            if self.fetch_detail and entry_url:
                detail_content = await self._fetch_detail_content(entry_url, summary)
                if detail_content and len(detail_content) > len(summary):
                    item.content = detail_content
                    item.extra["detail_fetched"] = True
                    item.extra["content_length"] = len(detail_content)

            items.append(item)

        return items

    async def _fetch_detail_content(self, url: str, fallback: str) -> str:
        """抓取详情页正文(借鉴 NewsCrawlerSkill._extract_content)

        优先尝试 article / content / news 正文容器,
        失败时回退到 RSS summary。
        """
        try:
            resp = await asyncio.to_thread(
                requests.get,
                url,
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return fallback

        # 借鉴 NewsCrawlerSkill 的 EXTRACT_PATTERNS
        extract_patterns = [
            r'<article[^>]*>(.*?)</article>',
            r'<div[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*article[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*id="[^"]*content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*detail[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*news[^"]*"[^>]*>(.*?)</div>',
        ]
        re_tag = re.compile(r'<[^>]+>')
        re_ws = re.compile(r'\s+')

        for pattern in extract_patterns:
            try:
                matches = re.findall(pattern, html, re.DOTALL | re.IGNORECASE)
            except re.error:
                continue
            if matches:
                longest = max(matches, key=len)
                text = re_tag.sub('', longest)
                text = re_ws.sub(' ', text).strip()
                if len(text) > 200:
                    return text[:8000]

        # 兜底:RSS summary
        return fallback


class APIFetcher(BaseFetcher):
    """API 抓取器 (GitHub Search API 等)"""

    async def fetch(self, source_config: dict) -> List[NewsItem]:
        code = source_config.get("code", "")
        if code == "github_tender":
            return await self._fetch_github_trending(source_config)
        return []

    async def _fetch_github_trending(self, config: dict) -> List[NewsItem]:
        cfg = config.get("config") or config.get("extra_config") or {}
        languages = " ".join(
            f"language:{lang}" for lang in cfg.get("languages", ["python"])
        )
        topics = " ".join(f"topic:{t}" for t in cfg.get("topics", []))
        query = f"{languages} {topics} stars:>={cfg.get('min_stars', 5)}"

        try:
            resp = await asyncio.to_thread(
                requests.get,
                config.get("url", "https://api.github.com/search/repositories"),
                params={"q": query, "sort": "stars", "per_page": cfg.get("max_results", 20)},
                timeout=30,
                headers={"Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
        except Exception as e:
            raise FetchError(f"GitHub API 抓取失败: {e}")

        data = resp.json()
        items: List[NewsItem] = []
        cutoff = datetime.now() - timedelta(hours=168)

        # 排除自身仓库 (避免 GitHub 自我命中)
        # 读取 source_config 顶层 exclude_repos 或 cfg.exclude_repos
        exclude_repos = (
            config.get("exclude_repos")
            or cfg.get("exclude_repos")
            or ["bidmaster-pro", "BidMaster-Pro", "bidmaster_pro"]  # 默认排除本项目
        )
        # 排除关键字 (title/url 中包含则过滤)
        exclude_keywords = (
            config.get("exclude_keywords")
            or cfg.get("exclude_keywords")
            or ["bidmaster"]
        )

        for repo in data.get("items", []):
            try:
                updated = datetime.strptime(repo["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                updated = datetime.now()
            if updated < cutoff:
                continue

            full_name = (repo.get("full_name") or "").lower()
            html_url = (repo.get("html_url") or "").lower()
            name = (repo.get("name") or "").lower()
            desc = (repo.get("description") or "").lower()

            # 过滤: 仓库名命中排除列表
            if any(ex.lower() in full_name or ex.lower() in name for ex in exclude_repos):
                continue
            # 过滤: 关键字命中
            if any(kw.lower() in name or kw.lower() in html_url or kw.lower() in desc for kw in exclude_keywords):
                continue

            items.append(
                NewsItem(
                    title=repo["name"],
                    url=repo["html_url"],
                    source="GitHub Trending",
                    pub_date=updated.isoformat(),
                    content=(repo.get("description") or "")[:500],
                    source_code=config.get("code", ""),
                    industry_code="12",
                    extra={
                        "fetch_type": "api",
                        "stars": repo.get("stargazers_count", 0),
                        "language": repo.get("language", ""),
                    },
                )
            )
        return items


class HTMLFetcher(BaseFetcher):
    """HTML 列表页抓取器（配置驱动）

    国内招标采购站点绝大多数没有 RSS，只提供列表页。抓取规则全部写在
    sources.yaml 的 config 里，本类只做通用执行——新增站点不用改代码：

      item_selector   列表项 CSS 选择器（必填），如 "ul.c_list_bid > li"；
                      soupsieve 支持 :has()，可用 "li:has(div.xxx)" 定位
      link_selector   条目内链接选择器，默认 "a"
      base_url        相对链接的基准 URL，默认取源自身的 url
      encoding        强制编码；缺省时若服务端未声明 charset（requests 会退到
                      iso-8859-1，中文必乱码）则用 apparent_encoding 探测
      date_pattern    含 1 个捕获组的正则，从条目文本提取发布时间
      field_patterns  {字段名: 正则}，从条目文本提取 region / owner_org /
                      project_code / bid_deadline，直接喂给评分与落库
      max_items       单源最多条目数，默认 30
      min_title_len   标题最小长度，用于滤掉「更多」「首页」等导航短链接，默认 10

    只抓列表页、不追详情页：招标列表的标题+地域+采购人已足够评分与去重，
    而逐条追详情会让单源多出几十次请求，把整个聚合拖到分钟级以上。
    """

    DEFAULT_MAX_ITEMS = 30
    DEFAULT_MIN_TITLE_LEN = 10
    SKIP_HREF_PREFIXES = ("javascript:", "#", "mailto:", "tel:")

    def __init__(self, max_age_hours: int = 168):
        self.max_age_hours = max_age_hours

    async def fetch(self, source_config: dict) -> List[NewsItem]:
        url = source_config.get("url", "")
        if not url:
            return []

        cfg = source_config.get("config") or source_config.get("extra_config") or {}
        item_selector = cfg.get("item_selector")
        if not item_selector:
            raise FetchError(f"HTML 源缺少 config.item_selector，无法解析: {url}")

        try:
            resp = await asyncio.to_thread(
                requests.get,
                url,
                timeout=20,
                headers={"User-Agent": _BROWSER_UA},
            )
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            raise FetchError(f"HTML 抓取超时: {url}")
        except requests.exceptions.RequestException as e:
            raise FetchError(f"HTML 抓取失败: {e}")

        encoding = cfg.get("encoding")
        if encoding:
            resp.encoding = encoding
        elif not resp.encoding or resp.encoding.lower().startswith("iso-8859"):
            resp.encoding = resp.apparent_encoding

        html = resp.text

        def _select() -> list:
            return BeautifulSoup(html, "html.parser").select(item_selector)

        try:
            # 解析大页面是 CPU 活，同样丢线程池，别堵事件循环
            nodes = await asyncio.to_thread(_select)
        except Exception as e:
            raise FetchError(f"HTML 解析失败 (item_selector={item_selector}): {e}")

        base_url = cfg.get("base_url") or url
        link_selector = cfg.get("link_selector", "a")
        date_pattern = cfg.get("date_pattern")
        max_items = int(cfg.get("max_items", self.DEFAULT_MAX_ITEMS))
        min_title_len = int(cfg.get("min_title_len", self.DEFAULT_MIN_TITLE_LEN))
        cutoff = datetime.now() - timedelta(hours=self.max_age_hours)

        compiled_fields: dict = {}
        for fname, pattern in (cfg.get("field_patterns") or {}).items():
            try:
                compiled_fields[fname] = re.compile(pattern)
            except re.error as e:
                raise FetchError(f"config.field_patterns.{fname} 正则非法: {e}")

        if date_pattern:
            try:
                date_re = re.compile(date_pattern)
            except re.error as e:
                raise FetchError(f"config.date_pattern 正则非法: {e}")
        else:
            date_re = None

        items: List[NewsItem] = []
        for node in nodes:
            if len(items) >= max_items:
                break

            link = node.select_one(link_selector)
            if not link or not link.get("href"):
                continue
            # 列表页 <a> 的可见文本常被 CSS 截断，title 属性才是完整标题
            title = (link.get("title") or link.get_text(strip=True) or "").strip()
            if len(title) < min_title_len:
                continue
            href = link["href"].strip()
            if href.lower().startswith(self.SKIP_HREF_PREFIXES):
                continue

            text = node.get_text(" ", strip=True)

            pub_date = ""
            if date_re:
                m = date_re.search(text)
                if m:
                    pub_date = _normalize_pub_date(m.group(1))
            if pub_date:
                try:
                    if datetime.fromisoformat(pub_date) < cutoff:
                        continue
                except ValueError:
                    pass

            extracted: dict = {}
            for fname, pattern in compiled_fields.items():
                m = pattern.search(text)
                if m:
                    value = m.group(1).strip()
                    if value:
                        extracted[fname] = value

            items.append(
                NewsItem(
                    title=title,
                    url=urljoin(base_url, href),
                    source=source_config.get("name", ""),
                    pub_date=pub_date,
                    content=text[:500],
                    source_code=source_config.get("code", ""),
                    industry_code=source_config.get("industry", ""),
                    region=extracted.pop("region", ""),
                    owner_org=extracted.pop("owner_org", ""),
                    project_code=extracted.pop("project_code", ""),
                    bid_deadline=extracted.pop("bid_deadline", ""),
                    extra={"fetch_type": "html", **extracted},
                )
            )
        return items


class BrowserFetcher(BaseFetcher):
    """浏览器自动化抓取器 (二期实现)"""

    async def fetch(self, source_config: dict) -> List[NewsItem]:
        return []


FETCHER_REGISTRY = {
    "rss": RSSFetcher,
    "api": APIFetcher,
    "html": HTMLFetcher,
    "crawl": HTMLFetcher,  # 同 HTML
    "browser": BrowserFetcher,
}


def get_fetcher(source_type: str) -> BaseFetcher:
    fetcher_cls = FETCHER_REGISTRY.get(source_type)
    if not fetcher_cls:
        raise FetchError(f"不支持的抓取类型: {source_type}")
    return fetcher_cls()
