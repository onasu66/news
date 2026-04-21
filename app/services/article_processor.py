"""RSS記事をAI解説付きのサイト記事に変換するパイプライン"""
import re
from datetime import datetime
from .rss_service import NewsItem, sanitize_display_text, JST
from .translate_service import is_foreign_article, translate_and_rewrite, translate_title_to_japanese, text_mainly_japanese, FOREIGN_SOURCES
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
    if item.source in FOREIGN_SOURCES and (not text_mainly_japanese(title_ja) or not text_mainly_japanese(summary_ja)):
        return False  # 海外は日本語にならない場合は保存しない（無駄なAPI連打はしない）

    # タイトルは「元のタイトルをベースに、事実を変えず、誇張せず、必要なら少しだけ分かりやすく」整える
    # 論文（研究・論文）は見出し加工を避け、元タイトルを基本そのまま使う
    if item.category != "研究・論文":
        title_ja = _rewrite_news_title(title_ja)

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
    if body:
        body_clean = sanitize_display_text(body)[:40000]
        # 英語本文は日本語に翻訳してから反映（約3分で読める分量になるようAIで調整）
        if is_foreign_article(item.source, item.title, body_clean):
            from app.services.translate_service import translate_article_body
            body_clean = translate_article_body(body_clean)
        content = sanitize_display_text(f"{item.title}\n\n{item.summary}\n\n{body_clean}")
    else:
        content = sanitize_display_text(f"{item.title}\n\n{item.summary}")
    data = generate_all_explanations(item.id, item.title, content, category=item.category)
    blocks = data.get("blocks", [])
    personas = data.get("personas", [])
    display_persona_ids = data.get("display_persona_ids")

    if not blocks:
        return False

    # 記事を先に保存してから解説を保存（Firestore で has_explanation を正しく付与するため）
    if not save_article(item):
        return False  # 記事の保存に失敗した場合は成功にしない
    save_cache(
        item.id, blocks, personas,
        display_persona_ids=display_persona_ids,
        quick_understand=data.get("quick_understand"),
        vote_data=data.get("vote_data"),
    )
    return True


def _rewrite_news_title(title: str) -> str:
    """ニュース用タイトルを編集方針で整形（感情の温度を少し上げつつ事実ベース）。"""
    t = (title or "").strip()
    if not t:
        return ""

    # すでに【】が付いていても、煽りを避けるため外す（必要ならAIで自然なタイトルに戻す）
    if t.startswith("【") and "】" in t[:12]:
        t = t.split("】", 1)[1].strip()

    # 長すぎる場合のみ短縮（まずはルールベースで）
    def _shorten(s: str, max_len: int = 36) -> str:
        s = " ".join(s.split())
        return s if len(s) <= max_len else (s[:max_len] + "…")

    # AI が使える場合は「編集者リライト」を1回だけ試す
    try:
        from app.config import settings
        if settings.OPENAI_API_KEY:
            from openai import OpenAI
            from app.utils.openai_compat import create_with_retry
            client = OpenAI(api_key=settings.OPENAI_API_KEY)
            system_prompt = """あなたはニュース編集者です。見出しを整えます。
ルール：
- 元のタイトルをベースにする
- 事実を変えない
- 過激な煽りは禁止（衝撃/悲報/暴露/真相/裏側 など）
- 不安・期待・驚きなどの感情ニュアンスを「ほんの少し」加える
- 思わず続きが気になる言い回しにする
- 長いタイトルは短く、スマホで見切れにくい長さにする
- 出力はタイトル1行のみ（引用符や説明不要）"""
            user_prompt = f"元タイトル：{t}\n\n上のルールで、自然な日本語の見出しに整えてください。"
            resp = create_with_retry(
                client,
                120,
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.55,
            )
            out = (resp.choices[0].message.content or "").strip().strip("「」\"'")
            if out:
                return _shorten(out)
    except Exception:
        pass

    return _shorten(t)


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
    if uncached:
        ranked = rank_and_filter_articles(uncached, trend_keywords, max_articles=15)
        force = False
    else:
        ranked = rank_and_filter_articles(rss_items, trend_keywords, max_articles=15)
        force = True

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


def process_new_rss_articles(
    rss_items: list[NewsItem],
    max_per_run: int = 5,
    trend_keywords: list[str] | None = None,
    existing_articles: list[NewsItem] | None = None,
) -> int:
    """
    RSS記事を Autocomplete スコアリング → 軽量フィルタ → 同一内容は1本に → 上位N件をAI処理して掲載。

    existing_articles を渡すと load_all() を呼ばずに既存タイトルで重複排除（Firestore 読取削減）。
    """
    if not rss_items:
        return 0
    cached_ids = get_cached_article_ids()
    uncached = [x for x in rss_items if x.id not in cached_ids]

    from .keyword_scorer import rank_and_filter_articles

    # 新規候補があれば新規を優先、なければ既存も含めて上書き取り込み（force=True）
    base_candidates = uncached if uncached else rss_items
    force = False if uncached else True

    # 論文を増やしたい要件のため「論文とニュースを同じ上位N件で奪い合う」方式は避け、
    # 論文を先に別枠で確保→残り枠をニュースで埋める。
    paper_candidates = [x for x in base_candidates if x.category == "研究・論文"]
    news_candidates = [x for x in base_candidates if x.category != "研究・論文"]

    # 既存掲載記事の正規化タイトル（同じ内容は1本だけにするため）。渡されていれば load_all() しない
    existing_norm = set()
    if existing_articles is not None:
        for a in existing_articles:
            existing_norm.add(_normalize_title_for_dedup(a.title))
    else:
        for a in load_all():
            existing_norm.add(_normalize_title_for_dedup(a.title))

    # 候補内で正規化タイトルが重複しているものはスコア上位1件だけ残す
    def _dedup(items: list[NewsItem]) -> list[NewsItem]:
        seen_norm: set[str] = set()
        out: list[NewsItem] = []
        for item in items:
            norm = _normalize_title_for_dedup(item.title)
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
    except Exception:
        papers_per_domain = 2
        max_total_papers = 18
    paper_budget = min(max_total_papers, max_per_run)

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

            domestic_pick = _select_diverse_batch(domestics, 4, max_per_source=2, max_per_category=2)
            foreign_pick = _select_diverse_batch(foreigners, 2, max_per_source=2, max_per_category=2)

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
            news_picks = news_picks[:remaining_slots]
        else:
            news_picks = _select_diverse_batch(non_papers, remaining_slots, max_per_source=2, max_per_category=2)

    to_process = paper_picks + news_picks

    # max_per_run 本を狙うため、足りない場合は条件を緩めて補充する。
    # 1) まだ未選出のニュース候補（dedup済み）から順に補充
    # 2) それでも不足なら、論文候補の残りから補充
    selected_ids = {x.id for x in to_process}
    if len(to_process) < max_per_run:
        for x in non_papers:
            if len(to_process) >= max_per_run:
                break
            if x.id in selected_ids:
                continue
            to_process.append(x)
            selected_ids.add(x.id)
    if len(to_process) < max_per_run:
        for x in deduped_papers:
            if len(to_process) >= max_per_run:
                break
            if x.id in selected_ids:
                continue
            to_process.append(x)
            selected_ids.add(x.id)

    count = 0
    for item in to_process:
        try:
            ok = process_rss_to_site_article(item, force=force)
            # force=false のままだと「実際には既存キャッシュがある/途中状態」が原因でスキップ扱いになることがある。
            # 選んだ分は記事化する方針に寄せるため、失敗時は force=true で1回だけ上書き再試行する。
            if not ok and not force:
                ok = process_rss_to_site_article(item, force=True)
            if ok:
                count += 1
                _log_save(item.id, item.title, True, source="rss_seed")
            else:
                _log_save(
                    item.id,
                    item.title,
                    False,
                    error="スキップ（既存/生成失敗）※force=true再試行でも失敗",
                    source="rss_seed",
                )
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


