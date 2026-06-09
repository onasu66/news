"""Claude Code が選定した記事リストを既存パイプラインで記事化する

curated_articles.json（プロジェクトルートに置く）を読み込み、
NewsItem に変換して process_rss_to_site_article に渡す。

JSON フォーマット:
[
  {
    "title": "記事タイトル",
    "url": "https://...",
    "summary": "記事の要約（任意）",
    "source": "メディア名",
    "category": "テクノロジー",
    "published": "2026-04-26T10:00:00",  // 省略可
    "image_url": null                     // 省略可
  },
  ...
]
"""
import json
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .rss_service import NewsItem

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

CURATED_FILE = Path(__file__).resolve().parent.parent.parent / "curated_articles.json"
HISTORY_FILE = Path(__file__).resolve().parent.parent.parent / "curated_history.json"

_VALID_CATEGORIES = {"国内", "国際", "テクノロジー", "政治・社会", "スポーツ", "エンタメ", "研究・論文"}
# 研究・論文サイト（news に入っても URL から研究・論文へ補正）
_RESEARCH_URL_MARKERS = (
    "arxiv.org",
    "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov/pubmed",
    "biorxiv.org",
    "medrxiv.org",
    "sciencedaily.com",
    "eurekalert.org",
    "plos.org",
    "frontiersin.org",
    "springer.com/article",
    "wiley.com/doi",
    "cell.com/",
    "science.org/doi",
    "nature.com/articles/",
    "nature.com/nature/articles/",
    "doi.org/",
)


def _resolve_curated_category(url: str, category: str) -> str:
    """URL が研究系なら category を研究・論文に補正（Claude の誤分類対策）。"""
    u = (url or "").lower()
    if category == "研究・論文":
        return category
    for marker in _RESEARCH_URL_MARKERS:
        if marker in u:
            if category != "研究・論文":
                logger.info("URL補正: %s → 研究・論文 (%s)", category, url[:80])
            return "研究・論文"
    return category


_CATEGORY_MAP = {
    "社会": "政治・社会",
    "政策": "政治・社会",
    "経済": "政治・社会",
    "AI": "テクノロジー",
    "AI・テクノロジー": "テクノロジー",
    "テック": "テクノロジー",
    "科学": "研究・論文",
    "研究": "研究・論文",
    "論文": "研究・論文",
    "学術": "研究・論文",
    "サイエンス": "研究・論文",
    "paper": "研究・論文",
    "papers": "研究・論文",
    "research": "研究・論文",
    "research paper": "研究・論文",
    "science": "研究・論文",
    "ビジネス": "政治・社会",
    "環境": "政治・社会",
}


# ── 重複履歴の管理 ────────────────────────────────────────────────────────────

def _history_max_entries() -> int:
    try:
        from app.config import settings as _s

        return max(50, int(getattr(_s, "CURATED_HISTORY_MAX", 300)))
    except Exception:
        return 300


def _history_lookback_days() -> int:
    try:
        from app.config import settings as _s

        return max(1, int(getattr(_s, "CURATED_HISTORY_LOOKBACK_DAYS", 14)))
    except Exception:
        return 14


def _load_history_records() -> list[dict]:
    """履歴を [{"url": "...", "at": "..."}] 形式で返す（旧フォーマット互換）。"""
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []

    out: list[dict] = []
    # 旧形式（URL文字列配列）は「直近N件のみ有効」にして重複判定を緩める
    if data and all(isinstance(x, str) for x in data):
        tail = data[-_history_max_entries():]
        now_iso = datetime.now(JST).replace(tzinfo=None).isoformat()
        for u in tail:
            su = str(u).strip()
            if su:
                out.append({"url": su, "at": now_iso})
        return out

    for row in data:
        if not isinstance(row, dict):
            continue
        u = str(row.get("url", "")).strip()
        if not u:
            continue
        at = str(row.get("at", "")).strip()
        out.append({"url": u, "at": at})
    return out


def _load_history() -> set[str]:
    """重複除外に使う「有効期間内」の URL セットを返す。"""
    records = _load_history_records()
    if not records:
        return set()
    now = datetime.now(JST).replace(tzinfo=None)
    lookback = _history_lookback_days()
    active: set[str] = set()
    for r in records:
        at_raw = r.get("at", "")
        try:
            at = datetime.fromisoformat(at_raw) if at_raw else now
        except Exception:
            at = now
        # 直近 lookback 日の履歴だけ重複判定に使う（古いURLは再利用可）
        if (now - at).days <= lookback:
            active.add(str(r.get("url", "")).strip())
    return active


def _save_history(urls: set[str]) -> None:
    """選定済み URL を履歴ファイルに保存（時刻付き・上限件数付き）。"""
    if not urls:
        return
    try:
        existing = _load_history_records()
        now_iso = datetime.now(JST).replace(tzinfo=None).isoformat()
        for u in urls:
            su = str(u).strip()
            if su:
                existing.append({"url": su, "at": now_iso})

        # URL単位で最新だけ残す
        dedup_latest: dict[str, str] = {}
        for r in existing:
            u = str(r.get("url", "")).strip()
            at = str(r.get("at", "")).strip()
            if not u:
                continue
            prev = dedup_latest.get(u)
            if (not prev) or (at and at > prev):
                dedup_latest[u] = at

        merged = [{"url": u, "at": dedup_latest[u]} for u in dedup_latest.keys()]
        merged.sort(key=lambda x: x.get("at", ""), reverse=True)
        merged = merged[: _history_max_entries()]
        HISTORY_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("履歴保存に失敗: %s", e)


# ── JSON → NewsItem 変換 ──────────────────────────────────────────────────────

def load_curated_articles(path: Optional[Path] = None) -> list[NewsItem]:
    """curated_articles.json を読み込んで NewsItem リストに変換。重複 URL は除外。"""
    fp = path or CURATED_FILE
    if not fp.exists():
        logger.warning("curated_articles.json が見つかりません: %s", fp)
        return []
    try:
        raw = fp.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("JSON のルートがリストでない")
    except Exception as e:
        logger.error("curated_articles.json の読み込みに失敗: %s", e)
        return []

    history = _load_history()
    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for entry in data:
        url = (entry.get("url") or entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not title or not url:
            continue
        # 重複 URL（履歴・今回リスト内）はスキップ
        if url in history or url in seen_urls:
            logger.debug("重複スキップ: %s", url)
            continue
        seen_urls.add(url)

        # ID は URL + タイトルの MD5（"cc-" プレフィックス）
        item_id = "cc-" + hashlib.md5(f"{url}{title}".encode()).hexdigest()[:14]

        pub_str = (entry.get("published") or "").strip()
        try:
            published = datetime.fromisoformat(pub_str)
            if published.tzinfo:
                published = published.astimezone(JST).replace(tzinfo=None)
        except Exception:
            published = datetime.now(JST).replace(tzinfo=None)

        raw_cat = (entry.get("category") or "").strip()
        category = _CATEGORY_MAP.get(raw_cat, _CATEGORY_MAP.get(raw_cat.lower(), raw_cat))
        if category not in _VALID_CATEGORIES:
            logger.warning("未知のカテゴリ '%s' -> 'テクノジー' に変換", raw_cat)
            category = "テクノロジー"
        category = _resolve_curated_category(url, category)

        summary = (entry.get("summary") or "").strip()
        try:
            from app.config import settings as _s

            min_sum = max(80, int(getattr(_s, "CURATED_MIN_SUMMARY_CHARS", 280)))
        except Exception:
            min_sum = 280
        if len(summary) < min_sum:
            logger.info("要約が短すぎるため除外 (%d字): %s", len(summary), title[:50])
            continue

        items.append(NewsItem(
            id=item_id,
            title=title,
            link=url,
            summary=summary,
            published=published,
            source=entry.get("source") or "Claude Code選定",
            category=category,
            image_url=entry.get("image_url") or None,
        ))

    logger.info("curated_articles.json: %d件読み込み（重複除外後 %d件）", len(data), len(items))
    return items


# ── メイン処理 ────────────────────────────────────────────────────────────────

def process_curated_articles(path: Optional[Path] = None, max_per_run: int = 30) -> int:
    """
    curated_articles.json の記事を既存パイプラインで記事化する。
    成功件数を返す。処理済み URL は curated_history.json に追記する。
    """
    from .article_processor import process_rss_to_site_article
    from .save_history import add_entry as _log_save
    from .explanation_cache import get_cached_article_ids

    items = load_curated_articles(path)
    if not items:
        logger.info("処理する記事がありません")
        return 0

    # すでにAI解説生成済みの記事はスキップ
    cached_ids = get_cached_article_ids()
    to_process = [x for x in items if x.id not in cached_ids][:max_per_run]

    if not to_process:
        logger.info("全件すでに記事化済みです")
        return 0

    logger.info("記事化開始: %d件", len(to_process))
    processed_urls: set[str] = set()
    count = 0

    from .news_aggregator import NewsAggregator

    # 本文フェッチ前: 要約が極端に短いものだけ弾く。
    # 400〜600字の curated 要約は本文取得後に400字判定＋途中切れ除外（process_rss_to_site_article 内）。
    pre_filtered: list = []
    for item in to_process:
        is_paper = (item.category or "").strip() == "研究・論文"
        summary_len = len((item.summary or "").strip())
        min_pre = 360 if is_paper else 200
        if summary_len < min_pre:
            logger.info("[PRE-SKIP] 要約が短すぎる (%d字): %s", summary_len, (item.title or "")[:60])
            _log_save(item.id, item.title, False, error="要約不足(pre-check)", source="curated")
        else:
            pre_filtered.append(item)
    if len(pre_filtered) < len(to_process):
        logger.info("事前フィルタ: %d件 → %d件", len(to_process), len(pre_filtered))
    to_process = pre_filtered

    if not to_process:
        logger.info("事前チェックで全件スキップ")
        return 0

    NewsAggregator.begin_bulk_update()
    try:
        for item in to_process:
            try:
                ok = process_rss_to_site_article(item, force=False)
                if ok:
                    count += 1
                    processed_urls.add(item.link)
                    _log_save(item.id, item.title, True, source="curated")
                    logger.info("[OK] %s", item.title[:60])
                else:
                    _log_save(item.id, item.title, False,
                              error="スキップ（既存または生成失敗）", source="curated")
                    logger.warning("[SKIP] %s", item.title[:60])
            except Exception as e:
                _log_save(item.id, item.title, False, error=str(e), source="curated")
                logger.error("[ERR] %s: %s", item.title[:60], e)
    finally:
        NewsAggregator.end_bulk_update()

    # 処理済み URL を履歴に記録（次回の重複除外に使う）
    if processed_urls:
        _save_history(processed_urls)

    logger.info("記事化完了: %d / %d 件", count, len(to_process))
    # （案A）ローカルで記事化したら Render に通知して一覧キャッシュを更新させる
    if count > 0:
        try:
            from .indexnow_service import flush_indexnow_queue

            flush_indexnow_queue()
        except Exception:
            pass
        try:
            from .render_notifier import notify_render_cache_refresh

            notify_render_cache_refresh(reason=f"curated_added:{count}")
        except Exception:
            pass
    return count
