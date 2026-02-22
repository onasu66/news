"""RSS記事をAI解説付きのサイト記事に変換するパイプライン"""
from .rss_service import NewsItem, sanitize_display_text
from .translate_service import is_foreign_article, translate_and_rewrite
from .ai_batch_service import generate_all_explanations
from .explanation_cache import save_cache, get_cached, get_cached_article_ids
from .article_cache import save_article
from .article_fetcher import fetch_article_body


def _extract_display_summary(blocks: list) -> str:
    """AIブロックから一覧用の要約を抽出（理解ナビゲーターの事実 or 最初のtextブロック）"""
    for b in blocks:
        if not isinstance(b, dict) or not b.get("content"):
            continue
        if b.get("type") == "navigator_section" and b.get("section") == "facts":
            text = b["content"].strip()
            return text[:200] + ("..." if len(text) > 200 else "")
        if b.get("type") == "text":
            text = b["content"].strip()
            return text[:200] + ("..." if len(text) > 200 else "")
    return ""


def process_rss_to_site_article(item: NewsItem, force: bool = False) -> bool:
    """
    RSS記事をミドルマンAI解説付きのサイト記事に変換して掲載。
    成功したらTrue、既に処理済みや失敗ならFalse。
    force=True のときは既存キャッシュを無視して上書き取り込みする。
    """
    if not force and get_cached(item.id):
        return False  # 既にAI処理済み（force でなければスキップ）

    # 海外記事はタイトル・要約を日本語に
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

    # 記事URLから本文を取得して反映（取れればRSS要約より充実した内容に）
    body = fetch_article_body(item.link)
    if body:
        body_clean = sanitize_display_text(body)[:40000]
        # 英語本文は日本語に翻訳してから反映（約3分で読める分量になるようAIで調整）
        if is_foreign_article(item.source, item.title, body_clean):
            from app.services.translate_service import translate_article_body
            body_clean = translate_article_body(body_clean)
        content = sanitize_display_text(f"{item.title}\n\n{item.summary}\n\n{body_clean}")
    else:
        content = sanitize_display_text(f"{item.title}\n\n{item.summary}")
    data = generate_all_explanations(item.id, item.title, content)
    blocks = data.get("blocks", [])
    personas = data.get("personas", [""] * 5)

    if not blocks:
        return False

    save_cache(item.id, blocks, personas)
    if not save_article(item):
        return False  # 記事の保存に失敗した場合は成功にしない
    return True


def _rank_by_trending(items: list[NewsItem], trend_keywords: list[str]) -> list[NewsItem]:
    """トレンド合致度＋ソース重みで記事をランク付け（話題度の高い順）"""
    SOURCE_WEIGHT = {
        "Yahoo!ニュース": 1.2, "NHK": 1.2, "読売新聞オンライン": 1.2,
        "共同通信": 1.1, "Reuters": 1.0, "AP News": 1.0, "BBC News": 1.0,
    }

    def score(x):
        text = f"{x.title} {x.summary}"
        trend = sum(1 for kw in trend_keywords if kw in text)
        weight = SOURCE_WEIGHT.get(x.source, 1.0)
        return (trend * 10 + weight, x.published)

    return sorted(items, key=score, reverse=True)


def process_new_rss_articles(rss_items: list[NewsItem], max_per_run: int = 5, trend_keywords: list[str] | None = None) -> int:
    """
    RSSから取得した記事のうち未処理のものをAIで変換して掲載。
    トレンドキーワードがある場合はトレンド合致度で精査し、合致した話題を優先して取り込む。
    未取り込みがなければ、RSSの最新を強制で上書き取り込みする（ボタンで必ず1件追加できるようにする）。
    """
    if not rss_items:
        return 0
    cached_ids = get_cached_article_ids()
    uncached = [x for x in rss_items if x.id not in cached_ids]
    # トレンドで精査: キーワードがあるときはトレンド合致＋ソース重みでランクし、上位から取り込む
    if trend_keywords:
        uncached = _rank_by_trending(uncached, trend_keywords)
    if not uncached:
        # 未取り込みがなければ全件をトレンド順（または日付順）で並べ直し、上書き取り込み
        ranked_all = _rank_by_trending(rss_items, trend_keywords) if trend_keywords else rss_items
        to_process = ranked_all[:max_per_run]
        force = True
    else:
        to_process = uncached[:max_per_run]
        force = False

    count = 0
    for item in to_process:
        try:
            if process_rss_to_site_article(item, force=force):
                count += 1
        except Exception:
            continue
    return count


