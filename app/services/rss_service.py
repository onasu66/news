"""RSSフィード取得サービス"""
import html
import re
from urllib.parse import quote
from datetime import datetime
from typing import Optional
from dataclasses import dataclass
import hashlib
import feedparser


def _clean_summary(text: str, max_len: int = 18000) -> str:
    """HTMLタグ・実体参照を除去してプレーンテキストにする（先にクリーニングしてから truncate）"""
    if not text:
        return ""
    # 1. 先にHTMLタグ除去（truncateで切れた<a href="...">等の断片も削除）
    text = re.sub(r'<[^>]*>', '', text)   # 閉じタグあり
    text = re.sub(r'<[^>]*', '', text)     # 閉じタグなし断片
    # 2. HTML実体参照をデコード（&nbsp; &amp; &lt; など）
    text = html.unescape(text)
    # 3. 余分な空白を正規化
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_len] if len(text) > max_len else text


def sanitize_display_text(text: str) -> str:
    """表示用テキストからHTML断片・実体参照を除去（キャッシュ済み悪データ対策）"""
    if not text:
        return ""
    text = re.sub(r'<[^>]*>', '', text)
    text = re.sub(r'<[^>]*', '', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


@dataclass
class NewsItem:
    """ニュース記事モデル"""
    id: str
    title: str
    link: str
    summary: str
    published: datetime
    source: str
    category: str  # ジャンル: 総合, 国内, 国際, テクノロジー, 政治・社会, スポーツ, エンタメ
    image_url: Optional[str] = None


# RSSフィード (URL, 表示名, ジャンル) — NHK・共同通信・Reuters・AP・Yahoo!・BBC・読売（全文は記事URL取得で対応）
RSS_FEEDS = [
    ("https://www.nhk.or.jp/rss/news/cat0.xml", "NHK", "国内"),
    ("https://english.kyodonews.net/list/feed/rss4kyodonews-fzone", "共同通信", "国際"),
    ("https://feeds.reuters.com/reuters/topNews", "Reuters", "国際"),
    ("http://hosted2.ap.org/atom/APDEFAULT/3d281c11a96b4ad082fe88aa0db04305", "AP News", "国際"),
    ("https://news.yahoo.co.jp/rss/topics/top-picks.xml", "Yahoo!ニュース", "総合"),
    ("http://feeds.bbci.co.uk/news/rss.xml", "BBC News", "国際"),
    ("https://rss.yomiuri.co.jp/f/yol_topstories", "読売新聞オンライン", "政治・社会"),
]

# タイトルキーワードでジャンルを上書き（総合ソース向け）
CATEGORY_KEYWORDS = {
    "スポーツ": ["野球", "サッカー", "試合", "選手", "ゴルフ", "テニス", "NBA", "MLB", "オリンピック", "世界選手権"],
    "エンタメ": ["映画", "ドラマ", "俳優", "女優", "アイドル", "歌手", "コンサート", "ライブ", "芸能"],
}


def _detect_category_from_title(title: str, default: str) -> str:
    """タイトルからジャンルを推定（総合ソース向け）"""
    t = title.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in title for kw in keywords):
            return cat
    return default


def _extract_image(entry) -> Optional[str]:
    """エントリから画像URLを抽出"""
    # media_content
    if hasattr(entry, "media_content") and entry.media_content:
        return entry.media_content[0].get("url")
    # media_thumbnail
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        return entry.media_thumbnail[0].get("url")
    # enclosure
    if hasattr(entry, "enclosures") and entry.enclosures:
        enc = entry.enclosures[0]
        if enc.get("type", "").startswith("image"):
            return enc.get("href")
    # summaryからimgタグを抽出
    if hasattr(entry, "summary"):
        import re
        match = re.search(r'<img[^>]+src="([^"]+)"', entry.summary)
        if match:
            return match.group(1)
    return None


def _parse_datetime(published: str) -> datetime:
    """公開日をパース（タイムゾーンは除去して比較可能に）"""
    try:
        from dateutil import parser as date_parser
        dt = date_parser.parse(published)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return datetime.now()


def _get_feed_url(original_url: str) -> str:
    """Full-Text RSS が有効なら全文取得用URLに差し替える"""
    try:
        from app.config import settings
        base = getattr(settings, "FULLTEXT_RSS_BASE_URL", "") or ""
    except Exception:
        base = ""
    if not base:
        return original_url
    return f"{base}/makefulltextfeed.php?url={quote(original_url, safe='')}&max=50"


def fetch_rss_news() -> list[NewsItem]:
    """複数のRSSフィードからニュースを取得（Full-Text RSS 有効時は全文フィードに変換）"""
    all_news: list[NewsItem] = []
    seen_ids = set()

    for feed_item in RSS_FEEDS:
        original_url = feed_item[0]
        url = _get_feed_url(original_url)
        source = feed_item[1]
        category = feed_item[2]
        try:
            feed = feedparser.parse(url, agent="NewsSite/1.0")
            for entry in feed.entries[:50]:  # 各フィードから最大50件（未取り込み記事を探す範囲を広げる）
                title = entry.get("title", "")
                link = entry.get("link", "")
                raw_summary = entry.get("summary") or entry.get("description") or ""
                raw_content = ""
                # RSSの全文：content は複数ある場合があるので全て結合（content:encoded 等）
                if getattr(entry, "content", None):
                    for c in entry.content:
                        val = c.get("value", getattr(c, "value", "")) if isinstance(c, dict) else getattr(c, "value", str(c))
                        if val and val not in raw_content:
                            raw_content = (raw_content + "\n\n" + val).strip() if raw_content else val
                # description が summary と別で長い場合も足す
                desc = entry.get("description", "")
                if desc and desc != raw_summary and desc not in raw_content and len(desc) > len(raw_summary):
                    raw_content = (raw_content + "\n\n" + desc).strip() if raw_content else desc
                # summary + content を結合して記事量を確保（重複避けて長く）
                combined = raw_summary or ""
                if raw_content and raw_content not in combined:
                    combined = (combined + "\n\n" + raw_content).strip()
                summary = _clean_summary(combined or "")

                published_str = entry.get("published", entry.get("updated", ""))
                published = _parse_datetime(published_str) if published_str else datetime.now()

                image_url = _extract_image(entry)

                # 重複排除用ID
                item_id = hashlib.md5(f"{link}{title}".encode()).hexdigest()[:16]
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                # 総合ソースはタイトルからジャンルを推定
                final_category = _detect_category_from_title(title, category)

                all_news.append(NewsItem(
                    id=item_id,
                    title=title,
                    link=link,
                    summary=summary,
                    published=published,
                    source=source,
                    category=final_category,
                    image_url=image_url,
                ))
        except Exception:
            continue

    # 日付でソート（新しい順＝人気度の代理）
    all_news.sort(key=lambda x: x.published, reverse=True)
    return all_news[:200]
