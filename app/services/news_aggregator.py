"""ニュース集約・キャッシュサービス"""
from datetime import datetime, timedelta
from typing import Optional

from app.config import settings
from .rss_service import fetch_rss_news, NewsItem
from .trends_service import fetch_trending_searches, TrendItem
from .article_cache import load_all, load_all_processed, load_by_id, save_article
from .article_processor import process_new_rss_articles
from .explanation_cache import get_cached_article_ids

# ジャンル表示順（研究・論文は論文専用ページで表示）
# 「総合」はRSS由来で基本付与されないため、タブを出さない（管理者手動記事などは「すべて」から見える想定）
CATEGORY_ORDER = ["国内", "国際", "テクノロジー", "政治・社会", "スポーツ", "エンタメ", "研究・論文"]

# 論文ページ用：上位ジャンル（ドメイン）の表示順
# - 💪 筋肉・スポーツ・身体
# - 🧬 医療・ヘルスケア
# - AI・テック
# - ⚛️ 物理・宇宙
# - 💰 経済・ビジネス
# - 🧠 心理学（PubMed 検索RSS）
# - 🧪 総合科学
# - 🔬 工学・応用
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

# 論文用：ソース名 → 上位ジャンルのマッピング
SOURCE_TO_PAPER_DOMAIN: dict[str, str] = {
    # 総合科学
    "Nature": "総合科学",
    "Science Magazine": "総合科学",
    # AI・テック
    "arXiv cs.AI": "AI・テック",
    "arXiv cs.LG": "AI・テック",
    "arXiv cs.CL": "AI・テック",
    "arXiv cs.CV": "AI・テック",
    "Frontiers in Artificial Intelligence": "AI・テック",
    # 物理・宇宙
    "arXiv astro-ph": "物理・宇宙",
    "arXiv quant-ph": "物理・宇宙",
    # 筋肉・スポーツ・身体
    "Frontiers in Sports and Active Living": "筋肉・スポーツ・身体",
    # 医療・ヘルスケア
    "PLOS ONE": "医療・ヘルスケア",
    "BMJ Open": "医療・ヘルスケア",
    "medRxiv": "医療・ヘルスケア",
    "arXiv q-bio": "医療・ヘルスケア",
    # 心理学
    "PubMed (心理学)": "心理学",
    "Frontiers in Psychology": "心理学",
    # 哲学・倫理
    "arXiv cs.CY": "哲学",
    "Journal of Medical Ethics": "哲学",
    # 総合科学（プレプリント）
    "bioRxiv": "総合科学",
    # 経済・ビジネス
    "SSRN": "経済・ビジネス",
    "IDEAS/RePEc": "経済・ビジネス",
    # 工学・応用
    "Sensors (MDPI)": "工学・応用",
}

# 論文フィルター（A〜I）用キーワード定義（タイトル・要約からタグ付け）
PAPER_FILTER_KEYWORDS: dict[str, dict[str, list[str]]] = {
    "筋肉・スポーツ・身体": {
        "A": ["hypertrophy", "筋肥大"],
        "B": ["strength", "筋力", "1rm", "one-repetition maximum"],
        "C": ["endurance", "持久力", "vo2max", "vo2 max"],
        "D": ["protein", "タンパク", "たんぱく", "diet", "栄養"],
        "E": ["supplement", "サプリ", "creatine", "クレアチン", "beta-alanine", "ベータアラニン"],
        "F": ["sleep", "睡眠", "recovery", "回復"],
        "G": ["fat loss", "脂肪減少", "weight loss", "減量", "ダイエット"],
        "H": ["testosterone", "ホルモン", "cortisol", "テストステロン"],
        "I": ["injury", "ケガ", "傷害", "rehabilitation", "リハビリ"],
    },
    "心理学": {
        "A": ["randomized", "rct", "randomised", "臨床試験"],
        "B": ["meta-analysis", "systematic review", "メタアナリシス"],
        "C": ["cognitive", "認知", "memory", "記憶"],
        "D": ["depression", "うつ", "anxiety", "不安", "ptsd"],
        "E": ["motivation", "動機", "habit", "習慣", "behavior", "行動"],
        "F": ["stress", "ストレス", "resilience", "レジリエンス"],
        "G": ["adolescent", "青年", "child", "児童"],
        "H": ["intervention", "介入", "therapy", "心理療法"],
        "I": ["neuroimaging", "fmri", "eeg", "脳"],
    },
    "医療・ヘルスケア": {
        "A": ["treatment", "治療", "therapy"],
        "B": ["prevention", "予防"],
        "C": ["epidemiology", "疫学"],
        "D": ["vaccine", "ワクチン", "drug", "新薬"],
        "E": ["diabetes", "糖尿病", "hypertension", "高血圧", "chronic"],
        "F": ["mental health", "メンタル", "うつ", "depression", "anxiety"],
        "G": ["longevity", "寿命", "aging", "アンチエイジング"],
        "H": ["lifestyle", "生活習慣", "diet", "運動", "exercise"],
        "I": ["diagnosis", "診断", "ai diagnosis"],
    },
    "AI・テック": {
        "A": ["gpt", "llm", "large language model"],
        "B": ["diffusion", "stable diffusion", "image generation"],
        "C": ["video generation", "video diffusion"],
        "D": ["reinforcement learning", "強化学習"],
        "E": ["robot", "robotics"],
        "F": ["application", "ビジネス", "use case"],
        "G": ["breakthrough", "state-of-the-art", "sota"],
        "H": ["benchmark", "performance comparison", "accuracy"],
        "I": ["ethics", "倫理", "safety", "安全性"],
    },
    "物理・宇宙": {
        "A": ["galaxy", "銀河", "planet", "惑星", "exoplanet"],
        "B": ["black hole", "ブラックホール"],
        "C": ["quantum", "量子力学"],
        "D": ["cosmology", "ビッグバン", "cosmic"],
        "E": ["particle", "粒子物理"],
        "F": ["telescope", "観測技術"],
        "G": ["mystery", "謎", "unexplained", "new discovery"],
    },
    "経済・ビジネス": {
        "A": ["inflation", "インフレ", "interest rate", "金利"],
        "B": ["stock market", "株式市場", "equity"],
        "C": ["crypto", "cryptocurrency", "bitcoin"],
        "D": ["policy", "中央銀行", "federal reserve", "政府"],
        "E": ["corporate strategy", "企業戦略", "m&a"],
        "F": ["consumer", "行動経済学", "behavioral"],
        "G": ["labor", "雇用", "失業"],
        "H": ["global economy", "グローバル経済", "world economy"],
    },
    "総合科学": {
        # 総合寄りの記事は細かい軸を持たないのでゆるくタグ付け
        "G": ["breakthrough", "新発見", "landmark"],
    },
    "工学・応用": {
        "A": ["robot", "robotics", "automation"],
        "B": ["ai engineering", "control"],
        "C": ["material", "materials", "nanomaterial"],
        "D": ["battery", "solar", "fuel cell"],
        "E": ["semiconductor", "chip"],
        "F": ["5g", "6g", "network"],
        "G": ["mechanical", "fluid"],
        "H": ["infrastructure", "earthquake", "構造"],
        "I": ["biomedical", "bioengineering"],
        "J": ["manufacturing", "3d printing"],
    },
    "哲学": {
        "A": ["ethics", "倫理", "moral", "道徳"],
        "B": ["autonomy", "自律", "consent", "同意"],
        "C": ["justice", "正義", "fairness", "公平"],
        "D": ["consciousness", "意識", "phenomenology", "現象学"],
        "E": ["well-being", "福祉", "flourishing"],
        "F": ["rights", "権利", "義務", "duty"],
        "G": ["meaning", "意味", "existence", "存在"],
    },
}


def _detect_paper_filter(domain: str, title: str, summary: str) -> str:
    """論文1件に対して A〜I などのフィルターコードを1つ付与（最初にマッチしたもの）"""
    conf = PAPER_FILTER_KEYWORDS.get(domain)
    if not conf:
        return ""
    text = f"{title or ''} {summary or ''}".lower()
    for code, keywords in conf.items():
        for kw in keywords:
            if kw.lower() in text:
                return code
    return ""


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
# 閲覧時の一覧キャッシュは期限で破棄しない（更新イベント時のみ再取得）
# - 通常閲覧時の Firestore 読み取りを最小化する
# - 再起動時・force_refresh 実行時には再取得される
CACHE_NEVER_EXPIRE = True


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
        force_refresh時のみRSS取得→AI処理を実行し、一覧を再取得する。
        閲覧時はTTLで破棄しない（更新イベント駆動）。
        """
        if force_refresh or not cls._news_cache:
            processed_ids = get_cached_article_ids()
            # 読み取り削減: load_all の回数を最小化し、force_refresh 時も既存スナップショットを使い回す
            all_items = load_all()
            cached = [x for x in all_items if x.id in processed_ids][:PAGE_DISPLAY_LIMIT]
            if cached and not force_refresh:
                cls._news_cache = sorted(cached, key=lambda x: x.published or datetime.min, reverse=True)
                cls._last_updated = datetime.now()
                return cls._news_cache
            if force_refresh:
                # RSS/AI の途中で例外が出ても、ここまで保存された記事を一覧に載せる（finally で必ず再読込）
                try:
                    news = fetch_rss_news()
                    if news:
                        trends = cls.get_trends(force_refresh=True)
                        trend_keywords = [t.keyword for t in trends]
                        min_added = max(0, getattr(settings, "RSS_MIN_ADDED_PER_REFRESH", 30))
                        max_loops = max(1, getattr(settings, "RSS_REFRESH_MAX_LOOPS", 3))
                        batch_max = max(5, int(getattr(settings, "RSS_PROCESS_MAX_PER_BATCH", 32)))
                        added_run = 0
                        existing_items_snapshot = list(all_items)
                        for _ in range(max_loops):
                            batch = process_new_rss_articles(
                                news,
                                max_per_run=batch_max,
                                trend_keywords=trend_keywords,
                                existing_articles=existing_items_snapshot,
                            )
                            added_run += batch
                            if min_added <= 0 or added_run >= min_added or batch == 0:
                                break
                finally:
                    processed_ids = get_cached_article_ids()
                    # force_refresh 終了後の再読は 1 回だけ
                    all_items = load_all()
            cls._news_cache = sorted(
                [x for x in all_items if x.id in processed_ids][:PAGE_DISPLAY_LIMIT],
                key=lambda x: x.published or datetime.min,
                reverse=True,
            )
            cls._last_updated = datetime.now()
        return cls._news_cache

    @classmethod
    def sync_list_cache_from_db(cls) -> None:
        """
        RSS・AI は回さず、解説付き ID（Firestore なら _meta/cache 1 読）で一覧キャッシュを揃える。
        ID 集合が前回と同じなら記事ドキュメントは読まない。増えた ID だけ load_by_id し、
        load_all（最大 ~2000 読）を毎回避けて無料枠を守る。
        """
        try:
            from .explanation_cache import invalidate_ids_cache

            invalidate_ids_cache()
        except Exception:
            pass
        processed_ids = get_cached_article_ids()
        if not processed_ids:
            cls._news_cache = []
            cls._last_updated = datetime.now()
            return

        cached_ids = {x.id for x in cls._news_cache}
        if processed_ids == cached_ids and cls._news_cache:
            cls._last_updated = datetime.now()
            return

        if not cls._news_cache:
            items = []
            for nid in processed_ids:
                item = load_by_id(nid)
                if item:
                    items.append(item)
            cls._news_cache = sorted(
                items,
                key=lambda x: x.published or datetime.min,
                reverse=True,
            )[:PAGE_DISPLAY_LIMIT]
            cls._last_updated = datetime.now()
            return

        new_ids = processed_ids - cached_ids
        gone_ids = cached_ids - processed_ids
        items = [x for x in cls._news_cache if x.id not in gone_ids]
        for nid in new_ids:
            item = load_by_id(nid)
            if item:
                items.append(item)
        cls._news_cache = sorted(
            items,
            key=lambda x: x.published or datetime.min,
            reverse=True,
        )[:PAGE_DISPLAY_LIMIT]
        cls._last_updated = datetime.now()

    @classmethod
    def get_news_by_category(cls, force_refresh: bool = False, page: int = 1) -> tuple[list[tuple[str, list[NewsItem]]], dict]:
        """
        ジャンルごとにグループ化したニュース一覧。
        page=1 は最新、page=2 は過去記事...。
        戻り値: (news_by_category, pagination_info)
        pagination_info: {page, per_page, total, total_pages, has_prev, has_next}
        """
        per_page = ITEMS_PER_PAGE
        if not force_refresh:
            try:
                from .firestore_store import use_firestore, firestore_query_news_page

                if use_firestore():
                    page_items, total = firestore_query_news_page(page=page, per_page=per_page)
                    total_pages = max(1, (int(total) + per_page - 1) // per_page) if total else 1
                    page = max(1, min(int(page), total_pages))

                    by_cat: dict[str, list[NewsItem]] = {}
                    for item in page_items:
                        by_cat.setdefault(item.category, []).append(item)
                    news_by_category = [(cat, by_cat.get(cat, [])) for cat in CATEGORY_ORDER]

                    pagination = {
                        "page": page,
                        "per_page": per_page,
                        "total": int(total) if total else len(page_items),
                        "total_pages": total_pages,
                        "has_prev": page > 1,
                        "has_next": page < total_pages,
                    }
                    return news_by_category, pagination
            except Exception:
                # Firestore 側で何か起きても、従来の挙動へフォールバック
                pass

        news = cls.get_news(force_refresh)
        total = len(news)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))

        start = (page - 1) * per_page
        page_items = news[start : start + per_page]

        by_cat: dict[str, list[NewsItem]] = {}
        for item in page_items:
            by_cat.setdefault(item.category, []).append(item)
        # 全ジャンルを表示順で出す（記事が0件のジャンルもタブ・パネルを表示）
        news_by_category = [(cat, by_cat.get(cat, [])) for cat in CATEGORY_ORDER]

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
    def get_papers_by_category(cls, force_refresh: bool = False, page: int = 1) -> tuple[list[tuple[str, list[NewsItem]]], dict]:
        """
        論文（研究・論文ジャンル）を上位ジャンル（ドメイン）ごとにグループ化。
        戻り値: (papers_by_category, pagination_info)
        papers_by_category は (domain_name, list[NewsItem]) のリスト（表示順は PAPER_DOMAIN_ORDER）。
        """
        # Firestore なら /papers は get_news() 経由で load_all(最大2000件) しない。
        # has_explanation & category で直接クエリしてページングすることで、初回遷移を速くする。
        if not force_refresh:
            try:
                from .firestore_store import use_firestore, firestore_query_papers_page

                if use_firestore():
                    per_page = ITEMS_PER_PAGE
                    page_items, total = firestore_query_papers_page(page=page, per_page=per_page)
                    total_pages = max(1, (int(total) + per_page - 1) // per_page) if total else 1
                    page = max(1, min(int(page), total_pages))

                    by_domain: dict[str, list[NewsItem]] = {}
                    for item in page_items:
                        domain = SOURCE_TO_PAPER_DOMAIN.get(item.source, "総合科学")
                        item.paper_filter_code = _detect_paper_filter(domain, item.title, item.summary)
                        by_domain.setdefault(domain, []).append(item)

                    domains_order = [d for d in PAPER_DOMAIN_ORDER if d in by_domain]
                    papers_by_category = [(dom, by_domain.get(dom, [])) for dom in domains_order]

                    pagination = {
                        "page": page,
                        "per_page": per_page,
                        "total": int(total) if total else len(page_items),
                        "total_pages": total_pages,
                        "has_prev": page > 1,
                        "has_next": page < total_pages,
                    }
                    return papers_by_category, pagination
            except Exception:
                # Firestore 側で何か起きても、従来の挙動へフォールバック
                pass

        news = cls.get_news(force_refresh)
        papers = [a for a in news if a.category == "研究・論文"]
        papers.sort(key=lambda x: x.published or datetime.min, reverse=True)
        total = len(papers)
        per_page = ITEMS_PER_PAGE
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        page_items = papers[start : start + per_page]
        domains_with_articles = {SOURCE_TO_PAPER_DOMAIN.get(p.source, "総合科学") for p in papers}
        domains_order = [d for d in PAPER_DOMAIN_ORDER if d in domains_with_articles]
        by_domain: dict[str, list[NewsItem]] = {}
        for item in page_items:
            domain = SOURCE_TO_PAPER_DOMAIN.get(item.source, "総合科学")
            item.paper_filter_code = _detect_paper_filter(domain, item.title, item.summary)
            by_domain.setdefault(domain, []).append(item)
        papers_by_category = [(dom, by_domain.get(dom, [])) for dom in domains_order]
        pagination = {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        }
        return papers_by_category, pagination

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
