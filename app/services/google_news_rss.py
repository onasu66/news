"""Google News RSS からトレンドキーワードに合致する記事を並列収集する

キーワードごとに https://news.google.com/rss/search を叩いて記事リストを返す。
APIキー不要・無料。Google のランキング済み記事なので検索需要との連動が高い。
"""
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

_GOOGLE_NEWS_RSS_BASE = "https://news.google.com/rss/search"


def _is_blocked(url: str) -> bool:
    from app.services.paywall_domains import is_blocked_news_url

    return is_blocked_news_url(url)


def _resolve_redirect(url: str, timeout: float = 5.0) -> str:
    """Google News のラッパー URL を実際の記事 URL に解決する。"""
    from app.services.google_news_url import resolve_google_news_url

    return resolve_google_news_url(url, timeout=timeout)


def _parse_published(entry) -> str:
    try:
        if entry.get("published_parsed"):
            dt = datetime(*entry.published_parsed[:6])
            return dt.isoformat()
    except Exception:
        pass
    return datetime.now(JST).replace(tzinfo=None).isoformat()


def _fetch_for_keyword(keyword: str, max_items: int = 8) -> list[dict]:
    """1 キーワードに対して Google News RSS を取得し記事 dict のリストを返す。"""
    try:
        import feedparser, httpx
        url = (
            f"{_GOOGLE_NEWS_RSS_BASE}"
            f"?q={quote_plus(keyword)}&hl=ja&gl=JP&ceid=JP:ja"
        )
        resp = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsSite/1.0)"},
            timeout=15.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)

        results: list[dict] = []
        for entry in feed.entries[:max_items]:
            raw_link = (entry.get("link") or "").strip()
            title = (entry.get("title") or "").strip()
            if not raw_link or not title:
                continue

            # リダイレクト解決（Google News → 実際の記事URL）
            link = _resolve_redirect(raw_link)
            if _is_blocked(link):
                continue

            # ソース名
            source = ""
            src = entry.get("source") or {}
            if isinstance(src, dict):
                source = src.get("title", "")
            if not source:
                source = "Google News"

            item_id = "gn-" + hashlib.md5(link.encode()).hexdigest()[:14]
            results.append({
                "id": item_id,
                "title": title,
                "url": link,
                "source": source,
                "category": "国内",
                "published": _parse_published(entry),
                "keyword": keyword,
                "image_url": None,
            })
        return results
    except Exception as e:
        logger.debug("Google News RSS 失敗 (keyword=%s): %s", keyword, e)
        return []


def fetch_news_for_keywords(
    keywords: list[str],
    max_per_keyword: int = 6,
    max_total: int = 60,
) -> list[dict]:
    """
    複数キーワードを並列で Google News RSS 検索し、重複除去した記事リストを返す。

    keywords       : トレンドキーワードのリスト（Google Trends / last30days 由来）
    max_per_keyword: 1 キーワードあたりの最大取得件数
    max_total      : 返す記事の総上限
    """
    if not keywords:
        return []

    seen_urls: set[str] = set()
    all_results: list[dict] = []

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {
            ex.submit(_fetch_for_keyword, kw, max_per_keyword): kw
            for kw in keywords[:15]
        }
        for future in as_completed(futures):
            try:
                items = future.result()
                for item in items:
                    url = item["url"]
                    if url not in seen_urls:
                        seen_urls.add(url)
                        all_results.append(item)
            except Exception:
                pass

    logger.info(
        "Google News RSS: キーワード %d件 → 記事 %d件取得",
        min(len(keywords), 15),
        len(all_results),
    )
    return all_results[:max_total]
