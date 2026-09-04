"""Turso (libSQL) ストア - Neon 代替のクラウド SQLite。

TURSO_DATABASE_URL + TURSO_AUTH_TOKEN が設定されていれば有効。
neon_store の各関数から委譲される（呼び出し側の変更を最小化）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_conn_lock = threading.Lock()
_shared_conn = None
_URL = ""
_TOKEN = ""


def _creds() -> tuple[str, str]:
    global _URL, _TOKEN
    if _URL and _TOKEN:
        return _URL, _TOKEN
    try:
        from app.config import settings

        _URL = (getattr(settings, "TURSO_DATABASE_URL", "") or "").strip()
        _TOKEN = (getattr(settings, "TURSO_AUTH_TOKEN", "") or "").strip()
    except Exception:
        _URL = (os.getenv("TURSO_DATABASE_URL", "") or "").strip()
        _TOKEN = (os.getenv("TURSO_AUTH_TOKEN", "") or "").strip()
    # settings が空でも os.environ を再確認（Render 注入タイミング対策）
    if not _URL:
        _URL = (os.getenv("TURSO_DATABASE_URL", "") or "").strip()
    if not _TOKEN:
        _TOKEN = (os.getenv("TURSO_AUTH_TOKEN", "") or "").strip()
    return _URL, _TOKEN


def turso_credentials_configured() -> bool:
    """Turso の URL/TOKEN が環境に入っているか（libsql の import 成否は問わない）。"""
    url, token = _creds()
    return bool(url and token)


def use_turso() -> bool:
    if not turso_credentials_configured():
        return False
    try:
        import libsql  # noqa: F401

        return True
    except Exception as e:
        logger.error(
            "use_turso: TURSO_* は設定済みだが libsql の import に失敗しました (%s)。"
            " Neon へフォールバックしません。requirements.txt の libsql を確認してください。",
            e,
        )
        return False


def reset_turso_connection() -> None:
    global _shared_conn
    with _conn_lock:
        try:
            if _shared_conn is not None:
                _shared_conn.close()
        except Exception:
            pass
        _shared_conn = None


def _connect():
    import libsql

    url, token = _creds()
    # remote-only: database に libsql:// URL を渡す
    return libsql.connect(database=url, auth_token=token)


@contextmanager
def turso_conn(op: str = "unknown"):
    """操作ごとに接続を開く（リモート HTTP 想定。共有接続は壊れやすいので都度接続）。"""
    conn = None
    try:
        conn = _connect()
        yield conn
        try:
            conn.commit()
        except Exception:
            pass
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def turso_init_schema() -> None:
    with turso_conn("init_schema") as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                summary TEXT,
                published TEXT,
                source TEXT DEFAULT '',
                category TEXT DEFAULT '総合',
                image_url TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                has_explanation INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
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
                editorial_take TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for col in ("editorial_take",):
            try:
                conn.execute(f"ALTER TABLE explanations ADD COLUMN {col} TEXT")
            except Exception:
                pass
        try:
            conn.execute("ALTER TABLE articles ADD COLUMN has_explanation INTEGER DEFAULT 0")
        except Exception:
            pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_added_at ON articles(added_at DESC)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_cat_expl ON articles(category, has_explanation, published DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_daily (
                id TEXT PRIMARY KEY CHECK (id = 'latest'),
                payload TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seo_state (
                id TEXT PRIMARY KEY CHECK (id = 'latest'),
                payload TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_vote_counts (
                persona_id INTEGER PRIMARY KEY,
                vote_count INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS policy_topics (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'active',
                generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expert_analyses TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS policy_proposals (
                id TEXT PRIMARY KEY,
                topic_id TEXT,
                title TEXT NOT NULL,
                summary TEXT,
                cost_estimate TEXT,
                effect_prediction TEXT,
                pros TEXT,
                cons TEXT,
                expert_sources TEXT,
                rank INTEGER,
                vote_count INTEGER DEFAULT 0,
                generated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_policy_proposals_topic ON policy_proposals(topic_id, rank)"
        )
        try:
            conn.execute("ALTER TABLE policy_topics ADD COLUMN expert_analyses TEXT")
        except Exception:
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                subcategory TEXT,
                name TEXT NOT NULL,
                value REAL,
                unit TEXT,
                year INTEGER,
                month INTEGER,
                region TEXT,
                source TEXT,
                source_url TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_metrics_category ON metrics(category, year DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS consultations (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                source TEXT DEFAULT 'line',
                source_user TEXT,
                persona_id INTEGER NOT NULL,
                persona_name TEXT NOT NULL,
                persona_emoji TEXT DEFAULT '',
                answer TEXT NOT NULL,
                published_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    logger.info("Turso スキーマ初期化完了")


def _row_to_news_item(row) -> "NewsItem":
    from .rss_service import NewsItem, sanitize_display_text

    def _g(key, default=None):
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            return row[key]
        except Exception:
            return default

    pub_raw = _g("published")
    try:
        pub = datetime.fromisoformat(str(pub_raw).replace("Z", "+00:00")) if pub_raw else datetime.now()
        if pub.tzinfo:
            pub = pub.replace(tzinfo=None)
    except Exception:
        pub = datetime.now()
    added_raw = _g("added_at")
    added_at = None
    try:
        if added_raw:
            added_at = datetime.fromisoformat(str(added_raw).replace("Z", "+00:00"))
            if added_at.tzinfo:
                added_at = added_at.replace(tzinfo=None)
    except Exception:
        added_at = None
    return NewsItem(
        id=_g("id"),
        title=_g("title") or "",
        link=_g("link") or "",
        summary=sanitize_display_text(_g("summary") or ""),
        published=pub,
        source=_g("source") or "",
        category=_g("category") or "総合",
        image_url=_g("image_url"),
        added_at=added_at,
    )


def _published_str(item) -> str:
    p = getattr(item, "published", None)
    if hasattr(p, "isoformat"):
        return p.isoformat()
    return str(p or _now_iso())


def turso_load_by_id(article_id: str):
    with turso_conn("load_by_id") as conn:
        cur = conn.execute(
            "SELECT id, title, link, summary, published, source, category, image_url, added_at "
            "FROM articles WHERE id = ?",
            (article_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    return _row_to_news_item(dict(zip(cols, row)))


def turso_load_all() -> list:
    try:
        from app.config import settings

        cap = max(50, min(int(getattr(settings, "NEON_ARTICLES_LIST_LIMIT", 1200) or 1200), 5000))
    except Exception:
        cap = 1200
    with turso_conn("load_all") as conn:
        cur = conn.execute(
            "SELECT id, title, link, summary, published, source, category, image_url, added_at "
            "FROM articles ORDER BY datetime(COALESCE(added_at, published)) DESC LIMIT ?",
            (cap,),
        )
        rows = cur.fetchall()
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    return [_row_to_news_item(dict(zip(cols, r))) for r in rows]


def turso_load_all_papers_for_site_list(limit: int = 20000) -> list:
    cap = max(1, min(int(limit), 50000))
    with turso_conn("load_all_papers") as conn:
        cur = conn.execute(
            "SELECT id, title, link, summary, published, source, category, image_url, added_at "
            "FROM articles WHERE TRIM(COALESCE(category, '')) IN ('研究・論文', '研究論文') "
            "ORDER BY datetime(COALESCE(added_at, published)) DESC LIMIT ?",
            (cap,),
        )
        rows = cur.fetchall()
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    return [_row_to_news_item(dict(zip(cols, r))) for r in rows]


def turso_save_articles_batch(items) -> int:
    count = 0
    with turso_conn("save_articles_batch") as conn:
        for item in items:
            try:
                conn.execute(
                    """
                    INSERT INTO articles (id, title, link, summary, published, source, category, image_url, added_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title,
                        link=excluded.link,
                        summary=excluded.summary,
                        published=excluded.published,
                        source=excluded.source,
                        category=excluded.category,
                        image_url=excluded.image_url
                    """,
                    (
                        item.id,
                        item.title,
                        item.link,
                        (item.summary or "")[:4000],
                        _published_str(item),
                        item.source,
                        item.category,
                        item.image_url,
                        _now_iso(),
                    ),
                )
                count += 1
            except Exception as e:
                logger.warning("turso_save_articles_batch: %s 失敗: %s", getattr(item, "id", "?"), e)
    return count


def turso_save_article(item) -> bool:
    try:
        turso_save_articles_batch([item])
        return True
    except Exception as e:
        logger.warning("turso_save_article 失敗: %s", e)
        return False


def turso_delete_article(article_id: str) -> bool:
    try:
        with turso_conn("delete_article") as conn:
            conn.execute("DELETE FROM explanations WHERE article_id = ?", (article_id,))
            cur = conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
            return cur.rowcount > 0
    except Exception as e:
        logger.warning("turso_delete_article 失敗: %s", e)
        return False


def _is_bad_fallback_cache(blocks: list) -> bool:
    if not isinstance(blocks, list) or len(blocks) != 2:
        return False
    types = [b.get("type") for b in blocks if isinstance(b, dict)]
    if types != ["text", "explain"]:
        return False
    explain_content = next(
        (b.get("content", "") for b in blocks if isinstance(b, dict) and b.get("type") == "explain"),
        "",
    )
    return any(p in explain_content for p in ("構造化に失敗", "通常の解説を表示", "しばらくしてから再度"))


def turso_get_cached(article_id: str) -> Optional[dict]:
    with turso_conn("get_cached") as conn:
        cur = conn.execute(
            "SELECT inline_blocks, personas, display_persona_ids, "
            "quick_understand, vote_data, paper_graph, paper_quiz, deep_insights, editorial_take "
            "FROM explanations WHERE article_id = ?",
            (article_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    cols = [
        "inline_blocks",
        "personas",
        "display_persona_ids",
        "quick_understand",
        "vote_data",
        "paper_graph",
        "paper_quiz",
        "deep_insights",
        "editorial_take",
    ]
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
    if (
        display_persona_ids is not None
        and isinstance(display_persona_ids, list)
        and len(display_persona_ids) == 3
        and isinstance(personas, list)
        and len(personas) == 3
    ):
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
    result["editorial_take"] = str(d.get("editorial_take") or "")
    return result


def turso_save_cache(
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
    editorial_take: str | None = None,
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

    now = _now_iso()
    with turso_conn("save_cache") as conn:
        conn.execute(
            """
            INSERT INTO explanations
                (article_id, inline_blocks, personas, display_persona_ids,
                 quick_understand, vote_data, paper_graph, paper_quiz, deep_insights, editorial_take, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
                inline_blocks=excluded.inline_blocks,
                personas=excluded.personas,
                display_persona_ids=excluded.display_persona_ids,
                quick_understand=excluded.quick_understand,
                vote_data=excluded.vote_data,
                paper_graph=excluded.paper_graph,
                paper_quiz=excluded.paper_quiz,
                deep_insights=excluded.deep_insights,
                editorial_take=excluded.editorial_take,
                created_at=excluded.created_at
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
                str(editorial_take or ""),
                now,
            ),
        )
        conn.execute("UPDATE articles SET has_explanation = 1 WHERE id = ?", (article_id,))


def turso_delete_cache(article_id: str) -> bool:
    try:
        with turso_conn("delete_cache") as conn:
            cur = conn.execute("DELETE FROM explanations WHERE article_id = ?", (article_id,))
            deleted = cur.rowcount > 0
            if deleted:
                conn.execute("UPDATE articles SET has_explanation = 0 WHERE id = ?", (article_id,))
        return deleted
    except Exception as e:
        logger.warning("turso_delete_cache 失敗: %s", e)
        return False


def turso_get_cached_article_ids() -> set:
    with turso_conn("get_cached_article_ids") as conn:
        cur = conn.execute("SELECT id FROM articles WHERE has_explanation = 1")
        rows = cur.fetchall()
    return {r[0] for r in rows}


def turso_get_cached_article_ids_ordered() -> list:
    with turso_conn("get_cached_article_ids_ordered") as conn:
        cur = conn.execute(
            "SELECT id FROM articles WHERE has_explanation = 1 "
            "ORDER BY datetime(COALESCE(added_at, published)) DESC"
        )
        rows = cur.fetchall()
    return [r[0] for r in rows]


def turso_get_related_tags_bulk(article_ids: list, *, max_tags_per_article: int = 3) -> dict:
    # Neon 版と同様、現状タグテーブルは無いので空を返す
    return {aid: [] for aid in (article_ids or [])}


def turso_query_papers_page(page: int, per_page: int) -> tuple:
    page = max(1, int(page))
    per_page = max(1, min(int(per_page), 100))
    offset = (page - 1) * per_page
    with turso_conn("query_papers_page") as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE TRIM(COALESCE(category,'')) IN ('研究・論文','研究論文') "
            "AND has_explanation = 1"
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, title, link, summary, published, source, category, image_url, added_at "
            "FROM articles WHERE TRIM(COALESCE(category,'')) IN ('研究・論文','研究論文') "
            "AND has_explanation = 1 "
            "ORDER BY datetime(COALESCE(added_at, published)) DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    return [_row_to_news_item(dict(zip(cols, r))) for r in rows], int(total or 0)


def turso_query_news_page(page: int, per_page: int) -> tuple:
    page = max(1, int(page))
    per_page = max(1, min(int(per_page), 100))
    offset = (page - 1) * per_page
    with turso_conn("query_news_page") as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE TRIM(COALESCE(category,'')) NOT IN ('研究・論文','研究論文') "
            "AND has_explanation = 1"
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, title, link, summary, published, source, category, image_url, added_at "
            "FROM articles WHERE TRIM(COALESCE(category,'')) NOT IN ('研究・論文','研究論文') "
            "AND has_explanation = 1 "
            "ORDER BY datetime(COALESCE(added_at, published)) DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()
    cols = ["id", "title", "link", "summary", "published", "source", "category", "image_url", "added_at"]
    return [_row_to_news_item(dict(zip(cols, r))) for r in rows], int(total or 0)


def turso_ai_daily_get() -> Optional[dict]:
    with turso_conn("ai_daily_get") as conn:
        row = conn.execute("SELECT payload FROM ai_daily WHERE id = 'latest'").fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def turso_ai_daily_save(data: dict) -> None:
    blob = json.dumps(data, ensure_ascii=False)
    with turso_conn("ai_daily_save") as conn:
        conn.execute(
            """
            INSERT INTO ai_daily (id, payload, updated_at) VALUES ('latest', ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (blob, _now_iso()),
        )


def turso_persona_vote_increment(persona_id: int) -> int:
    with turso_conn("persona_vote_increment") as conn:
        conn.execute(
            """
            INSERT INTO persona_vote_counts (persona_id, vote_count, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(persona_id) DO UPDATE SET
                vote_count = vote_count + 1,
                updated_at = excluded.updated_at
            """,
            (persona_id, _now_iso()),
        )
        row = conn.execute(
            "SELECT vote_count FROM persona_vote_counts WHERE persona_id = ?",
            (persona_id,),
        ).fetchone()
    return int(row[0]) if row else 1


def turso_persona_vote_get_all() -> dict:
    with turso_conn("persona_vote_get_all") as conn:
        rows = conn.execute("SELECT persona_id, vote_count FROM persona_vote_counts").fetchall()
    return {r[0]: r[1] for r in rows}


def turso_policy_topic_upsert(
    topic_id: str,
    title: str,
    description: str = "",
    expert_analyses: list | None = None,
) -> None:
    analyses_json = json.dumps(expert_analyses or [], ensure_ascii=False)
    with turso_conn("policy_topic_upsert") as conn:
        conn.execute(
            """
            INSERT INTO policy_topics (id, title, description, status, expert_analyses, generated_at)
            VALUES (?, ?, ?, 'active', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                expert_analyses=excluded.expert_analyses,
                generated_at=excluded.generated_at
            """,
            (topic_id, title, description, analyses_json, _now_iso()),
        )


def turso_policy_proposals_save(topic_id: str, proposals: list) -> None:
    with turso_conn("policy_proposals_save") as conn:
        conn.execute("DELETE FROM policy_proposals WHERE topic_id = ?", (topic_id,))
        for p in proposals:
            pros_json = json.dumps(p.get("pros", []), ensure_ascii=False)
            cons_json = json.dumps(p.get("cons", []), ensure_ascii=False)
            sources_json = json.dumps(p.get("expert_sources", []), ensure_ascii=False)
            proposal_id = f"{topic_id}_{p.get('rank', 0)}"
            conn.execute(
                """
                INSERT INTO policy_proposals
                    (id, topic_id, title, summary, cost_estimate, effect_prediction,
                     pros, cons, expert_sources, rank, vote_count, generated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    summary=excluded.summary,
                    cost_estimate=excluded.cost_estimate,
                    effect_prediction=excluded.effect_prediction,
                    pros=excluded.pros,
                    cons=excluded.cons,
                    expert_sources=excluded.expert_sources,
                    rank=excluded.rank,
                    generated_at=excluded.generated_at
                """,
                (
                    proposal_id,
                    topic_id,
                    p.get("title", ""),
                    p.get("summary", ""),
                    p.get("cost_estimate", ""),
                    p.get("effect_prediction", ""),
                    pros_json,
                    cons_json,
                    sources_json,
                    p.get("rank", 0),
                    _now_iso(),
                ),
            )


def turso_policy_proposals_get(topic_id: str) -> list:
    with turso_conn("policy_proposals_get") as conn:
        rows = conn.execute(
            "SELECT id, topic_id, title, summary, cost_estimate, effect_prediction, "
            "pros, cons, expert_sources, rank, vote_count "
            "FROM policy_proposals WHERE topic_id = ? ORDER BY rank",
            (topic_id,),
        ).fetchall()
    cols = [
        "id",
        "topic_id",
        "title",
        "summary",
        "cost_estimate",
        "effect_prediction",
        "pros",
        "cons",
        "expert_sources",
        "rank",
        "vote_count",
    ]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        for k in ("pros", "cons", "expert_sources"):
            try:
                d[k] = json.loads(d[k]) if d.get(k) else []
            except Exception:
                d[k] = []
        out.append(d)
    return out


def turso_policy_topics_get_active() -> list:
    with turso_conn("policy_topics_get_active") as conn:
        rows = conn.execute(
            "SELECT id, title, description, status, expert_analyses, generated_at "
            "FROM policy_topics WHERE status = 'active' ORDER BY generated_at DESC"
        ).fetchall()
    cols = ["id", "title", "description", "status", "expert_analyses", "generated_at"]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        try:
            d["expert_analyses"] = json.loads(d["expert_analyses"]) if d.get("expert_analyses") else []
        except Exception:
            d["expert_analyses"] = []
        out.append(d)
    return out


def turso_policy_vote_increment(proposal_id: str) -> int:
    with turso_conn("policy_vote_increment") as conn:
        conn.execute(
            "UPDATE policy_proposals SET vote_count = COALESCE(vote_count,0) + 1 WHERE id = ?",
            (proposal_id,),
        )
        row = conn.execute(
            "SELECT vote_count FROM policy_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def turso_policy_vote_counts_get(topic_id: str) -> dict:
    with turso_conn("policy_vote_counts_get") as conn:
        rows = conn.execute(
            "SELECT id, vote_count FROM policy_proposals WHERE topic_id = ?",
            (topic_id,),
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def turso_metrics_upsert(rows: list) -> int:
    count = 0
    with turso_conn("metrics_upsert") as conn:
        for d in rows:
            try:
                conn.execute(
                    """
                    INSERT INTO metrics
                        (category, subcategory, name, value, unit, year, month, region, source, source_url, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        d.get("category"),
                        d.get("subcategory"),
                        d.get("name"),
                        d.get("value"),
                        d.get("unit"),
                        d.get("year"),
                        d.get("month"),
                        d.get("region"),
                        d.get("source"),
                        d.get("source_url"),
                        _now_iso(),
                    ),
                )
                count += 1
            except Exception as e:
                logger.warning("turso_metrics_upsert: 行スキップ(%s): %s", d.get("name"), e)
    return count


def turso_metrics_query(
    category: str | None = None,
    year: int | None = None,
    limit: int = 200,
) -> list:
    sql = (
        "SELECT id, category, subcategory, name, value, unit, year, month, region, source, source_url, updated_at "
        "FROM metrics WHERE 1=1"
    )
    params: list = []
    if category:
        sql += " AND category = ?"
        params.append(category)
    if year is not None:
        sql += " AND year = ?"
        params.append(year)
    sql += " ORDER BY year DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    with turso_conn("metrics_query") as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    cols = [
        "id",
        "category",
        "subcategory",
        "name",
        "value",
        "unit",
        "year",
        "month",
        "region",
        "source",
        "source_url",
        "updated_at",
    ]
    return [dict(zip(cols, r)) for r in rows]


def turso_metrics_search(q: str, limit: int = 100) -> list:
    like = f"%{(q or '').strip()}%"
    with turso_conn("metrics_search") as conn:
        rows = conn.execute(
            "SELECT id, category, subcategory, name, value, unit, year, month, region, source, source_url, updated_at "
            "FROM metrics WHERE name LIKE ? OR category LIKE ? OR COALESCE(subcategory,'') LIKE ? "
            "ORDER BY year DESC LIMIT ?",
            (like, like, like, max(1, min(int(limit), 500))),
        ).fetchall()
    cols = [
        "id",
        "category",
        "subcategory",
        "name",
        "value",
        "unit",
        "year",
        "month",
        "region",
        "source",
        "source_url",
        "updated_at",
    ]
    return [dict(zip(cols, r)) for r in rows]


def turso_metrics_categories() -> list:
    with turso_conn("metrics_categories") as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) as cnt FROM metrics "
            "WHERE category IS NOT NULL AND category != '' "
            "GROUP BY category ORDER BY cnt DESC"
        ).fetchall()
    return [{"category": r[0], "count": r[1]} for r in rows]


def turso_save_consultation(
    cid: str,
    question: str,
    source: str,
    source_user: str | None,
    persona_id: int,
    persona_name: str,
    persona_emoji: str,
    answer: str,
    published_at: str,
) -> None:
    with turso_conn("save_consultation") as conn:
        conn.execute(
            """INSERT INTO consultations
               (id, question, source, source_user, persona_id, persona_name, persona_emoji, answer, published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cid, question, source, source_user, persona_id, persona_name, persona_emoji, answer, published_at),
        )


def turso_get_consultations(limit: int = 30) -> list[dict]:
    with turso_conn("get_consultations") as conn:
        rows = conn.execute(
            "SELECT id, question, source, source_user, persona_id, persona_name, persona_emoji, answer, published_at "
            "FROM consultations ORDER BY published_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    cols = [
        "id",
        "question",
        "source",
        "source_user",
        "persona_id",
        "persona_name",
        "persona_emoji",
        "answer",
        "published_at",
    ]
    return [dict(zip(cols, r)) for r in rows]
