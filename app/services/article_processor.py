"""RSS記事をAI解説付きのサイト記事に変換するパイプライン"""
import re
from .rss_service import NewsItem, sanitize_display_text
from .translate_service import is_foreign_article, translate_and_rewrite, translate_title_to_japanese, text_mainly_japanese
from .ai_batch_service import generate_all_explanations
from .explanation_cache import save_cache, get_cached, get_cached_article_ids
from .article_cache import save_article, load_all
from .article_fetcher import fetch_article_body
from .save_history import add_entry as _log_save


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

    # --- タイトル・要約を必ず日本語にしてから保存する ---
    need_translate = is_foreign_article(item.source, item.title, item.summary or "")
    if not need_translate and item.title and not text_mainly_japanese(item.title):
        need_translate = True
    if not need_translate and item.summary and not text_mainly_japanese(item.summary):
        need_translate = True

    title_ja = item.title or ""
    summary_ja = item.summary or ""

    if need_translate:
        # 1) タイトル＋要約を一括翻訳
        t, s = translate_and_rewrite(item.title or "", item.summary or "")
        if t and text_mainly_japanese(t):
            title_ja = t
        if s and text_mainly_japanese(s):
            summary_ja = s
        # 2) タイトルがまだ日本語でなければタイトル専用翻訳（リトライ）
        if not text_mainly_japanese(title_ja):
            t2 = translate_title_to_japanese(item.title or "")
            if t2 and text_mainly_japanese(t2):
                title_ja = t2
        # 3) 要約がまだ日本語でなければ再翻訳して Firestore には日本語で保存
        if not text_mainly_japanese(summary_ja):
            _, s2 = translate_and_rewrite(item.title or "", item.summary or "")
            if s2 and text_mainly_japanese(s2):
                summary_ja = s2

    # 【】が既に付いていればそのまま、なければ AI で目を引く【】を付与
    if not title_ja.startswith("【"):
        title_ja = _add_bracket_title(title_ja)

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

    # 記事を先に保存してから解説を保存（Firestore で has_explanation を正しく付与するため）
    if not save_article(item):
        return False  # 記事の保存に失敗した場合は成功にしない
    save_cache(item.id, blocks, personas)
    return True


def _add_bracket_title(title: str) -> str:
    """タイトル先頭に【○○】を付ける。AIで内容に合った目を引くフレーズを生成。失敗時はルールベースで付与。"""
    import re
    if title.startswith("【"):
        return title
    t = title.strip()

    # AI で【】の中身を生成
    try:
        from app.utils.openai_compat import create_with_retry
        from openai import OpenAI
        from app.config import settings
        if settings.OPENAI_API_KEY:
            client = OpenAI(api_key=settings.OPENAI_API_KEY)
            resp = create_with_retry(
                client,
                50,
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "ニュース見出しの先頭に付ける【】の中身を1つだけ出力するアシスタント。"},
                    {"role": "user", "content": f"""次のニュースタイトルに合う【】の中身を1つだけ出力してください。
ルール：
- 2〜4文字の日本語のみ（英語禁止）
- 読者の目を引く・クリックしたくなる言葉
- 例：なぜ、衝撃、驚き、速報、激震、転換、急展開、真相、裏側、本音、盲点、必見、要注意、朗報、悲報、異変、深層
- 「解説」は使わない。もっとインパクトのある言葉を選ぶ
- 【】は付けず中身だけ出力

タイトル：{t[:200]}"""},
                ],
                temperature=0.7,
            )
            label = (resp.choices[0].message.content or "").strip().strip("【】「」").strip()
            if label and 1 <= len(label) <= 6:
                return f"【{label}】{t}"
    except Exception:
        pass

    # フォールバック：ルールベース
    bracket_map = [
        (r"(なぜ|理由|原因|背景|どうして)", "なぜ"),
        (r"(何|とは|どういう)", "真相"),
        (r"(発表|公開|開始|解禁|決定)", "速報"),
        (r"(判明|発覚|明らかに)", "判明"),
        (r"(初めて|史上初|世界初|日本初)", "驚き"),
        (r"(急増|急落|急騰|暴落|高騰)", "激震"),
        (r"(改正|法案|規制|制裁)", "要注意"),
        (r"(危機|懸念|警告|リスク)", "警鐘"),
        (r"(合意|締結|連携|提携)", "注目"),
        (r"(戦争|紛争|攻撃|侵攻)", "緊迫"),
        (r"(逮捕|起訴|容疑|事件)", "衝撃"),
        (r"(勝利|優勝|達成|記録)", "快挙"),
    ]
    for pattern, label in bracket_map:
        if re.search(pattern, t):
            return f"【{label}】{t}"
    return f"【必見】{t}"


def _select_diverse_batch(
    items: list[NewsItem],
    max_per_run: int,
    max_per_source: int = 2,
    max_per_category: int = 2,
) -> list[NewsItem]:
    """
    スコア順の候補から、同ソース最大 max_per_source 件・同ジャンル最大 max_per_category 件を守りつつ、
    最大 max_per_run 件を選ぶ（多様性を確保）。
    """
    source_count: dict[str, int] = {}
    category_count: dict[str, int] = {}
    chosen: list[NewsItem] = []
    # 第1パス: キャップを守りながら選ぶ
    for item in items:
        if len(chosen) >= max_per_run:
            break
        src = item.source or ""
        cat = item.category or "総合"
        if source_count.get(src, 0) >= max_per_source or category_count.get(cat, 0) >= max_per_category:
            continue
        chosen.append(item)
        source_count[src] = source_count.get(src, 0) + 1
        category_count[cat] = category_count.get(cat, 0) + 1
    # 第2パス: 足りなければキャップ無視で追加
    if len(chosen) < max_per_run:
        for item in items:
            if len(chosen) >= max_per_run:
                break
            if item in chosen:
                continue
            chosen.append(item)
    return chosen[:max_per_run]


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

    to_process = _select_diverse_batch(deduped, max_per_run, max_per_source=2, max_per_category=2)

    count = 0
    for item in to_process:
        try:
            if process_rss_to_site_article(item, force=force):
                count += 1
                _log_save(item.id, item.title, True, source="rss_seed")
            else:
                _log_save(item.id, item.title, False, error="スキップ（既存または生成失敗）", source="rss_seed")
        except Exception as e:
            _log_save(item.id, item.title, False, error=str(e), source="rss_seed")
    return count


def process_random_rss_articles(rss_items: list[NewsItem], count: int = 3) -> int:
    """
    RSS記事からランダムに count 件を選び、AI解説付きでFirestore/SQLiteに保存する。
    軽量フィルタ通過・未保存・同一内容重複排除のあと、シャッフルして先頭 count 件を処理。
    """
    if not rss_items:
        return 0
    from .keyword_scorer import lightweight_filter

    cached_ids = get_cached_article_ids()
    # 軽量フィルタ通過 & 未保存
    candidates = [
        x for x in rss_items
        if x.id not in cached_ids and lightweight_filter(x.title, x.summary, x.category)
    ]
    if not candidates:
        return 0

    existing_norm = {_normalize_title_for_dedup(a.title) for a in load_all()}
    seen_norm = set()
    deduped: list[NewsItem] = []
    for item in candidates:
        norm = _normalize_title_for_dedup(item.title)
        if norm in existing_norm or norm in seen_norm:
            continue
        seen_norm.add(norm)
        deduped.append(item)

    random.shuffle(deduped)
    to_process = deduped[:count]

    n = 0
    for item in to_process:
        try:
            if process_rss_to_site_article(item, force=False):
                n += 1
                _log_save(item.id, item.title, True, source="rss_random")
            else:
                _log_save(item.id, item.title, False, error="スキップ（既存または生成失敗）", source="rss_random")
        except Exception as e:
            _log_save(item.id, item.title, False, error=str(e), source="rss_random")
    return n


