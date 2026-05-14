"""Neon Postgres ストア - 記事・解説を Neon に永続化する。
DATABASE_URL が設定され psycopg2 が使えるときに有効（未設定時は SQLite）。"""
import json
import logging
import os
import threading
import time
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
        logger.debug("use_neon: DATABASE_URL が未設定のため Neon を使用しません")
        return False
    try:
        import psycopg2  # noqa: F401
        logger.debug("use_neon: True (psycopg2 OK, URL=%s...)", url[:40])
        return True
    except Exception as e:
        logger.warning("use_neon: psycopg2 import 失敗 (%s: %s) → Neon を使用しません", type(e).__name__, e)
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


def _reset_pool() -> None:
    """壊れた/古いプールを捨てて再作成可能な状態にする。"""
    global _pool
    with _pool_lock:
        try:
            if _pool is not None:
                _pool.closeall()
        except Exception:
            pass
        _pool = None


def _is_transient_neon_error(exc: BaseException) -> bool:
    """Neon 側のアイドル切断・ネットワーク瞬断など、再試行に値するエラーか。"""
    msg = str(exc).lower()
    needles = (
        "server closed the connection",
        "connection already closed",
        "ssl connection has been closed unexpectedly",
        "connection reset by peer",
        "broken pipe",
        "could not receive data from server",
        "software caused connection abort",
    )
    if any(n in msg for n in needles):
        return True
    tname = type(exc).__name__
    return tname in ("OperationalError", "InterfaceError")


def reset_neon_connection_pool() -> None:
    """壊れた／アイドル切断済みの接続が残ったプールを捨て、次回から作り直す。"""
    _reset_pool()


def is_neon_transient_connection_error(exc: BaseException) -> bool:
    """外部（記事取得など）から接続切れを判定する。"""
    return _is_transient_neon_error(exc)


def _log_neon_db_op(op: str) -> None:
    """環境変数 LOG_NEON_DB=true で、プール取得直後に操作名を1行ログする（トラフィック調査用）。"""
    v = os.getenv("LOG_NEON_DB", "").strip().lower()
    if v not in ("1", "true", "yes"):
        return
    import threading

    logger.info("NEON_DB op=%s thread=%s", op, threading.current_thread().name)


def _conn(op: str = "unknown"):
    """コネクションプールからコネクションを取得するコンテキストマネージャ。"""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        conn = None
        pool = None
        # Neon 側のアイドル切断後に closed connection が返るケースがあるため、1回だけ再取得を試す
        for attempt in (0, 1):
            try:
                pool = _get_pool()
                conn = pool.getconn()
                if getattr(conn, "closed", 1):
                    try:
                        pool.putconn(conn, close=True)
                    except Exception:
                        pass
                    conn = None
                    _reset_pool()
                    continue
                break
            except Exception:
                if conn is not None and pool is not None:
                    try:
                        pool.putconn(conn, close=True)
                    except Exception:
                        pass
                conn = None
                _reset_pool()
                if attempt == 1:
                    raise
        if conn is None:
            raise RuntimeError("Neon connection could not be established")
        _log_neon_db_op(op)
        try:
            conn.rollback()  # プール返却時の残留トランザクションをクリア
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                if pool is not None:
                    pool.putconn(conn)
            except Exception:
                pass

    return _ctx()


def neon_init_schema():
    """テーブル・インデックスを作成（冪等）。起動時に呼ぶ。"""
    with _conn("init_schema") as conn:
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
                    article_id TEXT PRIMARY KEY,
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_daily (
                    id TEXT PRIMARY KEY CHECK (id = 'latest'),
                    payload TEXT NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)


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
    with _conn("load_by_id") as conn:
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
    """全件読み込み。Neon の一時切断に備え数回まで再試行する。"""
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    last_exc: BaseException | None = None
    for attempt in range(3):
        try:
            with _conn("load_all") as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, title, link, summary, published, source, category, image_url, added_at "
                        "FROM articles ORDER BY added_at DESC NULLS LAST, published DESC NULLS LAST"
                    )
                    rows = cur.fetchall()
            return [_row_to_news_item(dict(zip(cols, r))) for r in rows]
        except Exception as e:
            last_exc = e
            if attempt < 2 and _is_transient_neon_error(e):
                logger.warning("neon_load_all 再試行 (%d/3): %s", attempt + 1, e)
                _reset_pool()
                time.sleep(0.35 * (attempt + 1))
                continue
            raise


def _papers_category_sql_predicate() -> str:
    """トップ論文一覧用: 「研究・論文」と中黒無し「研究論文」のみ論文側に載せる。"""
    return (
        "TRIM(BOTH FROM COALESCE(category, '')) IN ('研究・論文', '研究論文')"
    )


def neon_load_all_papers_for_site_list(limit: int = 20000) -> list:
    """論文トップ用: 研究・論文を added_at 降順で取得。"""
    cap = max(1, min(int(limit), 50000))
    pc = _papers_category_sql_predicate()
    with _conn("load_all_papers_site_list") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, link, summary, published, source, category, image_url, added_at "
                "FROM articles "
                f"WHERE {pc} "
                "ORDER BY added_at DESC NULLS LAST, published DESC NULLS LAST "
                "LIMIT %s",
                (cap,),
            )
            rows = cur.fetchall()
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    return [_row_to_news_item(dict(zip(cols, r))) for r in rows]


def neon_save_articles_batch(items) -> int:
    count = 0
    with _conn("save_articles_batch") as conn:
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
        with _conn("save_article") as conn:
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
        with _conn("delete_article") as conn:
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
    with _conn("get_cached") as conn:
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

    with _conn("save_cache") as conn:
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
        with _conn("delete_cache") as conn:
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
    """長時間アイドル後にプールの接続が Neon 側で切れていると SSL エラーになるため、1回だけリセットして再試行する。"""
    for attempt in (0, 1):
        try:
            with _conn("get_cached_article_ids") as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM articles WHERE has_explanation = TRUE")
                    rows = cur.fetchall()
            return {r[0] for r in rows}
        except Exception as e:
            if attempt == 0 and _is_transient_neon_error(e):
                logger.warning(
                    "neon_get_cached_article_ids: 接続切れのためプールを捨てて再試行します (%s)",
                    e,
                )
                _reset_pool()
                time.sleep(0.25)
                continue
            raise
    raise RuntimeError("neon_get_cached_article_ids: unreachable")


def neon_get_cached_article_ids_ordered() -> list:
    for attempt in (0, 1):
        try:
            with _conn("get_cached_article_ids_ordered") as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM articles WHERE has_explanation = TRUE "
                        "ORDER BY added_at DESC NULLS LAST"
                    )
                    rows = cur.fetchall()
            return [r[0] for r in rows]
        except Exception as e:
            if attempt == 0 and _is_transient_neon_error(e):
                logger.warning(
                    "neon_get_cached_article_ids_ordered: 接続切れのためプールを捨てて再試行します (%s)",
                    e,
                )
                _reset_pool()
                time.sleep(0.25)
                continue
            raise
    raise RuntimeError("neon_get_cached_article_ids_ordered: unreachable")


def neon_get_related_tags_bulk(article_ids: list, *, max_tags_per_article: int = 3) -> dict:
    if not article_ids:
        return {}
    with _conn("get_related_tags_bulk") as conn:
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
    with _conn("query_papers_page") as conn:
        with conn.cursor() as cur:
            pc = _papers_category_sql_predicate()
            cur.execute(
                "SELECT id, title, link, summary, published, source, category, image_url, added_at "
                "FROM articles "
                f"WHERE {pc} "
                "ORDER BY added_at DESC NULLS LAST, published DESC NULLS LAST "
                "LIMIT %s OFFSET %s",
                (per_page, offset),
            )
            rows = cur.fetchall()
            cur.execute(
                f"SELECT COUNT(*) FROM articles WHERE {pc}"
            )
            total = cur.fetchone()[0]
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    items = [_row_to_news_item(dict(zip(cols, r))) for r in rows]
    return items, total


def neon_query_news_page(page: int, per_page: int) -> tuple:
    page = max(1, int(page or 1))
    per_page = max(1, int(per_page or 1))
    offset = (page - 1) * per_page
    with _conn("query_news_page") as conn:
        with conn.cursor() as cur:
            pc = _papers_category_sql_predicate()
            cur.execute(
                "SELECT id, title, link, summary, published, source, category, image_url, added_at "
                "FROM articles "
                f"WHERE NOT ({pc}) "
                "ORDER BY published DESC NULLS LAST "
                "LIMIT %s OFFSET %s",
                (per_page, offset),
            )
            rows = cur.fetchall()
            cur.execute(
                f"SELECT COUNT(*) FROM articles WHERE NOT ({pc})"
            )
            total = cur.fetchone()[0]
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    items = [_row_to_news_item(dict(zip(cols, r))) for r in rows]
    return items, total


# --- 日次AIコンテンツ ---

def neon_ai_daily_get() -> Optional[dict]:
    with _conn("ai_daily_get") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM ai_daily WHERE id = 'latest'")
            row = cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def neon_ai_daily_save(data: dict) -> None:
    blob = json.dumps(data, ensure_ascii=False)
    with _conn("ai_daily_save") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai_daily (id, payload, updated_at)
                VALUES ('latest', %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    updated_at = NOW()
                """,
                (blob,),
            )
