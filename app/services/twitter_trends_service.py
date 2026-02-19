"""X(Twitter)トレンド取得 - Nitter経由でスクレイピング"""
from dataclasses import dataclass
import hashlib

# 稼働中のNitterインスタンス（順に試行）
NITTER_INSTANCES = [
    "https://nitter.privacyredirect.com",
    "https://nitter.catsarch.com",
    "https://nitter.tiekoetter.com",
]

# トレンドページのパス（インスタンスによって異なる場合あり）
TRENDS_PATHS = ["/explore/trends", "/i/trends"]


@dataclass
class TwitterTrendItem:
    """Twitterトレンドワード"""
    id: str
    keyword: str


def _is_valid_trend(text: str) -> bool:
    """トレンドとして有効な文字列か"""
    if not text or len(text) < 2 or len(text) > 80:
        return False
    # ナビゲーション等の単語を除外
    skip = {"トレンド", "検索", "ホーム", "通知", "メッセージ", "ブックマーク", "プロフィール", "もっと見る"}
    if text in skip:
        return False
    return True


def fetch_twitter_trends() -> list[TwitterTrendItem]:
    """NitterのトレンドページからX(Twitter)急上昇を取得"""
    try:
        import httpx
        from bs4 import BeautifulSoup

        trends: list[TwitterTrendItem] = []
        seen = set()

        for base_url in NITTER_INSTANCES:
            for path in TRENDS_PATHS:
                try:
                    url = base_url + path
                    with httpx.Client(timeout=12.0, follow_redirects=True) as client:
                        resp = client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; NewsSite/1.0)"})
                        resp.raise_for_status()

                    soup = BeautifulSoup(resp.text, "html.parser")

                    # トレンドへの検索リンク (hrefにq= または /search を含む)
                    for a in soup.find_all("a", href=True):
                        href = a.get("href", "")
                        if "/search" in href or "q=" in href:
                            text = a.get_text(strip=True)
                            keyword = text.lstrip("#").strip()
                            if _is_valid_trend(keyword) and keyword.lower() not in seen:
                                seen.add(keyword.lower())
                                item_id = hashlib.md5(keyword.encode()).hexdigest()[:16]
                                trends.append(TwitterTrendItem(id=item_id, keyword=keyword))

                    if len(trends) >= 5:
                        return trends[:15]
                except Exception:
                    continue

        return trends[:15] if trends else []
    except Exception:
        return []
