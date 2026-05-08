"""Neon Postgres ストア - 記事・解説を Neon に永続化。
Firestore / SQLite の代替として DATABASE_URL が設定されている場合に使用する。"""
import json
import logging
import os
import threading
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_pool = None
_pool_lock = threading.Lock()

_DATABASE_URL: str = ""


def _get_database_url() -> str:
    global _DATABASE_URL
    if _DATABASE_URL:
        return _DATABASE_URL
    try:
        from app.config import settings
        _DATABASE_URL = getattr(settings, "DATABASE_URL", "") or ""
    except Exception:
        _DATABASE_URL = os.getenv("DATABASE_URL", "")
    return _DATABASE_URL


def use_neon() -> bool:
    url = _get_database_url()
    if not url:
        return False
    try:
        import psycopg2  # noqa: F401
        return True
    except ModuleNotFoundError:
        return False


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        import psycopg2.pool
        url = _get_database_url()
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=url)
        return _pool


def _conn():
    """コネクションプールからコネクションを取得するコンテキストマネージャ。"""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        pool = _get_pool()
        conn = pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)

    return _ctx()


def neon_init_schema():
    """テーブル・インデックスを作成（冪等）。起動時に呼ぶ。"""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS articles (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    summary TEXT,
                    published TIMESTAMPTZ,
                    source TEXT DEFAULT '',
                    category TEXT DEFAULT '総合',
                    image_url TEXT,
                    added_at TIMESTAMPTZ DEFAULT NOW(),
                    has_explanation BOOLEAN DEFAULT FALSE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS explanations (
                    article_id TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
                    inline_blocks TEXT NOT NULL,
                    personas TEXT,
                    display_persona_ids TEXT,
                    quick_understand TEXT,
                    vote_data TEXT,
                    paper_graph TEXT,
                    paper_quiz TEXT,
                    deep_insights TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_added_at ON articles(added_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_cat_expl ON articles(category, has_explanation, published DESC)")


# --- ヘルパー ---

def _row_to_news_item(row: dict) -> "NewsItem":
    from .rss_service import NewsItem, sanitize_display_text

    pub = row.get("published")
    if pub is None:
        pub = datetime.now()
    elif hasattr(pub, "replace"):
        pub = pub.replace(tzinfo=None)

    added_at = row.get("added_at")
    if added_at is not None and hasattr(added_at, "replace"):
        added_at = added_at.replace(tzinfo=None)

    return NewsItem(
        id=row["id"],
        title=row.get("title", ""),
        link=row.get("link", ""),
        summary=sanitize_display_text(row.get("summary") or ""),
        published=pub,
        source=row.get("source") or "",
        category=row.get("category") or "総合",
        image_url=row.get("image_url"),
        added_at=added_at,
    )


def _published_dt(item) -> Optional[datetime]:
    p = item.published
    if p is None:
        return None
    if hasattr(p, "replace"):
        return p.replace(tzinfo=None) if p.tzinfo is not None else p
    try:
        return datetime.fromisoformat(str(p))
    except Exception:
        return None


# --- articles ---

def neon_load_by_id(article_id: str):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, link, summary, published, source, category, image_url, added_at "
                "FROM articles WHERE id = %s",
                (article_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    return _row_to_news_item(dict(zip(cols, row)))


def neon_load_all() -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, link, summary, published, source, category, image_url, added_at "
                "FROM articles ORDER BY added_at DESC NULLS LAST, published DESC NULLS LAST"
            )
            rows = cur.fetchall()
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    return [_row_to_news_item(dict(zip(cols, r))) for r in rows]


def neon_load_all_papers_for_site_list(limit: int = 20000) -> list:
    """論文トップ用: 研究・論文かつ has_explanation=true を added_at 降順で取得。"""
    cap = max(1, min(int(limit), 50000))
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, link, summary, published, source, category, image_url, added_at "
                "FROM articles "
                "WHERE category = '研究・論文' AND has_explanation = TRUE "
                "ORDER BY added_at DESC NULLS LAST, published DESC NULLS LAST "
                "LIMIT %s",
                (cap,),
            )
            rows = cur.fetchall()
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    return [_row_to_news_item(dict(zip(cols, r))) for r in rows]


def neon_save_articles_batch(items) -> int:
    count = 0
    with _conn() as conn:
        with conn.cursor() as cur:
            for item in items:
                try:
                    cur.execute(
                        """
                        INSERT INTO articles (id, title, link, summary, published, source, category, image_url, added_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            title = EXCLUDED.title,
                            link = EXCLUDED.link,
                            summary = EXCLUDED.summary,
                            published = EXCLUDED.published,
                            source = EXCLUDED.source,
                            category = EXCLUDED.category,
                            image_url = EXCLUDED.image_url,
                            added_at = NOW()
                        """,
                        (
                            item.id,
                            item.title,
                            item.link,
                            (item.summary or "")[:4000],
                            _published_dt(item),
                            item.source or "",
                            item.category or "総合",
                            item.image_url,
                        ),
                    )
                    count += 1
                except Exception as e:
                    logger.warning("neon_save_articles_batch: %s のUPSERT失敗: %s", item.id, e)
    return count


def neon_save_article(item) -> bool:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO articles (id, title, link, summary, published, source, category, image_url, added_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        link = EXCLUDED.link,
                        summary = EXCLUDED.summary,
                        published = EXCLUDED.published,
                        source = EXCLUDED.source,
                        category = EXCLUDED.category,
                        image_url = EXCLUDED.image_url,
                        added_at = NOW()
                    """,
                    (
                        item.id,
                        item.title,
                        item.link,
                        (item.summary or "")[:4000],
                        _published_dt(item),
                        item.source or "",
                        item.category or "総合",
                        item.image_url,
                    ),
                )
        return True
    except Exception as e:
        logger.warning("neon_save_article 失敗: %s", e)
        return False


def neon_delete_article(article_id: str) -> bool:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM articles WHERE id = %s", (article_id,))
                return cur.rowcount > 0
    except Exception as e:
        logger.warning("neon_delete_article 失敗: %s", e)
        return False


# --- explanations ---

def _is_bad_fallback_cache(blocks: list) -> bool:
    if not blocks or len(blocks) != 2:
        return False
    types = [b.get("type") for b in blocks if isinstance(b, dict)]
    if types != ["text", "explain"]:
        return False
    explain_content = next(
        (b.get("content", "") for b in blocks if isinstance(b, dict) and b.get("type") == "explain"), ""
    )
    return any(p in explain_content for p in ("構造化に失敗", "通常の解説を表示", "しばらくしてから再度"))


def neon_get_cached(article_id: str) -> Optional[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT inline_blocks, personas, display_persona_ids, "
                "quick_understand, vote_data, paper_graph, paper_quiz, deep_insights "
                "FROM explanations WHERE article_id = %s",
                (article_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    cols = ["inline_blocks", "personas", "display_persona_ids",
            "quick_understand", "vote_data", "paper_graph", "paper_quiz", "deep_insights"]
    d = dict(zip(cols, row))

    try:
        blocks = json.loads(d["inline_blocks"])
    except Exception:
        return None
    if _is_bad_fallback_cache(blocks):
        return None

    try:
        display_persona_ids = json.loads(d["display_persona_ids"]) if d.get("display_persona_ids") else None
    except Exception:
        display_persona_ids = None
    try:
        personas = json.loads(d["personas"]) if d.get("personas") else None
    except Exception:
        personas = None

    if (display_persona_ids is not None and isinstance(display_persona_ids, list)
            and len(display_persona_ids) == 3
            and isinstance(personas, list) and len(personas) == 3):
        result = {"blocks": blocks, "personas": personas, "display_persona_ids": display_persona_ids}
    else:
        if not isinstance(personas, list):
            personas = []
        personas = (personas + [""] * 14)[:14]
        result = {"blocks": blocks, "personas": personas}

    for key in ("quick_understand", "vote_data", "paper_graph", "paper_quiz", "deep_insights"):
        raw = d.get(key)
        if not raw:
            continue
        try:
            result[key] = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            pass

    return result


def neon_save_cache(
    article_id: str,
    blocks: list,
    personas: list,
    *,
    display_persona_ids: list | None = None,
    quick_understand: dict | None = None,
    vote_data: dict | None = None,
    paper_graph: dict | None = None,
    paper_quiz: dict | None = None,
    deep_insights: dict | None = None,
):
    _PERSONAS_COUNT = 14
    if display_persona_ids is not None and len(display_persona_ids) == 3 and len(personas) == 3:
        personas_json = json.dumps(personas, ensure_ascii=False)
        ids_json = json.dumps(display_persona_ids, ensure_ascii=False)
    else:
        while len(personas) < _PERSONAS_COUNT:
            personas.append("")
        personas_json = json.dumps(personas[:_PERSONAS_COUNT], ensure_ascii=False)
        ids_json = None

    def _j(v):
        return json.dumps(v, ensure_ascii=False) if v else None

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO explanations
                    (article_id, inline_blocks, personas, display_persona_ids,
                     quick_understand, vote_data, paper_graph, paper_quiz, deep_insights, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (article_id) DO UPDATE SET
                    inline_blocks = EXCLUDED.inline_blocks,
                    personas = EXCLUDED.personas,
                    display_persona_ids = EXCLUDED.display_persona_ids,
                    quick_understand = EXCLUDED.quick_understand,
                    vote_data = EXCLUDED.vote_data,
                    paper_graph = EXCLUDED.paper_graph,
                    paper_quiz = EXCLUDED.paper_quiz,
                    deep_insights = EXCLUDED.deep_insights,
                    created_at = NOW()
                """,
                (
                    article_id,
                    json.dumps(blocks, ensure_ascii=False),
                    personas_json,
                    ids_json,
                    _j(quick_understand),
                    _j(vote_data),
                    _j(paper_graph),
                    _j(paper_quiz),
                    _j(deep_insights),
                ),
            )
            cur.execute(
                "UPDATE articles SET has_explanation = TRUE WHERE id = %s",
                (article_id,),
            )


def neon_delete_cache(article_id: str) -> bool:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM explanations WHERE article_id = %s", (article_id,))
                deleted = cur.rowcount > 0
                if deleted:
                    cur.execute(
                        "UPDATE articles SET has_explanation = FALSE WHERE id = %s",
                        (article_id,),
                    )
        return deleted
    except Exception as e:
        logger.warning("neon_delete_cache 失敗: %s", e)
        return False


def neon_get_cached_article_ids() -> set:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM articles WHERE has_explanation = TRUE")
            rows = cur.fetchall()
    return {r[0] for r in rows}


def neon_get_cached_article_ids_ordered() -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM articles WHERE has_explanation = TRUE "
                "ORDER BY added_at DESC NULLS LAST"
            )
            rows = cur.fetchall()
    return [r[0] for r in rows]


def neon_get_related_tags_bulk(article_ids: list, *, max_tags_per_article: int = 3) -> dict:
    if not article_ids:
        return {}
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT article_id, paper_graph FROM explanations WHERE article_id = ANY(%s)",
                (list(article_ids),),
            )
            rows = cur.fetchall()
    results = {}
    for article_id, pg_raw in rows:
        try:
            pg = json.loads(pg_raw) if isinstance(pg_raw, str) else pg_raw
            if not isinstance(pg, dict):
                continue
            raw_tags = pg.get("related_tags", [])
            if not isinstance(raw_tags, list):
                continue
            tags = [str(t).strip() for t in raw_tags if str(t).strip()][:max_tags_per_article]
            results[article_id] = tags
        except Exception:
            continue
    return results


def neon_query_papers_page(page: int, per_page: int) -> tuple:
    page = max(1, int(page or 1))
    per_page = max(1, int(per_page or 1))
    offset = (page - 1) * per_page
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, link, summary, published, source, category, image_url, added_at "
                "FROM articles "
                "WHERE category = '研究・論文' AND has_explanation = TRUE "
                "ORDER BY published DESC NULLS LAST "
                "LIMIT %s OFFSET %s",
                (per_page, offset),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) FROM articles WHERE category = '研究・論文' AND has_explanation = TRUE"
            )
            total = cur.fetchone()[0]
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    items = [_row_to_news_item(dict(zip(cols, r))) for r in rows]
    return items, total


def neon_query_news_page(page: int, per_page: int) -> tuple:
    page = max(1, int(page or 1))
    per_page = max(1, int(per_page or 1))
    offset = (page - 1) * per_page
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, link, summary, published, source, category, image_url, added_at "
                "FROM articles "
                "WHERE has_explanation = TRUE AND category != '研究・論文' "
                "ORDER BY published DESC NULLS LAST "
                "LIMIT %s OFFSET %s",
                (per_page, offset),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) FROM articles WHERE has_explanation = TRUE AND category != '研究・論文'"
            )
            total = cur.fetchone()[0]
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    items = [_row_to_news_item(dict(zip(cols, r))) for r in rows]
    return items, total
