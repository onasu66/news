"""ニュース集約・キャッシュサービス"""
from datetime import datetime
from typing import Optional
from .rss_service import fetch_rss_news, NewsItem
from .trends_service import fetch_trending_searches, TrendItem
from .article_cache import load_all, load_all_processed, load_by_id, save_article
from .article_processor import process_new_rss_articles
from .explanation_cache import get_cached_article_ids

# ジャンル表示順
CATEGORY_ORDER = ["総合", "国内", "国際", "テクノロジー", "政治・社会", "スポーツ", "エンタメ"]


def _score_article_by_trends(item: NewsItem, trend_keywords: list[str]) -> int:
    """記事がトレンドキーワード（Google・X急上昇）に何件マッチするか"""
    if not trend_keywords:
        return 0
    text = f"{item.title} {item.summary}"
    score = 0
    for kw in trend_keywords:
        if kw in text:
            score += 1
    return score


# ソース重み（日本向けビュー数・信頼性の代理）
_SOURCE_WEIGHT = {
    "Yahoo!ニュース": 1.2,
    "NHK": 1.2,
    "読売新聞オンライン": 1.2,
    "共同通信": 1.1,
    "Reuters": 1.0,
    "AP News": 1.0,
    "BBC News": 1.0,
}


def _pick_best_trending_article(
    news: list[NewsItem],
    trend_keywords: list[str],
    exclude_ids: set[str],
) -> Optional[NewsItem]:
    """トレンド合致度＋ソース重みが最も高い記事を1件選ぶ（未公開のものから）"""
    candidates = [x for x in news if x.id not in exclude_ids]
    if not candidates:
        return None

    def _score(x):
        trend = _score_article_by_trends(x, trend_keywords)
        weight = _SOURCE_WEIGHT.get(x.source, 1.0)
        return (trend * 10 + weight, x.published)

    return max(candidates, key=_score)


# 1ページあたりの表示件数（ページネーション用）
ITEMS_PER_PAGE = 24
# キャッシュ上の最大件数（全件取得してページネーション）
PAGE_DISPLAY_LIMIT = 2000


class NewsAggregator:
    """ニュースを集約。RSSで読み込んだ記事はDBに蓄積し、ページに残す。"""
    _news_cache: list[NewsItem] = []
    _trends_cache: list[TrendItem] = []
    _last_updated: Optional[datetime] = None
    _trends_last_updated: Optional[datetime] = None

    @classmethod
    def get_news(cls, force_refresh: bool = False) -> list[NewsItem]:
        """
        AI処理済みのサイト記事のみ表示。
        通常リクエスト時はDBから即返却（ブロックしない）。
        force_refresh時のみRSS取得→AI処理を実行。
        """
        if force_refresh or not cls._news_cache:
            processed_ids = get_cached_article_ids()
            cached = load_all_processed(processed_ids)[:PAGE_DISPLAY_LIMIT]
            if cached and not force_refresh:
                cls._news_cache = cached
                cls._last_updated = datetime.now()
                return cls._news_cache
            if force_refresh:
                news = fetch_rss_news()
                if news:
                    # トレンド（Google急上昇＋X）で精査し、合致した話題を優先して取り込む
                    trends = cls.get_trends(force_refresh=True)
                    trend_keywords = [t.keyword for t in trends]
                    process_new_rss_articles(news, max_per_run=5, trend_keywords=trend_keywords)
                processed_ids = get_cached_article_ids()
            cls._news_cache = load_all_processed(processed_ids)[:PAGE_DISPLAY_LIMIT]
            cls._last_updated = datetime.now()
        return cls._news_cache

    @classmethod
    def get_news_by_category(cls, force_refresh: bool = False, page: int = 1) -> tuple[list[tuple[str, list[NewsItem]]], dict]:
        """
        ジャンルごとにグループ化したニュース一覧。
        page=1 は最新、page=2 は過去記事...。
        戻り値: (news_by_category, pagination_info)
        pagination_info: {page, per_page, total, total_pages, has_prev, has_next}
        """
        news = cls.get_news(force_refresh)
        total = len(news)
        per_page = ITEMS_PER_PAGE
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))

        start = (page - 1) * per_page
        page_items = news[start : start + per_page]

        by_cat: dict[str, list[NewsItem]] = {}
        for item in page_items:
            by_cat.setdefault(item.category, []).append(item)
        news_by_category = [
            (cat, by_cat[cat])
            for cat in CATEGORY_ORDER
            if cat in by_cat and by_cat[cat]
        ]

        pagination = {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        }
        return news_by_category, pagination

    @classmethod
    def get_trends(cls, force_refresh: bool = False) -> list[TrendItem]:
        """トレンド検索を取得（10分でキャッシュ更新）"""
        from datetime import timedelta

        now = datetime.now()
        cache_max_age = timedelta(minutes=10)
        if (
            force_refresh
            or not cls._trends_cache
            or (cls._trends_last_updated and now - cls._trends_last_updated > cache_max_age)
        ):
            cls._trends_cache = fetch_trending_searches()
            cls._trends_last_updated = now
        return cls._trends_cache

    @classmethod
    def get_article(cls, article_id: str) -> Optional[NewsItem]:
        """IDで記事を取得（キャッシュ→DBの順で検索）"""
        for item in cls._news_cache:
            if item.id == article_id:
                return item
        return load_by_id(article_id)
