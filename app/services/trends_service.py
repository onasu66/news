"""トレンド取得サービス（RSS取り込み時の精査に利用）

取得元: Google検索急上昇のみ（日本向け公式RSS）
  https://trends.google.com/trending/rss?geo=JP
"""
from typing import Optional
from dataclasses import dataclass
import hashlib


@dataclass
class TrendItem:
    """トレンド検索ワード"""
    id: str
    keyword: str
    source: str = "google"  # "google" or "twitter"
    traffic: Optional[str] = None
    article_title: Optional[str] = None
    article_link: Optional[str] = None
    image_url: Optional[str] = None


# Google公式トレンドRSS（pytrends非公式APIの代わり）
GOOGLE_TRENDS_RSS_URL = "https://trends.google.com/trending/rss?geo=JP"


def fetch_google_trends() -> list[TrendItem]:
    """Googleの検索急上昇を取得（公式RSSフィード使用）"""
    try:
        import feedparser
        import httpx

        resp = httpx.get(
            GOOGLE_TRENDS_RSS_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsSite/1.0)"},
            timeout=15.0,
        )
        resp.raise_for_status()
        # 明示的にUTF-8でデコード
        content = resp.content.decode("utf-8", errors="replace")
        feed = feedparser.parse(content)

        trends: list[TrendItem] = []
        for entry in feed.entries[:25]:
            title = entry.get("title", "").strip()
            if not title or _is_generic_trend_label(title):
                continue
            item_id = hashlib.md5(title.encode("utf-8")).hexdigest()[:16]
            trends.append(TrendItem(id=item_id, keyword=title, source="google"))
        return trends[:20]
    except Exception:
        return []


def _is_generic_trend_label(text: str) -> bool:
    """「トレンド1」等の汎用ラベルか判定"""
    t = text.strip()
    if len(t) < 2:
        return True
    import re
    return bool(re.match(r"^トレンド\s*\d+$", t))


def fetch_trending_searches() -> list[TrendItem]:
    """Google検索急上昇を取得（RSS取り込み時のトレンド精査に利用）"""
    try:
        return fetch_google_trends()
    except Exception:
        return []
