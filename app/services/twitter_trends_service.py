"""X(Twitter)トレンド取得 - 認証済みCookie検索（last30days同梱のbird-search経由）優先、Nitterはフォールバック"""
from dataclasses import dataclass
import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# 稼働中のNitterインスタンス（順に試行）
NITTER_INSTANCES = [
    "https://nitter.privacyredirect.com",
    "https://nitter.catsarch.com",
    "https://nitter.tiekoetter.com",
]

# last30days スキル同梱のX検索CLI（AUTH_TOKEN/CT0でX(Twitter)に直接アクセスする）
_BIRD_SEARCH_PATH = (
    Path.home() / ".claude" / "skills" / "last30days" / "scripts" / "lib"
    / "vendor" / "bird-search" / "bird-search.mjs"
)

# Xの公式トレンドAPIは使わないため、エンゲージメント上位を拾うための検索クエリで代替する
_BUZZ_QUERIES = ["話題 lang:ja", "速報 lang:ja", "バズ lang:ja"]

# トレンドページのパス（インスタンスによって異なる場合あり）
TRENDS_PATHS = ["/explore/trends", "/i/trends"]


@dataclass
class TwitterTrendItem:
    """Twitterトレンドワード"""
    id: str
    keyword: str


@dataclass
class TrendingPostItem:
    """急上昇ワードに紐づく投稿"""
    keyword: str
    user: str
    text: str
    url: str = ""


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


def _has_x_credentials() -> bool:
    from app.config import settings  # noqa: F401  (.env 読み込みのため)
    return bool(os.environ.get("AUTH_TOKEN", "").strip() and os.environ.get("CT0", "").strip())


def _bird_search(query: str, count: int = 25) -> list[dict]:
    """bird-search.mjs（last30days同梱）でX検索を実行し、tweetオブジェクトのリストを返す。"""
    if not _BIRD_SEARCH_PATH.is_file():
        return []
    node = shutil.which("node")
    if not node:
        return []
    try:
        r = subprocess.run(
            [node, str(_BIRD_SEARCH_PATH), query, "--count", str(count), "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            cwd=str(_BIRD_SEARCH_PATH.parent),
            env=os.environ.copy(),
        )
        data = json.loads((r.stdout or "").strip() or "[]")
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug("bird-search 失敗 (%s): %s", query, e)
        return []


def fetch_trending_posts_via_x_auth(limit: int = 10, count_per_query: int = 25) -> list[TrendingPostItem]:
    """AUTH_TOKEN/CT0の認証済みセッションでX検索し、エンゲージメント上位を急上昇ポスト代わりに返す。
    Xに公式トレンドAPIは無いため、バズりやすいクエリで検索→いいね+RT数で並べ替えて代替する。"""
    if not _has_x_credentials():
        return []

    posts: list[TrendingPostItem] = []
    seen_ids: set[str] = set()
    for query in _BUZZ_QUERIES:
        if len(posts) >= limit:
            break
        tweets = _bird_search(query, count=count_per_query)
        tweets.sort(
            key=lambda t: (t.get("likeCount") or 0) + (t.get("retweetCount") or 0),
            reverse=True,
        )
        for t in tweets:
            tid = t.get("id")
            text = (t.get("text") or "").strip()
            if not tid or tid in seen_ids or len(text) <= 10:
                continue
            username = (t.get("author") or {}).get("username") or "匿名"
            seen_ids.add(tid)
            posts.append(TrendingPostItem(
                keyword=query.split()[0],
                user=username,
                text=text,
                url=f"https://x.com/{username}/status/{tid}",
            ))
            if len(posts) >= limit:
                break

    return posts[:limit]


def fetch_trending_posts(limit: int = 10, posts_per_keyword: int = 2, max_keywords: int = 6) -> list[TrendingPostItem]:
    """急上昇ポストを取得する。AUTH_TOKEN/CT0が使えればX検索（認証済み）を優先し、
    使えない場合はNitterスクレイピングにフォールバックする。"""
    auth_posts = fetch_trending_posts_via_x_auth(limit=limit)
    if auth_posts:
        return auth_posts

    try:
        import httpx
        from bs4 import BeautifulSoup

        trends = fetch_twitter_trends()
        if not trends:
            return []

        posts: list[TrendingPostItem] = []
        for trend in trends[:max_keywords]:
            if len(posts) >= limit:
                break
            found_for_keyword = 0
            for base_url in NITTER_INSTANCES:
                if found_for_keyword >= posts_per_keyword:
                    break
                try:
                    with httpx.Client(timeout=12.0, follow_redirects=True) as client:
                        resp = client.get(
                            f"{base_url}/search",
                            params={"q": trend.keyword, "f": "tweets"},
                            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsSite/1.0)"},
                        )
                        resp.raise_for_status()

                    soup = BeautifulSoup(resp.text, "html.parser")
                    for tweet in soup.select(".timeline-item"):
                        if found_for_keyword >= posts_per_keyword or len(posts) >= limit:
                            break
                        text_el = tweet.select_one(".tweet-content")
                        user_el = tweet.select_one(".username")
                        if not text_el:
                            continue
                        text = text_el.get_text(" ", strip=True)
                        user = user_el.get_text(strip=True) if user_el else "匿名"

                        link_el = tweet.select_one("a.tweet-link") or tweet.select_one(".tweet-date a")
                        href = (link_el.get("href") or "").strip() if link_el else ""
                        post_url = ""
                        if href:
                            path = href.split("#")[0].split("?")[0]
                            if not path.startswith("/"):
                                path = "/" + path
                            post_url = f"https://x.com{path}"

                        if len(text) > 10:
                            posts.append(TrendingPostItem(keyword=trend.keyword, user=user, text=text, url=post_url))
                            found_for_keyword += 1
                    if found_for_keyword > 0:
                        break
                except Exception:
                    continue

        return posts[:limit]
    except Exception:
        return []
