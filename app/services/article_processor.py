"""RSS記事をAI解説付きのサイト記事に変換するパイプライン"""
import re
from .rss_service import NewsItem, sanitize_display_text
from .translate_service import is_foreign_article, translate_and_rewrite
from .ai_batch_service import generate_all_explanations
from .explanation_cache import save_cache, get_cached, get_cached_article_ids
from .article_cache import save_article, load_all
from .article_fetcher import fetch_article_body


def _normalize_title_for_dedup(title: str) -> str:
    """同一内容判定用：余分な空白・記号を除き小文字化（重複記事の正規化）"""
    t = re.sub(r"\s+", " ", (title or "").strip()).lower()
    return re.sub(r"[^\w\u3040-\u9fff\u30a0-\u30ff\u4e00-\u9fff\s]", "", t).strip()


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

    # 日本語記事もタイトルに【】が無ければ付与
    if not item.title.startswith("【"):
        item = NewsItem(
            id=item.id,
            title=_add_bracket_title(item.title),
            link=item.link,
            summary=item.summary,
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


def _add_bracket_title(title: str) -> str:
    """タイトルに【】付きのインパクト語句を先頭に付ける（簡易ルール）"""
    import re
    if title.startswith("【"):
        return title
    t = title.strip()
    bracket_map = [
        (r"(発表|公開|開始|解禁|決定)", "発表"),
        (r"(なぜ|理由|原因|背景)", "なぜ"),
        (r"(判明|発覚|明らかに)", "判明"),
        (r"(初めて|史上初|世界初|日本初)", "初"),
        (r"(急増|急落|急騰|暴落|高騰)", "速報"),
        (r"(改正|法案|規制|制裁)", "注目"),
        (r"(危機|懸念|警告|リスク)", "警鐘"),
        (r"(合意|締結|連携|提携)", "注目"),
    ]
    for pattern, label in bracket_map:
        if re.search(pattern, t):
            return f"【{label}】{t}"
    return f"【解説】{t}"


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
    RSS記事を Autocomplete スコアリング → 軽量フィルタ → 同一内容は1本に → 上位N件をAI処理して掲載。

    1. 軽量フィルタで低価値記事を除外（文字数・ジャンル・キーワード）
    2. Google Autocomplete + トレンド + 高価値キーワードでスコアリング
    3. 既存記事・候補内で「同じ内容」（正規化タイトル一致）は1本だけに除外
    4. スコア上位 max_per_run 件を AI 解説付きで記事化
    """
    if not rss_items:
        return 0
    cached_ids = get_cached_article_ids()
    uncached = [x for x in rss_items if x.id not in cached_ids]

    from .keyword_scorer import rank_and_filter_articles

    if uncached:
        ranked = rank_and_filter_articles(uncached, trend_keywords, max_articles=max_per_run * 3)
        force = False
    else:
        ranked = rank_and_filter_articles(rss_items, trend_keywords, max_articles=max_per_run * 3)
        force = True

    # 既存掲載記事の正規化タイトル（同じ内容は1本だけにするため）
    existing_norm = set()
    for a in load_all():
        existing_norm.add(_normalize_title_for_dedup(a.title))

    # 候補内で正規化タイトルが重複しているものはスコア上位1件だけ残す
    seen_norm = set()
    deduped: list[NewsItem] = []
    for item in ranked:
        norm = _normalize_title_for_dedup(item.title)
        if norm in existing_norm:
            continue
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        deduped.append(item)

    to_process = deduped[:max_per_run]

    count = 0
    for item in to_process:
        try:
            if process_rss_to_site_article(item, force=force):
                count += 1
        except Exception:
            continue
    return count


