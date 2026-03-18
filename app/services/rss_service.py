"""RSSフィード取得サービス"""
import html
import re
from urllib.parse import quote
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass
from zoneinfo import ZoneInfo
import hashlib
import feedparser

JST = ZoneInfo("Asia/Tokyo")


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


# RSSフィード (URL, 表示名, ジャンル)
# 既存の総合ニュース＋テック系・ビジネス系・サイエンス系をまとめて扱う。
RSS_FEEDS = [
    # 日本の総合・国内ニュース（NHK・共同・読売・Yahoo! などをすべて「国内」扱いに統一）
    ("https://www.nhk.or.jp/rss/news/cat0.xml", "NHK", "国内"),
    ("https://english.kyodonews.net/list/feed/rss4kyodonews-fzone", "共同通信", "国内"),
    ("https://feeds.reuters.com/reuters/topNews", "Reuters", "国際"),
    ("http://hosted2.ap.org/atom/APDEFAULT/3d281c11a96b4ad082fe88aa0db04305", "AP News", "国際"),
    ("https://news.yahoo.co.jp/rss/topics/top-picks.xml", "Yahoo!ニュース", "国内"),
    ("http://feeds.bbci.co.uk/news/rss.xml", "BBC News", "国際"),
    ("https://rss.yomiuri.co.jp/f/yol_topstories", "読売新聞オンライン", "国内"),
    ("https://www.worldnewsintl.org/feed", "World News International", "国際"),
    ("https://www.lemonde.fr/rss/une.xml", "Le Monde", "国際"),

    # テック系（テクノロジー）
    ("http://feeds.arstechnica.com/arstechnica/index", "Ars Technica", "テクノロジー"),
    ("https://techcrunch.com/feed/", "TechCrunch", "テクノロジー"),
    ("https://news.ycombinator.com/rss", "Hacker News", "テクノロジー"),
    ("https://www.theverge.com/rss/index.xml", "The Verge", "テクノロジー"),

    # ビジネス系（国際経済ニュース扱い）
    ("https://www.ft.com/?format=rss", "Financial Times", "国際"),
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC", "国際"),
    ("https://feeds.bloomberg.com/markets/news.rss", "Bloomberg Markets", "国際"),

    # 科学・宇宙系（テクノロジー扱い）
    ("https://www.sciencedaily.com/rss/all.xml", "ScienceDaily", "テクノロジー"),
    ("https://www.nasa.gov/rss/dyn/breaking_news.rss", "NASA", "テクノロジー"),

    # 研究・論文（論文専用ページで表示）
    # 総合科学（当たり率が高い）
    ("https://www.nature.com/nature.rss", "Nature", "研究・論文"),
    ("https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science", "Science Magazine", "研究・論文"),

    # AI・テック（arXiv + Frontiers AI）
    ("https://export.arxiv.org/rss/cs.AI", "arXiv cs.AI", "研究・論文"),
    ("https://export.arxiv.org/rss/cs.LG", "arXiv cs.LG", "研究・論文"),
    ("https://export.arxiv.org/rss/cs.CL", "arXiv cs.CL", "研究・論文"),
    ("https://export.arxiv.org/rss/cs.CV", "arXiv cs.CV", "研究・論文"),
    ("https://www.frontiersin.org/journals/artificial-intelligence/rss", "Frontiers in Artificial Intelligence", "研究・論文"),

    # 物理・宇宙（arXiv astro-ph / quant-ph）
    ("https://export.arxiv.org/rss/astro-ph", "arXiv astro-ph", "研究・論文"),
    ("https://export.arxiv.org/rss/quant-ph", "arXiv quant-ph", "研究・論文"),

    # 筋肉・スポーツ・身体（Frontiers sports / PLOS ONE）
    ("https://www.frontiersin.org/journals/sports-and-active-living/rss", "Frontiers in Sports and Active Living", "研究・論文"),
    ("https://journals.plos.org/plosone/feed/atom", "PLOS ONE", "研究・論文"),

    # 医学・健康（BMJ Open など）
    ("https://bmjopen.bmj.com/rss/current.xml", "BMJ Open", "研究・論文"),

    # 経済・ビジネス
    ("https://www.ssrn.com/index.cfm/en/rss/", "SSRN", "研究・論文"),
    ("https://ideas.repec.org/rss/rss.xml", "IDEAS/RePEc", "研究・論文"),

    # 工学・応用
    ("https://www.mdpi.com/rss/journal/sensors", "Sensors (MDPI)", "研究・論文"),
]

# タイトルキーワードでジャンルを上書き（総合ソース向け）
CATEGORY_KEYWORDS = {
    "スポーツ": ["野球", "サッカー", "試合", "選手", "ゴルフ", "テニス", "NBA", "MLB", "オリンピック", "世界選手権"],
    "エンタメ": ["映画", "ドラマ", "俳優", "女優", "アイドル", "歌手", "コンサート", "ライブ", "芸能"],
    "テクノロジー": ["AI", "量子", "solar", "hydrogen", "aviation", "cyber", "DMARC", "email", "technology", "perovskite", "vertical farm"],
}


def _detect_category_from_title(title: str, default: str) -> str:
    """タイトルからジャンルを推定（総合ソース向け）"""
    t = title.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw.lower() in t for kw in keywords):
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
    """公開日をパース。タイムゾーン付きならJSTに変換してからnaiveで返す（「取得日と同じ日」をJSTで判定するため）"""
    try:
        from dateutil import parser as date_parser
        dt = date_parser.parse(published)
        if dt.tzinfo:
            dt = dt.astimezone(JST).replace(tzinfo=None)
        return dt
    except Exception:
        return datetime.now(JST).replace(tzinfo=None)


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
    """複数のRSSフィードからニュースを取得。
    研究・論文は24時間以内、それ以外は6時間以内の記事に絞る。同一記事（link+title）は1本だけ。"""
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

                # 総合ソースのみタイトルからジャンル推定。研究・論文はRSSの設定をそのまま使う
                if category == "研究・論文":
                    final_category = category
                else:
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

    # 研究・論文は24時間以内、それ以外は6時間以内に絞る（同じ記事はlink+titleでid重複排除済み）
    now_jst = datetime.now(JST).replace(tzinfo=None)
    cutoff_6h = now_jst - timedelta(hours=6)
    cutoff_24h = now_jst - timedelta(hours=24)
    filtered: list[NewsItem] = []
    for x in all_news:
        if x.category == "研究・論文":
            if x.published >= cutoff_24h:
                filtered.append(x)
        else:
            if x.published >= cutoff_6h:
                filtered.append(x)

    # 日付でソート（新しい順＝人気度の代理）
    filtered.sort(key=lambda x: x.published, reverse=True)
    return filtered[:200]
