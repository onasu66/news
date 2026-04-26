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


# ── 重複履歴の管理 ────────────────────────────────────────────────────────────

def _load_history() -> set[str]:
    """過去に選定済みの URL セットを返す"""
    if not HISTORY_FILE.exists():
        return set()
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def _save_history(urls: set[str]) -> None:
    """選定済み URL を履歴ファイルに保存（上限 2000 件）"""
    try:
        existing = _load_history()
        merged = list(existing | urls)[-2000:]
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

        items.append(NewsItem(
            id=item_id,
            title=title,
            link=url,
            summary=entry.get("summary") or "",
            published=published,
            source=entry.get("source") or "Claude Code選定",
            category=entry.get("category") or "総合",
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

    # 処理済み URL を履歴に記録（次回の重複除外に使う）
    if processed_urls:
        _save_history(processed_urls)

    logger.info("記事化完了: %d / %d 件", count, len(to_process))
    return count
