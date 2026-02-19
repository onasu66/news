"""初期記事の投入・シード処理"""
from .rss_service import fetch_rss_news, NewsItem
from .trends_service import fetch_trending_searches
from .article_cache import load_all, save_articles_batch
from .translate_service import is_foreign_article, translate_and_rewrite

# ソース別の重み（日本向けのビュー数・信頼性の代理）
SOURCE_WEIGHT = {
    "Yahoo!ニュース": 1.2,
    "NHK": 1.2,
    "読売新聞オンライン": 1.2,
    "共同通信": 1.1,
    "Reuters": 1.0,
    "AP News": 1.0,
    "BBC News": 1.0,
}

TARGET_COUNT = 30


def _score_article(item: NewsItem, trend_keywords: list[str]) -> float:
    """トレンド合致 + ソース重みでスコア化"""
    text = f"{item.title} {item.summary}"
    trend_score = sum(1 for kw in trend_keywords if kw in text)
    src_weight = SOURCE_WEIGHT.get(item.source, 1.0)
    return trend_score * 10 + src_weight


def seed_articles(target: int = TARGET_COUNT) -> int:
    """初期記事を投入。トレンド合致・ソース重みで上位を選び、海外は日本語訳して保存"""
    existing = load_all()
    existing_ids = {x.id for x in existing}
    need = max(0, target - len(existing))
    if need == 0:
        return 0

    news = fetch_rss_news()
    trends = fetch_trending_searches()
    trend_keywords = [t.keyword for t in trends]

    candidates = [x for x in news if x.id not in existing_ids]
    ranked = sorted(
        candidates,
        key=lambda x: (_score_article(x, trend_keywords), x.published),
        reverse=True,
    )[:need]

    to_save: list[NewsItem] = []
    for item in ranked:
        if is_foreign_article(item.source, item.title, item.summary):
            title_ja, summary_ja = translate_and_rewrite(item.title, item.summary)
            item = NewsItem(
                id=item.id,
                title=title_ja,
                link=item.link,
                summary=summary_ja,
                published=item.published,
                source=item.source,
                category=item.category,
                image_url=item.image_url,
            )
        to_save.append(item)

    return save_articles_batch(to_save)
