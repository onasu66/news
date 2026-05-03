"""掲載記事の永続キャッシュ（SQLite / Firestore） - 過去記事を蓄積しニュースサイトとして表示"""
import json
import sqlite3
from pathlib import Path
from datetime import datetime

from .rss_service import NewsItem, sanitize_display_text

# SQLite: 一覧用の取得上限（Firestore は全件メモリスナップショット）
def _sqlite_articles_list_limit() -> int:
    try:
        from app.config import settings

        return max(1, int(getattr(settings, "SQLITE_ARTICLES_LIST_LIMIT", 100000)))
    except Exception:
        return 100000

def _use_firestore():
    try:
        from .firestore_store import use_firestore
        return use_firestore()
    except Exception:
        return False

_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "articles.db"


def _get_conn():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                summary TEXT,
                published TEXT,
                source TEXT,
                category TEXT,
                image_url TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def load_by_id(article_id: str) -> NewsItem | None:
    """IDで1件取得"""
    if _use_firestore():
        from .firestore_store import firestore_load_by_id
        return firestore_load_by_id(article_id)
    _init_db()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, link, summary, published, source, category, image_url FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
    if not row:
        return None
    try:
        pub = datetime.fromisoformat(row["published"]) if row["published"] else datetime.now()
    except Exception:
        pub = datetime.now()
    return NewsItem(
        id=row["id"],
        title=row["title"],
        link=row["link"],
        summary=sanitize_display_text(row["summary"] or ""),
        published=pub,
        source=row["source"] or "",
        category=row["category"] or "総合",
        image_url=row["image_url"],
    )


def load_all_processed(processed_ids: set[str]) -> list[NewsItem]:
    """AI処理済みの記事のみ読み込み（ミドルマン解説付き＝サイト記事として掲載済み）"""
    all_items = load_all()
    if not processed_ids:
        return []
    return [x for x in all_items if x.id in processed_ids]


def load_all() -> list[NewsItem]:
    """保存済みの全記事を読み込み（新しい順）"""
    if _use_firestore():
        from .firestore_store import firestore_load_all
        return firestore_load_all()
    _init_db()
    items = []
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, link, summary, published, source, category, image_url "
            "FROM articles "
            "ORDER BY published DESC, added_at DESC "
            "LIMIT ?",
            (_sqlite_articles_list_limit(),),
        ).fetchall()
    for row in rows:
        try:
            pub = datetime.fromisoformat(row["published"]) if row["published"] else datetime.now()
        except Exception:
            pub = datetime.now()
        items.append(NewsItem(
            id=row["id"],
            title=row["title"],
            link=row["link"],
            summary=sanitize_display_text(row["summary"] or ""),
            published=pub,
            source=row["source"] or "",
            category=row["category"] or "総合",
            image_url=row["image_url"],
        ))
    return items


def load_papers_for_site_list() -> list[NewsItem]:
    """
    論文トップ（/）用: 研究・論文を取得。解説付きID（get_cached_article_ids）で絞り、
    Firestore の has_explanation 未設定でも落ちない。IDメタが空のときはカテゴリのみで救済。
    """
    try:
        from app.config import settings

        limit = max(50, min(int(getattr(settings, "PAPERS_LIST_MAX", 20000)), 50000))
    except Exception:
        limit = 20000
    if _use_firestore():
        from .firestore_store import firestore_load_all_papers_for_site_list

        items = firestore_load_all_papers_for_site_list(limit=limit)
    else:
        items = _sqlite_load_papers_for_site_list(limit=limit)
    return sorted(
        items,
        key=lambda x: x.added_at or x.published or datetime.min,
        reverse=True,
    )[:limit]


def _sqlite_load_papers_for_site_list(limit: int) -> list[NewsItem]:
    from .explanation_cache import get_cached_article_ids

    try:
        processed = get_cached_article_ids()
    except Exception:
        processed = set()
    fetch_cap = min(max(limit * 150, 8000), 100000)
    _init_db()
    items: list[NewsItem] = []
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, title, link, summary, published, source, category, image_url, added_at
            FROM articles
            WHERE category = ?
            ORDER BY published DESC, added_at DESC
            LIMIT ?
            """,
            ("研究・論文", fetch_cap),
        ).fetchall()
    for row in rows:
        if processed and row["id"] not in processed:
            continue
        try:
            pub = datetime.fromisoformat(row["published"]) if row["published"] else datetime.now()
        except Exception:
            pub = datetime.now()
        added_at = None
        try:
            if row["added_at"]:
                added_at = datetime.fromisoformat(str(row["added_at"]))
        except Exception:
            added_at = None
        items.append(
            NewsItem(
                id=row["id"],
                title=row["title"],
                link=row["link"],
                summary=sanitize_display_text(row["summary"] or ""),
                published=pub,
                source=row["source"] or "",
                category=row["category"] or "研究・論文",
                image_url=row["image_url"],
                added_at=added_at,
            )
        )
        if len(items) >= limit:
            break
    return items


def save_articles_batch(items: list[NewsItem]) -> int:
    """記事を一括保存。保存できた件数を返す"""
    if _use_firestore():
        from .firestore_store import firestore_save_articles_batch
        return firestore_save_articles_batch(items)
    _init_db()
    count = 0
    with _get_conn() as conn:
        for item in items:
            try:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO articles (id, title, link, summary, published, source, category, image_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.id,
                        item.title,
                        item.link,
                        item.summary[:4000] if item.summary else "",
                        item.published.isoformat() if hasattr(item.published, "isoformat") else str(item.published),
                        item.source,
                        item.category,
                        item.image_url,
                    ),
                )
                if cur.rowcount > 0:
                    count += 1
            except Exception:
                pass
        conn.commit()
    return count


def save_article(item: NewsItem) -> bool:
    """記事を1件保存（既存は上書き＝再取り込みで一覧の先頭に反映）"""
    if _use_firestore():
        from .firestore_store import firestore_save_article
        return firestore_save_article(item)
    _init_db()
    try:
        with _get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO articles (id, title, link, summary, published, source, category, image_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.title,
                    item.link,
                    item.summary[:4000] if item.summary else "",
                    item.published.isoformat() if hasattr(item.published, "isoformat") else str(item.published),
                    item.source,
                    item.category,
                    item.image_url,
                ),
            )
            conn.commit()
        return True
    except Exception:
        return False


def delete_article(article_id: str) -> bool:
    """記事を1件削除。存在したらTrue"""
    if _use_firestore():
        from .firestore_store import firestore_delete_article
        return firestore_delete_article(article_id)
    _init_db()
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
        conn.commit()
    return cur.rowcount > 0

