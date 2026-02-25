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


def fetch_super_duper_trends() -> list[TrendItem]:
    """
    RapidAPI Super Duper Trends で急上昇ワードを取得（要 RAPIDAPI_KEY）。
    https://rapidapi.com/chriswmccully/api/super-duper-trends
    取得したワードはRSS取り込み時にトレンド合致度で記事を優先するために使う。
    """
    try:
        from app.config import settings
        if not getattr(settings, "RAPIDAPI_KEY", "").strip():
            return []
        import httpx
        host = getattr(settings, "RAPIDAPI_SUPER_DUPER_HOST", "super-duper-trends.p.rapidapi.com")
        trends: list[TrendItem] = []
        for path in ("/trending/now", "/trending/hourly", "/v1/trending", "/trending"):
            try:
                url = "https://" + host + path
                resp = httpx.get(
                    url,
                    headers={
                        "X-RapidAPI-Key": settings.RAPIDAPI_KEY,
                        "X-RapidAPI-Host": host,
                    },
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                items = data if isinstance(data, list) else (
                    data.get("items") or data.get("data") or data.get("trends")
                    or data.get("hourly") or data.get("results") or []
                )
                if not isinstance(items, list):
                    continue
                for entry in items[:25]:
                    if isinstance(entry, str):
                        kw = entry.strip()
                    elif isinstance(entry, dict):
                        kw = (entry.get("keyword") or entry.get("title") or entry.get("query") or entry.get("name") or "").strip()
                    else:
                        continue
                    if not kw or len(kw) < 2:
                        continue
                    item_id = hashlib.md5(("sdt-" + kw).encode("utf-8")).hexdigest()[:16]
                    trends.append(TrendItem(id=item_id, keyword=kw, source="google"))
                break
            except Exception:
                continue
        return trends[:20]
    except Exception:
        return []


def fetch_trending_searches() -> list[TrendItem]:
    """Google検索急上昇＋RapidAPI Super Duper Trends（設定時）をマージして取得"""
    seen: set[str] = set()
    out: list[TrendItem] = []
    try:
        for item in fetch_google_trends():
            k = item.keyword.lower().strip()
            if k not in seen:
                seen.add(k)
                out.append(item)
    except Exception:
        pass
    try:
        for item in fetch_super_duper_trends():
            k = item.keyword.lower().strip()
            if k not in seen:
                seen.add(k)
                out.append(item)
    except Exception:
        pass
    return out[:25]
