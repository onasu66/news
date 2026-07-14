"""RSS記事をAI解説付きのサイト記事に変換するパイプライン"""
import logging
import re
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from .rss_service import NewsItem, sanitize_display_text, JST
from .translate_service import is_foreign_article, translate_and_rewrite, translate_title_to_japanese, text_mainly_japanese
from .ai_batch_service import generate_all_explanations, upgrade_personas_with_claude_if_configured
from .explanation_cache import save_cache, get_cached, get_cached_article_ids
from .article_cache import save_article, load_all
from .article_fetcher import fetch_article_body
from .article_content_quality import (
    is_generated_article_sufficient,
    is_source_material_sufficient,
)
from .save_history import add_entry as _log_save

logger = logging.getLogger(__name__)


def _wait_between_gemini_articles(index: int, total: int) -> None:
    """Gemini 無料枠の RPM 対策: 記事候補の処理間に待機。"""
    if index >= total - 1:
        return
    try:
        import time
        from app.config import settings
        from app.utils.llm_client import use_gemini

        if not use_gemini():
            return
        sec = max(0, int(getattr(settings, "GEMINI_ARTICLE_INTERVAL_SEC", 45) or 0))
        if sec <= 0:
            return
        logger.info("Gemini RPM 対策: 次の記事候補まで %d 秒待機 (%d/%d)", sec, index + 1, total)
        time.sleep(sec)
    except Exception:
        pass


def _normalize_title_for_dedup(title: str) -> str:
    """同一内容判定用：余分な空白・記号を除き小文字化（重複記事の正規化）"""
    t = re.sub(r"\s+", " ", (title or "").strip()).lower()
    return re.sub(r"[^\w\u3040-\u9fff\u30a0-\u30ff\u4e00-\u9fff\s]", "", t).strip()


_URL_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "source",
    "rss",
}


def _normalize_url_for_dedup(url: str) -> str:
    """同一URL判定用。計測クエリ・fragment・末尾スラッシュ差分を吸収する。"""
    raw = (url or "").strip()
    if not raw or raw == "#":
        return ""
    try:
        parts = urlsplit(raw)
    except Exception:
        return raw.rstrip("/")
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r"/+$", "", parts.path or "/")
    query_items = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in _URL_TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    query = urlencode(sorted(query_items), doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def _title_similarity(a: str, b: str) -> float:
    na = _normalize_title_for_dedup(a)
    nb = _normalize_title_for_dedup(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    shorter, longer = sorted((na, nb), key=len)
    if len(shorter) >= 18 and shorter in longer:
        return 0.96
    return SequenceMatcher(None, na, nb).ratio()


def _is_duplicate_against_existing(item: NewsItem, existing_articles: list[NewsItem]) -> bool:
    """既存記事に同一URLまたはかなり近い見出しがあれば重複扱いにする。"""
    item_url = _normalize_url_for_dedup(getattr(item, "link", "") or "")
    item_title = getattr(item, "title", "") or ""
    item_cat = (getattr(item, "category", "") or "").strip()
    for existing in existing_articles or []:
        if getattr(existing, "id", "") == getattr(item, "id", ""):
            continue
        existing_url = _normalize_url_for_dedup(getattr(existing, "link", "") or "")
        if item_url and existing_url and item_url == existing_url:
            logger.info("重複スキップ(URL一致): %s", item_title[:80])
            return True
        existing_cat = (getattr(existing, "category", "") or "").strip()
        if item_cat and existing_cat and item_cat != existing_cat:
            continue
        if _title_similarity(item_title, getattr(existing, "title", "") or "") >= 0.92:
            logger.info("重複スキップ(タイトル類似): %s", item_title[:80])
            return True
    return False


_SOFT_NEWS_CATEGORIES = {"スポーツ", "エンタメ"}


def _is_soft_news_category(item: NewsItem) -> bool:
    return (getattr(item, "category", "") or "").strip() in _SOFT_NEWS_CATEGORIES


def _soft_news_limit(max_per_run: int) -> int:
    """スポーツ・エンタメ合計の上限。検索需要は拾うが、サイト全体の偏りを抑える。"""
    try:
        from app.config import settings

        configured = max(0, int(getattr(settings, "NEWS_SOFT_CATEGORY_MAX_PER_RUN", 2) or 2))
        ratio = max(0.0, min(1.0, float(getattr(settings, "NEWS_SOFT_CATEGORY_MAX_RATIO", 0.25) or 0.25)))
    except Exception:
        configured = 2
        ratio = 0.25
    ratio_limit = max(1, int(max_per_run * ratio)) if max_per_run > 0 else 0
    return max(0, min(configured, ratio_limit))


def _cap_soft_news(items: list[NewsItem], max_per_run: int) -> list[NewsItem]:
    limit = _soft_news_limit(max_per_run)
    if limit <= 0:
        return [x for x in items if not _is_soft_news_category(x)]
    soft_count = 0
    out: list[NewsItem] = []
    deferred: list[NewsItem] = []
    for item in items:
        if _is_soft_news_category(item):
            if soft_count < limit:
                out.append(item)
                soft_count += 1
            else:
                deferred.append(item)
        else:
            out.append(item)
    return out + deferred


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
    try:
        if _is_duplicate_against_existing(item, load_all()):
            return False
    except Exception:
        pass

    # 有料紙は本文が取れないため、翻訳・タイトル生成 API を使う前に弾く
    if (item.category or "").strip() != "研究・論文":
        try:
            from app.services.paywall_domains import is_paywalled_url, paywall_domain_label

            if is_paywalled_url(item.link or ""):
                logger.warning(
                    "有料メディアのため記事化スキップ: %s (%s)",
                    (item.title or "")[:60],
                    paywall_domain_label(item.link or "") or item.link,
                )
                return False
        except Exception:
            pass

    # --- タイトル・要約を日本語に（APIは1回＋必要時のみタイトル1回に抑える）---
    need_translate = is_foreign_article(item.source, item.title, item.summary or "")
    if not need_translate and item.title and not text_mainly_japanese(item.title):
        need_translate = True
    if not need_translate and item.summary and not text_mainly_japanese(item.summary):
        need_translate = True

    title_ja = item.title or ""
    summary_ja = item.summary or ""

    if need_translate:
        t, s = translate_and_rewrite(item.title or "", item.summary or "")
        if t and text_mainly_japanese(t):
            title_ja = t
        if s and text_mainly_japanese(s):
            summary_ja = s
        if not text_mainly_japanese(title_ja):
            t2 = translate_title_to_japanese(item.title or "")
            if t2 and text_mainly_japanese(t2):
                title_ja = t2
    # 翻訳に失敗した記事が英語のまま公開されるのを防ぐ。FOREIGN_SOURCES（既知の海外ソース名）の
    # 有無に依存せず、実際のタイトル・要約の文字種で判定する（CNBC/Bloomberg/TechCrunch等、
    # リストにないソースの翻訳失敗が素通りしていたバグの修正）。
    if item.category == "研究・論文":
        # 論文: 要約が日本語であれば保存する（タイトルが英語のままでも許容）
        if not text_mainly_japanese(summary_ja):
            return False
    else:
        # 通常ニュース: タイトル・要約ともに日本語が必要
        if not text_mainly_japanese(title_ja) or not text_mainly_japanese(summary_ja):
            return False

    # タイトルは記事化時に毎回生成（事実は維持し、誇張は避ける）
    title_before_rewrite = title_ja
    title_ja = _rewrite_news_title(title_ja, summary_ja, item.category)
    # リライト後も英語のままなら元の日本語タイトルに戻す（またはタイトル翻訳を再試行）
    if title_ja and not text_mainly_japanese(title_ja):
        if text_mainly_japanese(title_before_rewrite):
            title_ja = title_before_rewrite
        else:
            t3 = translate_title_to_japanese(item.title or "")
            if t3 and text_mainly_japanese(t3):
                title_ja = t3

    # ジャンルはRSSごとの設定（＋総合ソースはタイトルキーワード補正）のまま使う。AI分類は使わない。
    # 公開日時は「記事として取り込んだ時刻」を使う（元のRSSが古い日時でも、サイト上では追加順に並ぶようにする）
    published_dt = datetime.now(JST).replace(tzinfo=None)
    item = NewsItem(
        id=item.id,
        title=title_ja,
        link=item.link,
        summary=summary_ja,
        published=published_dt,
        source=item.source,
        category=item.category,
        image_url=item.image_url,
    )

    # 記事URLから本文を取得して反映（取れればRSS要約より充実した内容に）
    body = fetch_article_body(item.link)
    body_clean = ""
    if body:
        body_clean = sanitize_display_text(body)[:40000]

    # 翻訳前に素材量チェック（翻訳APIの無駄呼び出しを防ぐ）
    is_paper = (item.category or "").strip() == "研究・論文"
    if not is_source_material_sufficient(
        item.title, item.summary, body_clean or None, is_paper=is_paper
    ):
        logger.warning("素材不足のため記事化スキップ: %s", (item.title or "")[:60])
        return False

    if body_clean:
        # 英語本文は日本語に翻訳してから反映（言い換えで水増しせず、情報密度を優先してAIで調整）
        if is_foreign_article(item.source, item.title, body_clean):
            from app.services.translate_service import translate_article_body
            body_clean = translate_article_body(body_clean)

    if body_clean:
        content = sanitize_display_text(f"{item.title}\n\n{item.summary}\n\n{body_clean}")
    else:
        content = sanitize_display_text(f"{item.title}\n\n{item.summary}")
    data = generate_all_explanations(
        item.id, item.title, content, category=item.category, persist_cache=False
    )
    blocks = data.get("blocks", [])
    personas = list(data.get("personas") or ["", "", ""])
    while len(personas) < 3:
        personas.append("")
    personas = personas[:3]
    display_persona_ids = data.get("display_persona_ids")

    if not blocks or not is_generated_article_sufficient(blocks):
        logger.warning(
            "生成記事が品質基準未達のためスキップ: %s",
            (item.title or "")[:60],
        )
        return False

    # 記事を先に保存してから解説を保存（Neon で has_explanation を付与するため）
    if not save_article(item):
        return False  # 記事の保存に失敗した場合は成功にしない
    summary_for_persona = str(data.get("navigator_summary") or "")
    dips = list(display_persona_ids) if display_persona_ids is not None else []
    personas = upgrade_personas_with_claude_if_configured(
        item.title, summary_for_persona, dips, personas
    )
    save_cache(
        item.id, blocks, personas,
        display_persona_ids=display_persona_ids,
        quick_understand=data.get("quick_understand"),
        vote_data=data.get("vote_data"),
        paper_graph=data.get("paper_graph"),
        paper_quiz=data.get("paper_quiz"),
        deep_insights=data.get("deep_insights"),
        editorial_take=data.get("editorial_take"),
    )
    # IndexNow（Bing 等）・Render キャッシュ通知
    try:
        from .news_aggregator import NewsAggregator
        from .indexnow_service import notify_indexnow_article, queue_indexnow_article

        if NewsAggregator._bulk_update_depth > 0:
            queue_indexnow_article(item.id)
        else:
            notify_indexnow_article(item.id)
    except Exception:
        pass
    try:
        from .news_aggregator import NewsAggregator

        if NewsAggregator._bulk_update_depth == 0:
            from .render_notifier import notify_render_cache_refresh

            notify_render_cache_refresh(reason=f"article_saved:{item.id[:16]}")
    except Exception:
        pass
    return True


def _rewrite_news_title(title: str, summary: str = "", category: str = "") -> str:
    """記事化時タイトルを生成。OpenAI失敗時はルールベース短縮へフォールバック。"""
    t = (title or "").strip()
    if not t:
        return ""

    # すでに【】が付いていても、煽りを避けるため外す（必要ならAIで自然なタイトルに戻す）
    if t.startswith("【") and "】" in t[:12]:
        t = t.split("】", 1)[1].strip()

    # 長すぎる場合のみ短縮（まずはルールベースで）。単語の途中で切れないよう、
    # 区切りになりやすい文字の直後/直前まで戻ってから「…」を付ける。
    def _shorten(s: str, max_len: int = 42) -> str:
        s = " ".join(s.split())
        if len(s) <= max_len:
            return s
        chunk = s[:max_len]
        for sep in ("」", "』", "）", ")", "、", "・", " "):
            i = chunk.rfind(sep)
            if i >= max_len // 2:
                cut = i + 1 if sep in ("」", "』", "）", ")") else i
                return chunk[:cut].rstrip("、・ ") + "…"
        return chunk + "…"

    def _looks_search_friendly(s: str) -> bool:
        markers = (
            "とは",
            "なぜ",
            "何か",
            "仕組み",
            "理由",
            "影響",
            "課題",
            "今後",
            "解説",
            "わかる",
            "わかった",
        )
        return any(marker in s for marker in markers)

    def _search_friendly_fallback(s: str) -> str:
        base = " ".join((s or "").split()).strip("「」\"'。、")
        if not base:
            return ""
        if _looks_search_friendly(base):
            if "解説" not in base and len(base) <= 36:
                return _shorten(f"{base}を解説")
            return _shorten(base)

        is_paper_fallback = (category or "").strip() == "研究・論文"
        suffixes = (
            ("とは？研究結果と影響を解説", 19),
            ("とは？仕組みと影響を解説", 17),
            ("とは？要点を解説", 10),
        ) if is_paper_fallback else (
            ("とは？仕組みと影響を解説", 17),
            ("とは？理由と今後を解説", 15),
            ("とは？要点を解説", 10),
        )
        for suffix, reserve in suffixes:
            head = base[: max(12, 42 - reserve)].rstrip("、・ ")
            candidate = f"{head}{suffix}"
            if len(candidate) <= 42:
                return candidate
        return _shorten(f"{base}とは？要点を解説")

    # AI が使える場合は「編集者リライト」を1回だけ試す
    try:
        from app.config import settings
        from app.utils.llm_client import get_chat_client, is_ai_configured
        enabled = str(getattr(settings, "TITLE_GENERATION_ENABLED", "true")).strip().lower() in ("1", "true", "yes")
        if is_ai_configured() and enabled:
            from app.utils.openai_compat import create_with_retry
            client = get_chat_client()
            is_paper = (category or "").strip() == "研究・論文"

            # 検索意図タイプを推定して追加指示を出す
            _intro_kws = ("とは", "何", "なに", "仕組み", "原因", "理由", "効果", "使い方", "違い", "メリット", "デメリット")
            _summary_text = (summary or "").lower()
            _needs_intro_style = any(kw in t or kw in _summary_text[:200] for kw in _intro_kws)

            system_prompt = """あなたはニュース編集者兼SEO担当です。検索で見つかりやすく、かつ思わず読みたくなる見出しに整えます。
ルール：
- 出力は必ず日本語のみ（ひらがな・カタカナ・漢字）。英語・ローマ字は1文字も含めない
- 元タイトル・要約の事実は変えない（誇張・捏造は禁止）
- ユーザーが検索しそうな語（固有名詞・企業名・地名・数字・イベント名）を前半に置く
- 「えっ、そうなの？」と思わせる意外性・驚きがあれば積極的に表現する
- 具体的な数字や固有名詞があればタイトルに含める（「約3割」「〇〇社」「〇〇歳」など）
- 原則として検索されやすい解説型にする。「○○とは？仕組みと影響を解説」「○○の理由と今後を解説」「○○でわかった○○とは」のような構造を優先
- 「とは」「仕組み」「理由」「影響」「今後」「解説」のうち少なくとも1語を自然に含める
- 煽り・デマ誘導は禁止（「衝撃」「悲報」「暴露」「真相」「ヤバい」など）
- 28〜42文字程度。長い場合は重要キーワードを残して短く
- 自然な日本語。キーワードの羅列や【】は使わない
- 出力はタイトル1行のみ（引用符や説明不要）"""
            if is_paper:
                system_prompt += "\n- 論文は研究対象・主要な数値・実用的な発見が伝わる見出し。「○○とは？研究結果と影響を解説」「○○でわかった○○とは」を優先"
            if _needs_intro_style:
                system_prompt += "\n- この記事は概念説明・背景解説向き。「○○とは何か」「○○の仕組みと影響」などの解説型タイトルが最適"

            user_prompt = (
                f"カテゴリ：{category or 'ニュース'}\n"
                f"元タイトル：{t}\n"
                f"要約：{(summary or '')[:800]}\n\n"
                "Google で「○○ とは」「○○ 何」「○○ 理由」「○○ 仕組み」で検索する読者も意識し、"
                "上のルールで見出し1行だけ出力してください。"
            )
            model = (getattr(settings, "TITLE_OPENAI_MODEL", "") or "").strip() or settings.OPENAI_MODEL
            # gpt- 指定時は AI_PROVIDER=gemini でも OpenAI 直行（openai_compat）
            resp = create_with_retry(
                client,
                120,
                gemini_task="title",
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.55,
            )
            out = (resp.choices[0].message.content or "").strip().strip("「」\"'")
            if out and text_mainly_japanese(out):
                return _search_friendly_fallback(out)
    except Exception:
        pass

    return _search_friendly_fallback(t)


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
        import re as _re
        from .keyword_scorer import seo_potential_score
        text = f"{x.title} {x.summary}"
        trend = 0
        for kw in trend_keywords:
            tokens = [t for t in _re.split(r"\s+", kw.strip()) if len(t) >= 2]
            if kw in text:
                trend += 2  # フレーズ完全一致
            else:
                trend += sum(1 for t in tokens if t in text)  # トークン個別一致
        weight = SOURCE_WEIGHT.get(x.source, 1.0)
        seo = seo_potential_score(x.title, x.summary, x.category)
        return (trend * 10 + seo * 1.5 + weight, x.published)

    return sorted(items, key=score, reverse=True)


def process_startup_articles(rss_items: list[NewsItem] | None = None, trend_keywords: list[str] | None = None) -> int:
    """
    起動時用: 日本関連記事1本＋海外記事1本を追加する（ルールは process_new_rss_articles と同じ）。
    rss_items が None の場合は内部で fetch する。
    """
    from .rss_service import fetch_rss_news
    from .keyword_scorer import rank_and_filter_articles

    if rss_items is None:
        rss_items = fetch_rss_news()
    if not rss_items:
        return 0

    cached_ids = get_cached_article_ids()
    uncached = [x for x in rss_items if x.id not in cached_ids]
    base_pool = uncached if uncached else rss_items
    force = bool(not uncached)

    # --- [AI選定] 有効時: 起動時の2本もAIで選定 ---
    from .article_selector import select_articles_with_ai, is_ai_curation_enabled
    if is_ai_curation_enabled() and base_pool:
        from .keyword_scorer import lightweight_filter
        prefiltered = [x for x in base_pool if lightweight_filter(x.title, x.summary, x.category)]
        if prefiltered:
            base_pool = select_articles_with_ai(
                prefiltered,
                max_select=20,
                trend_keywords=trend_keywords,  # 呼び出し元のトレンドも渡す（内部でも自動取得）
            )

    ranked = rank_and_filter_articles(base_pool, trend_keywords, max_articles=15)

    existing_norm = {_normalize_title_for_dedup(a.title) for a in load_all()}
    seen_norm: set[str] = set()
    deduped: list[NewsItem] = []
    for item in ranked:
        norm = _normalize_title_for_dedup(item.title)
        if norm in existing_norm or norm in seen_norm:
            continue
        seen_norm.add(norm)
        deduped.append(item)

    domestics = [x for x in deduped if not is_foreign_article(x.source, x.title, x.summary or "")]
    foreigners = [x for x in deduped if is_foreign_article(x.source, x.title, x.summary or "")]

    domestic_pick = _select_diverse_batch(domestics, 1, max_per_source=1, max_per_category=1)
    foreign_pick = _select_diverse_batch(foreigners, 1, max_per_source=1, max_per_category=1)
    to_process: list[NewsItem] = domestic_pick + foreign_pick

    count = 0
    for item in to_process:
        try:
            if process_rss_to_site_article(item, force=force):
                count += 1
                _log_save(item.id, item.title, True, source="startup")
            else:
                _log_save(item.id, item.title, False, error="スキップ（既存または生成失敗）", source="startup")
        except Exception as e:
            _log_save(item.id, item.title, False, error=str(e), source="startup")
    return count


def _detect_rss_time_slot() -> str:
    """現在時刻（JST）からRSSスロットを判定する。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    hour = datetime.now(ZoneInfo("Asia/Tokyo")).hour
    if 5 <= hour < 14:
        return "morning"   # 朝～昼: 国内速報・政治社会優先
    elif 14 <= hour < 21:
        return "afternoon"  # 午後～夜: テクノロジー・国際バランス
    else:
        return "night"     # 深夜: 研究・論文多め


# RSS時間帯スロット設定（Claude設定と連動）
_RSS_SLOT_CONFIGS: dict[str, dict] = {
    "morning": {
        "label": "朝（速報・国内重視）",
        "paper_ratio": 0.25,    # 全体の25%を論文に
        "domestic_ratio": 0.70, # ニュースの70%を国内に
        "min_news_slots": 6,
    },
    "afternoon": {
        "label": "午後（テクノロジー・解説）",
        "paper_ratio": 0.35,
        "domestic_ratio": 0.50,
        "min_news_slots": 5,
    },
    "night": {
        "label": "夜（論文・深掘り）",
        "paper_ratio": 0.50,    # 半分を論文に
        "domestic_ratio": 0.55,
        "min_news_slots": 4,
    },
}


def process_new_rss_articles(
    rss_items: list[NewsItem],
    max_per_run: int = 5,
    trend_keywords: list[str] | None = None,
    existing_articles: list[NewsItem] | None = None,
    time_slot: str | None = None,
) -> int:
    """
    RSS記事を Autocomplete スコアリング → 軽量フィルタ → 同一内容は1本に → 上位N件をAI処理して掲載。

    time_slot: "morning" / "afternoon" / "night"。None の場合は現在時刻から自動判定。
    existing_articles を渡すと load_all() を呼ばずに既存タイトルで重複排除。
    """
    if time_slot is None:
        time_slot = _detect_rss_time_slot()
    slot_cfg = _RSS_SLOT_CONFIGS.get(time_slot) or _RSS_SLOT_CONFIGS["morning"]
    logger.info("RSS記事化: スロット=%s (%s) max=%d", time_slot, slot_cfg["label"], max_per_run)
    if not rss_items:
        return 0
    cached_ids = get_cached_article_ids()
    uncached = [x for x in rss_items if x.id not in cached_ids]

    from .keyword_scorer import rank_and_filter_articles

    # 新規候補があれば新規を優先、なければ既存も含めて上書き取り込み（force=True）
    base_candidates = uncached if uncached else rss_items
    force = False if uncached else True

    # --- [AI選定] 有効時: 候補を OpenAI で一括評価して絞り込む ---
    from .article_selector import select_articles_with_ai, is_ai_curation_enabled
    if is_ai_curation_enabled() and base_candidates:
        # 軽量フィルタを通過したものだけを選定対象にする（コスト削減）
        from .keyword_scorer import lightweight_filter
        prefiltered = [
            x for x in base_candidates
            if lightweight_filter(x.title, x.summary, x.category)
        ]
        if prefiltered:
            base_candidates = select_articles_with_ai(
                prefiltered,
                max_select=max(max_per_run * 4, 30),
                trend_keywords=trend_keywords,  # 呼び出し元のトレンドも渡す（内部でも自動取得）
            )

    # 論文を増やしたい要件のため「論文とニュースを同じ上位N件で奪い合う」方式は避け、
    # 論文を先に別枠で確保→残り枠をニュースで埋める。
    paper_candidates = [x for x in base_candidates if x.category == "研究・論文"]
    news_candidates = [x for x in base_candidates if x.category != "研究・論文"]

    # 既存掲載記事の正規化タイトル（同じ内容は1本だけにするため）。渡されていれば load_all() しない
    existing_items_for_dedup = list(existing_articles) if existing_articles is not None else load_all()
    existing_norm = set()
    if existing_articles is not None:
        for a in existing_items_for_dedup:
            existing_norm.add(_normalize_title_for_dedup(a.title))
    else:
        for a in existing_items_for_dedup:
            existing_norm.add(_normalize_title_for_dedup(a.title))

    # 候補内で正規化タイトルが重複しているものはスコア上位1件だけ残す
    def _dedup(items: list[NewsItem]) -> list[NewsItem]:
        seen_norm: set[str] = set()
        out: list[NewsItem] = []
        for item in items:
            norm = _normalize_title_for_dedup(item.title)
            if _is_duplicate_against_existing(item, existing_items_for_dedup):
                continue
            if norm in existing_norm:
                continue
            if norm in seen_norm:
                continue
            seen_norm.add(norm)
            out.append(item)
        return out

    SOURCE_TO_PAPER_DOMAIN = {
        "Nature": "総合科学",
        "Science Magazine": "総合科学",
        "arXiv cs.AI": "AI・テック",
        "arXiv cs.LG": "AI・テック",
        "arXiv cs.CL": "AI・テック",
        "arXiv cs.CV": "AI・テック",
        "arXiv cs.RO": "AI・テック",
        "arXiv cs.HC": "AI・テック",
        "arXiv cs.IR": "AI・テック",
        "arXiv cs.NE": "AI・テック",
        "arXiv stat.ML": "AI・テック",
        "AI (MDPI)": "AI・テック",
        "arXiv math.OC": "AI・テック",
        "arXiv math.ST": "AI・テック",
        "Frontiers in Artificial Intelligence": "AI・テック",
        "arXiv astro-ph": "物理・宇宙",
        "arXiv quant-ph": "物理・宇宙",
        "arXiv physics.app-ph": "物理・宇宙",
        "arXiv physics.bio-ph": "物理・宇宙",
        "arXiv physics.med-ph": "物理・宇宙",
        "arXiv physics.soc-ph": "物理・宇宙",
        "arXiv math.PR": "物理・宇宙",
        "Frontiers in Sports and Active Living": "筋肉・スポーツ・身体",
        "PLOS ONE": "医療・ヘルスケア",
        "BMJ Open": "医療・ヘルスケア",
        "medRxiv": "医療・ヘルスケア",
        "arXiv q-bio": "医療・ヘルスケア",
        "PubMed (心理学)": "心理学",
        "PubMed (神経科学)": "心理学",
        "Frontiers in Psychology": "心理学",
        "IJERPH (MDPI)": "心理学",
        "arXiv cs.CY": "哲学",
        "Journal of Medical Ethics": "哲学",
        "bioRxiv": "総合科学",
        "SSRN": "経済・ビジネス",
        "IDEAS/RePEc": "経済・ビジネス",
        "arXiv econ.EM": "経済・ビジネス",
        "PubMed (公衆衛生)": "経済・ビジネス",
        "Sensors (MDPI)": "工学・応用",
        "PubMed (AI医療)": "工学・応用",
        "PubMed (栄養・代謝)": "医療・ヘルスケア",
        "arXiv math.DS": "総合科学",
    }
    PAPER_DOMAIN_ORDER = [
        "筋肉・スポーツ・身体",
        "医療・ヘルスケア",
        "心理学",
        "AI・テック",
        "物理・宇宙",
        "経済・ビジネス",
        "総合科学",
        "工学・応用",
        "哲学",
    ]

    # 論文はドメインごとにランキング→各ドメインから複数本選ぶ（設定: RSS_PAPERS_PER_DOMAIN）
    try:
        from app.config import settings as _ap_settings

        papers_per_domain = max(1, int(getattr(_ap_settings, "RSS_PAPERS_PER_DOMAIN", 2)))
        max_total_papers = max(1, int(getattr(_ap_settings, "RSS_MAX_TOTAL_PAPERS_PER_RUN", 18)))
        min_news_slots_cfg = max(0, int(getattr(_ap_settings, "RSS_MIN_NEWS_SLOTS_PER_RUN", 5)))
    except Exception:
        papers_per_domain = 2
        max_total_papers = 18
        min_news_slots_cfg = 5

    # 時間帯スロットによる論文/ニュース比率調整
    slot_paper_ratio = slot_cfg.get("paper_ratio", 0.35)
    slot_min_news = slot_cfg.get("min_news_slots", min_news_slots_cfg)
    # スロット指定の最低ニュース枠とconfig設定の大きい方を採用（安全側）
    min_news_slots = max(slot_min_news, min_news_slots_cfg)
    # 時間帯比率と設定値から論文上限を決定
    slot_paper_budget = max(0, int(max_per_run * slot_paper_ratio))

    # 論文が max_per_run 本まで先に埋まると remaining_slots=0 になりニュースが0本になるため、
    # 一般ニュース用に最低枠を差し引いてから論文の上限を決める。
    if max_per_run >= 2:
        min_news_slots = min(min_news_slots, max_per_run - 1)
    else:
        min_news_slots = 0
    paper_budget = min(max_total_papers, slot_paper_budget, max(0, max_per_run - min_news_slots))

    deduped_papers = _dedup(paper_candidates)
    paper_picks: list[NewsItem] = []
    for domain in PAPER_DOMAIN_ORDER:
        if len(paper_picks) >= paper_budget:
            break
        domain_items = [x for x in deduped_papers if SOURCE_TO_PAPER_DOMAIN.get(x.source) == domain]
        if not domain_items:
            continue
        ranked = rank_and_filter_articles(
            domain_items, trend_keywords, max_articles=max(8, papers_per_domain * 4)
        )
        order = ranked if ranked else domain_items
        slots_here = min(papers_per_domain, paper_budget - len(paper_picks))
        for i in range(min(slots_here, len(order))):
            picked = order[i]
            paper_picks.append(picked)
            deduped_papers = [x for x in deduped_papers if x.id != picked.id]

    # ニュースは従来どおり全体ランキング→上位から選ぶ
    ranked_news = (
        rank_and_filter_articles(news_candidates, trend_keywords, max_articles=max(60, max_per_run * 6))
        if news_candidates
        else []
    )
    deduped_news = _dedup(ranked_news)
    non_papers = deduped_news

    remaining_slots = max_per_run - len(paper_picks)

    # --- 残り枠でニュース記事を選ぶ（従来ロジックを非論文だけに適用） ---
    news_picks: list[NewsItem] = []
    if remaining_slots > 0:
        if remaining_slots >= 6:
            domestics: list[NewsItem] = []
            foreigners: list[NewsItem] = []
            for x in non_papers:
                if is_foreign_article(x.source, x.title, x.summary or ""):
                    foreigners.append(x)
                else:
                    domestics.append(x)

            # 時間帯スロットによる国内/海外比率
            domestic_ratio = slot_cfg.get("domestic_ratio", 0.65)
            domestic_target = max(1, round(remaining_slots * domestic_ratio))
            foreign_target = max(1, remaining_slots - domestic_target)
            domestic_pick = _select_diverse_batch(domestics, domestic_target, max_per_source=2, max_per_category=2)
            foreign_pick = _select_diverse_batch(foreigners, foreign_target, max_per_source=2, max_per_category=2)

            news_picks = domestic_pick + foreign_pick

            if len(news_picks) < remaining_slots:
                remaining = [x for x in non_papers if x not in news_picks]
                extra = _select_diverse_batch(
                    remaining,
                    remaining_slots - len(news_picks),
                    max_per_source=2,
                    max_per_category=2,
                )
                news_picks.extend(extra)
            news_picks = _cap_soft_news(news_picks, max_per_run)[:remaining_slots]
        else:
            news_picks = _select_diverse_batch(
                _cap_soft_news(non_papers, max_per_run),
                remaining_slots,
                max_per_source=2,
                max_per_category=2,
            )

    primary_picks = paper_picks + news_picks

    # 優先順の候補池: 選定分 → 残りニュース → 残り論文（スキップ時に次候補へ）
    reserve_queue: list[NewsItem] = []
    reserve_seen: set[str] = set()

    def _enqueue_reserve(candidates: list[NewsItem]) -> None:
        for x in candidates:
            if x.id not in reserve_seen:
                reserve_queue.append(x)
                reserve_seen.add(x.id)

    _enqueue_reserve(primary_picks)
    _enqueue_reserve(non_papers)
    _enqueue_reserve(deduped_papers)

    logger.info(
        "RSS記事化: 候補池 %d 件（目標 %d 件）",
        len(reserve_queue),
        max_per_run,
    )

    count = 0
    attempts = 0
    soft_news_count = 0
    soft_news_limit = _soft_news_limit(max_per_run)
    from .news_aggregator import NewsAggregator

    NewsAggregator.begin_bulk_update()
    try:
        for item in reserve_queue:
            if count >= max_per_run:
                break
            attempts += 1
            if _is_soft_news_category(item) and soft_news_count >= soft_news_limit:
                _log_save(
                    item.id,
                    item.title,
                    False,
                    error="スポーツ・エンタメ枠上限",
                    source="rss_seed",
                )
                continue
            try:
                ok = process_rss_to_site_article(item, force=force)
                if not ok and not force:
                    ok = process_rss_to_site_article(item, force=True)
                if ok:
                    count += 1
                    if _is_soft_news_category(item):
                        soft_news_count += 1
                    _log_save(item.id, item.title, True, source="rss_seed")
                else:
                    _log_save(
                        item.id,
                        item.title,
                        False,
                        error="スキップ（既存/生成失敗）→次候補へ",
                        source="rss_seed",
                    )
                    logger.info(
                        "RSSスキップ (%d/%d): %s",
                        count,
                        max_per_run,
                        (item.title or "")[:50],
                    )
            except Exception as e:
                _log_save(item.id, item.title, False, error=str(e), source="rss_seed")
            _wait_between_gemini_articles(attempts - 1, len(reserve_queue))
    finally:
        NewsAggregator.end_bulk_update()
    if count < max_per_run:
        logger.warning(
            "RSS記事化: 目標 %d 件に届かず %d 件（候補 %d 件・試行 %d 件）",
            max_per_run,
            count,
            len(reserve_queue),
            attempts,
        )
    # （案A）ローカルで記事化したら Render に通知して一覧キャッシュを更新させる
    if count > 0:
        try:
            from .indexnow_service import flush_indexnow_queue

            flush_indexnow_queue()
        except Exception:
            pass
        try:
            from .render_notifier import notify_render_cache_refresh

            notify_render_cache_refresh(reason=f"rss_added:{count}")
        except Exception:
            pass
    return count


def process_random_rss_articles(rss_items: list[NewsItem], count: int = 3) -> int:
    """
    RSS記事からランダムに count 件を選び、AI解説付きで DB に保存する。
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

    existing_items = load_all()
    existing_norm = {_normalize_title_for_dedup(a.title) for a in existing_items}
    seen_norm = set()
    deduped: list[NewsItem] = []
    for item in candidates:
        norm = _normalize_title_for_dedup(item.title)
        if _is_duplicate_against_existing(item, existing_items):
            continue
        if norm in existing_norm or norm in seen_norm:
            continue
        seen_norm.add(norm)
        deduped.append(item)

    random.shuffle(deduped)
    to_process = deduped[:count]

    n = 0
    for idx, item in enumerate(to_process):
        try:
            if process_rss_to_site_article(item, force=False):
                n += 1
                _log_save(item.id, item.title, True, source="rss_random")
            else:
                _log_save(item.id, item.title, False, error="スキップ（既存または生成失敗）", source="rss_random")
        except Exception as e:
            _log_save(item.id, item.title, False, error=str(e), source="rss_random")
        _wait_between_gemini_articles(idx, len(to_process))
    return n
