"""
Turso (libSQL) ストア。

neon_store と同じ役割を SQLite 方言で提供する。Turso は libSQL = SQLite 互換のため、
プレースホルダは `?`、真偽値は 0/1、時刻は ISO 文字列で保持する。

接続は turso_serverless（HTTP 経由・純 Python）を使う。ネイティブ拡張を持たないため
Render のビルドで追加のコンパイルが不要。DB-API 2.0 準拠だが threadsafety=1 のため、
接続はスレッドごとに保持する。
"""
import json
import logging
import os
import threading
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_local = threading.local()

# Postgres の NOW() 相当。SQLite は CURRENT_TIMESTAMP が UTC の 'YYYY-MM-DD HH:MM:SS'
_NOW = "CURRENT_TIMESTAMP"


def _get_database_url() -> str:
    return (os.getenv("TURSO_DATABASE_URL", "") or "").strip()


def _get_auth_token() -> str:
    return (os.getenv("TURSO_AUTH_TOKEN", "") or "").strip()


def use_turso() -> bool:
    """TURSO_DATABASE_URL が設定され、SDK が import できれば True。"""
    url = _get_database_url()
    if not url:
        return False
    try:
        import turso_serverless  # noqa: F401

        return True
    except Exception as e:
        logger.warning(
            "use_turso: turso_serverless の import に失敗 (%s: %s) → Turso を使用しません",
            type(e).__name__,
            e,
        )
        return False


def _new_connection():
    import turso_serverless

    return turso_serverless.connect(_get_database_url(), auth_token=_get_auth_token() or None)


def get_connection():
    """スレッドローカルな接続を返す（threadsafety=1 のため共有しない）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _new_connection()
        _local.conn = conn
    return conn


def reset_connection() -> None:
    """接続を破棄する。次回アクセス時に張り直す。"""
    conn = getattr(_local, "conn", None)
    _local.conn = None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _execute(sql: str, params: tuple = (), *, retry: bool = True):
    """
    クエリを実行して (rows, rowcount, lastrowid) を返す。
    HTTP 接続が切れている場合は 1 回だけ張り直して再試行する。
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, params)
        try:
            rows = cur.fetchall()
        except Exception:
            rows = []
        return rows, cur.rowcount, cur.lastrowid
    except Exception as e:
        if retry:
            logger.warning("turso _execute 再試行 (%s: %s)", type(e).__name__, e)
            reset_connection()
            return _execute(sql, params, retry=False)
        raise


def _commit() -> None:
    try:
        get_connection().commit()
    except Exception as e:
        logger.warning("turso commit 失敗: %s", e)
        raise


def _placeholders(n: int) -> str:
    return ",".join(["?"] * n)


# --- 型変換ヘルパー ---

def _to_dt(val) -> Optional[datetime]:
    """DB から返る値（ISO 文字列 / datetime / None）を naive datetime にする。"""
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=None) if val.tzinfo is not None else val
    s = str(val).strip()
    # SQLite の CURRENT_TIMESTAMP は 'YYYY-MM-DD HH:MM:SS'
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
    except Exception:
        return None


def _dt_to_text(val) -> Optional[str]:
    """datetime / 文字列を DB 保存用の ISO 文字列にする。"""
    if val is None:
        return None
    if isinstance(val, datetime):
        v = val.replace(tzinfo=None) if val.tzinfo is not None else val
        return v.isoformat(sep=" ", timespec="seconds")
    return str(val)


def _row_to_news_item(row: dict) -> "object":
    from .rss_service import NewsItem, sanitize_display_text

    pub = _to_dt(row.get("published")) or datetime.now()
    return NewsItem(
        id=row["id"],
        title=row.get("title", ""),
        link=row.get("link", ""),
        summary=sanitize_display_text(row.get("summary") or ""),
        published=pub,
        source=row.get("source") or "",
        category=row.get("category") or "総合",
        image_url=row.get("image_url"),
        added_at=_to_dt(row.get("added_at")),
    )


def _published_text(item) -> Optional[str]:
    return _dt_to_text(getattr(item, "published", None))


_ARTICLE_COLS = [
    "id", "title", "link", "summary", "published",
    "source", "category", "image_url", "added_at",
]
_ARTICLE_SELECT = ", ".join(_ARTICLE_COLS)


# --- スキーマ ---

def init_schema() -> None:
    """テーブル・インデックスを作成（冪等）。起動時に呼ぶ。"""
    stmts = [
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
        """,
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
        """,
        "CREATE INDEX IF NOT EXISTS idx_articles_added_at ON articles(added_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_articles_cat_expl ON articles(category, has_explanation, published DESC)",
        """
        CREATE TABLE IF NOT EXISTS ai_daily (
            id TEXT PRIMARY KEY CHECK (id = 'latest'),
            payload TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS persona_vote_counts (
            persona_id INTEGER PRIMARY KEY,
            vote_count INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS policy_topics (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'active',
            expert_analyses TEXT,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS policy_proposals (
            id TEXT PRIMARY KEY,
            topic_id TEXT REFERENCES policy_topics(id),
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
        """,
        "CREATE INDEX IF NOT EXISTS idx_policy_proposals_topic ON policy_proposals(topic_id, rank)",
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
        """,
        "CREATE INDEX IF NOT EXISTS idx_metrics_category ON metrics(category, year DESC)",
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
        """,
    ]
    for sql in stmts:
        _execute(sql)
    _commit()
    logger.info("turso init_schema: 完了")


# --- articles ---

def load_by_id(article_id: str):
    rows, _, _ = _execute(
        f"SELECT {_ARTICLE_SELECT} FROM articles WHERE id = ?", (article_id,)
    )
    if not rows:
        return None
    return _row_to_news_item(dict(zip(_ARTICLE_COLS, rows[0])))


def _articles_list_limit() -> int:
    try:
        from app.config import settings

        return max(50, min(int(getattr(settings, "NEON_ARTICLES_LIST_LIMIT", 1200) or 1200), 5000))
    except Exception:
        return 1200


def load_all() -> list:
    cap = _articles_list_limit()
    # SQLite は DESC のとき NULL が末尾に来るため NULLS LAST 相当になる
    rows, _, _ = _execute(
        f"SELECT {_ARTICLE_SELECT} FROM articles "
        "ORDER BY added_at DESC, published DESC LIMIT ?",
        (cap,),
    )
    return [_row_to_news_item(dict(zip(_ARTICLE_COLS, r))) for r in rows]


def _papers_category_sql_predicate() -> str:
    """「研究・論文」と中黒無し「研究論文」のみ論文側に載せる。"""
    return "TRIM(COALESCE(category, '')) IN ('研究・論文', '研究論文')"


def load_all_papers_for_site_list(limit: int = 20000) -> list:
    cap = max(1, min(int(limit), 50000))
    pc = _papers_category_sql_predicate()
    rows, _, _ = _execute(
        f"SELECT {_ARTICLE_SELECT} FROM articles WHERE {pc} "
        "ORDER BY added_at DESC, published DESC LIMIT ?",
        (cap,),
    )
    return [_row_to_news_item(dict(zip(_ARTICLE_COLS, r))) for r in rows]


def save_article(item) -> bool:
    """1 件保存（既存は上書き）。"""
    try:
        _execute(
            """
            INSERT INTO articles
                (id, title, link, summary, published, source, category, image_url, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                link = excluded.link,
                summary = excluded.summary,
                published = excluded.published,
                source = excluded.source,
                category = excluded.category,
                image_url = excluded.image_url
            """,
            (
                item.id,
                item.title,
                item.link,
                (item.summary or "")[:4000],
                _published_text(item),
                item.source,
                item.category,
                item.image_url,
                _dt_to_text(getattr(item, "added_at", None)),
            ),
        )
        _commit()
        return True
    except Exception as e:
        logger.warning("turso save_article 失敗 (%s): %s", getattr(item, "id", "?"), e)
        return False


def save_articles_batch(items) -> int:
    """一括保存。新規に追加できた件数を返す（既存は無視）。"""
    count = 0
    for item in items:
        try:
            _, rowcount, _ = _execute(
                """
                INSERT INTO articles
                    (id, title, link, summary, published, source, category, image_url, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    item.id,
                    item.title,
                    item.link,
                    (item.summary or "")[:4000],
                    _published_text(item),
                    item.source,
                    item.category,
                    item.image_url,
                    _dt_to_text(getattr(item, "added_at", None)),
                ),
            )
            if rowcount and rowcount > 0:
                count += 1
        except Exception as e:
            logger.warning("turso save_articles_batch 1件失敗: %s", e)
    if count:
        _commit()
    return count


def delete_article(article_id: str) -> bool:
    _, rowcount, _ = _execute("DELETE FROM articles WHERE id = ?", (article_id,))
    _execute("DELETE FROM explanations WHERE article_id = ?", (article_id,))
    _commit()
    return bool(rowcount and rowcount > 0)


def query_papers_page(page: int, per_page: int) -> tuple:
    page = max(1, int(page or 1))
    per_page = max(1, int(per_page or 1))
    offset = (page - 1) * per_page
    pc = _papers_category_sql_predicate()
    rows, _, _ = _execute(
        f"SELECT {_ARTICLE_SELECT} FROM articles WHERE {pc} "
        "ORDER BY added_at DESC, published DESC LIMIT ? OFFSET ?",
        (per_page, offset),
    )
    total_rows, _, _ = _execute(f"SELECT COUNT(*) FROM articles WHERE {pc}")
    total = total_rows[0][0] if total_rows else 0
    items = [_row_to_news_item(dict(zip(_ARTICLE_COLS, r))) for r in rows]
    return items, total


def query_news_page(page: int, per_page: int) -> tuple:
    page = max(1, int(page or 1))
    per_page = max(1, int(per_page or 1))
    offset = (page - 1) * per_page
    pc = _papers_category_sql_predicate()
    rows, _, _ = _execute(
        f"SELECT {_ARTICLE_SELECT} FROM articles WHERE NOT ({pc}) "
        "ORDER BY published DESC LIMIT ? OFFSET ?",
        (per_page, offset),
    )
    total_rows, _, _ = _execute(f"SELECT COUNT(*) FROM articles WHERE NOT ({pc})")
    total = total_rows[0][0] if total_rows else 0
    items = [_row_to_news_item(dict(zip(_ARTICLE_COLS, r))) for r in rows]
    return items, total


# --- explanations ---

_EXPL_COLS = [
    "inline_blocks", "personas", "display_persona_ids", "quick_understand",
    "vote_data", "paper_graph", "paper_quiz", "deep_insights", "editorial_take",
]
PERSONAS_COUNT = 14


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


def get_cached(article_id: str) -> Optional[dict]:
    rows, _, _ = _execute(
        f"SELECT {', '.join(_EXPL_COLS)} FROM explanations WHERE article_id = ?",
        (article_id,),
    )
    if not rows:
        return None
    d = dict(zip(_EXPL_COLS, rows[0]))

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
        personas = (personas + [""] * PERSONAS_COUNT)[:PERSONAS_COUNT]
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


def save_cache(
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
    if display_persona_ids is not None and len(display_persona_ids) == 3 and len(personas) == 3:
        personas_json = json.dumps(personas, ensure_ascii=False)
        ids_json = json.dumps(display_persona_ids, ensure_ascii=False)
    else:
        while len(personas) < PERSONAS_COUNT:
            personas.append("")
        personas_json = json.dumps(personas[:PERSONAS_COUNT], ensure_ascii=False)
        ids_json = None

    def _j(v):
        return json.dumps(v, ensure_ascii=False) if v else None

    _execute(
        """
        INSERT INTO explanations
            (article_id, inline_blocks, personas, display_persona_ids,
             quick_understand, vote_data, paper_graph, paper_quiz, deep_insights,
             editorial_take, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(article_id) DO UPDATE SET
            inline_blocks = excluded.inline_blocks,
            personas = excluded.personas,
            display_persona_ids = excluded.display_persona_ids,
            quick_understand = excluded.quick_understand,
            vote_data = excluded.vote_data,
            paper_graph = excluded.paper_graph,
            paper_quiz = excluded.paper_quiz,
            deep_insights = excluded.deep_insights,
            editorial_take = excluded.editorial_take,
            created_at = CURRENT_TIMESTAMP
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
        ),
    )
    _execute("UPDATE articles SET has_explanation = 1 WHERE id = ?", (article_id,))
    _commit()


def delete_cache(article_id: str) -> bool:
    try:
        _, rowcount, _ = _execute("DELETE FROM explanations WHERE article_id = ?", (article_id,))
        deleted = bool(rowcount and rowcount > 0)
        if deleted:
            _execute("UPDATE articles SET has_explanation = 0 WHERE id = ?", (article_id,))
        _commit()
        return deleted
    except Exception as e:
        logger.warning("turso delete_cache 失敗: %s", e)
        return False


def get_cached_article_ids() -> set:
    rows, _, _ = _execute("SELECT id FROM articles WHERE has_explanation = 1")
    return {r[0] for r in rows}


def get_cached_article_ids_ordered() -> list:
    rows, _, _ = _execute(
        "SELECT id FROM articles WHERE has_explanation = 1 ORDER BY added_at DESC"
    )
    return [r[0] for r in rows]


def _related_tags_from_raw(raw, max_tags_per_article: int) -> list | None:
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if isinstance(val, dict):
        val = val.get("related_tags", [])
    if not isinstance(val, list):
        return None
    return [str(t).strip() for t in val if str(t).strip()][:max_tags_per_article]


def get_related_tags_bulk(article_ids: list, *, max_tags_per_article: int = 3) -> dict:
    """
    関連タグだけを取得する。paper_graph 全体は記事あたり数 KB になるため、
    SQLite の json_extract で related_tags 配列のみを取り出して転送量を抑える。
    """
    if not article_ids:
        return {}
    ids = list(article_ids)
    results: dict = {}
    ph = _placeholders(len(ids))
    try:
        rows, _, _ = _execute(
            f"SELECT article_id, json_extract(paper_graph, '$.related_tags') "
            f"FROM explanations WHERE article_id IN ({ph}) "
            "AND paper_graph IS NOT NULL AND paper_graph <> ''",
            tuple(ids),
        )
    except Exception as e:
        logger.warning(
            "turso get_related_tags_bulk: json_extract に失敗（全体取得にフォールバック）: %s", e
        )
        rows, _, _ = _execute(
            f"SELECT article_id, paper_graph FROM explanations WHERE article_id IN ({ph})",
            tuple(ids),
        )
    for article_id, raw in rows:
        tags = _related_tags_from_raw(raw, max_tags_per_article)
        if tags is not None:
            results[article_id] = tags
    return results


# --- 日次AIコンテンツ ---

def ai_daily_get() -> Optional[dict]:
    rows, _, _ = _execute("SELECT payload FROM ai_daily WHERE id = 'latest'")
    if not rows or not rows[0][0]:
        return None
    try:
        return json.loads(rows[0][0])
    except Exception:
        return None


def ai_daily_save(data: dict) -> None:
    blob = json.dumps(data, ensure_ascii=False)
    _execute(
        """
        INSERT INTO ai_daily (id, payload, updated_at)
        VALUES ('latest', ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            payload = excluded.payload,
            updated_at = CURRENT_TIMESTAMP
        """,
        (blob,),
    )
    _commit()


# --- キャラ投票 ---

def persona_vote_increment(persona_id: int) -> int:
    _execute(
        """
        INSERT INTO persona_vote_counts (persona_id, vote_count, updated_at)
        VALUES (?, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(persona_id) DO UPDATE SET
            vote_count = persona_vote_counts.vote_count + 1,
            updated_at = CURRENT_TIMESTAMP
        """,
        (persona_id,),
    )
    _commit()
    rows, _, _ = _execute(
        "SELECT vote_count FROM persona_vote_counts WHERE persona_id = ?", (persona_id,)
    )
    return rows[0][0] if rows else 1


def persona_vote_get_all() -> dict:
    rows, _, _ = _execute("SELECT persona_id, vote_count FROM persona_vote_counts")
    return {r[0]: r[1] for r in rows}


# --- 政策トピック & 提案 ---

def policy_topic_upsert(
    topic_id: str,
    title: str,
    description: str = "",
    expert_analyses: list | None = None,
) -> None:
    analyses_json = json.dumps(expert_analyses or [], ensure_ascii=False)
    _execute(
        """
        INSERT INTO policy_topics (id, title, description, status, expert_analyses, generated_at)
        VALUES (?, ?, ?, 'active', ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            expert_analyses = excluded.expert_analyses,
            generated_at = CURRENT_TIMESTAMP
        """,
        (topic_id, title, description, analyses_json),
    )
    _commit()


def policy_proposals_save(topic_id: str, proposals: list) -> None:
    _execute("DELETE FROM policy_proposals WHERE topic_id = ?", (topic_id,))
    for p in proposals:
        _execute(
            """
            INSERT INTO policy_proposals
                (id, topic_id, title, summary, cost_estimate, effect_prediction,
                 pros, cons, expert_sources, rank, vote_count, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                summary = excluded.summary,
                cost_estimate = excluded.cost_estimate,
                effect_prediction = excluded.effect_prediction,
                pros = excluded.pros,
                cons = excluded.cons,
                expert_sources = excluded.expert_sources,
                rank = excluded.rank,
                generated_at = CURRENT_TIMESTAMP
            """,
            (
                f"{topic_id}_{p.get('rank', 0)}",
                topic_id,
                p.get("title", ""),
                p.get("summary", ""),
                p.get("cost_estimate", ""),
                p.get("effect_prediction", ""),
                json.dumps(p.get("pros", []), ensure_ascii=False),
                json.dumps(p.get("cons", []), ensure_ascii=False),
                json.dumps(p.get("expert_sources", []), ensure_ascii=False),
                p.get("rank", 0),
            ),
        )
    _commit()


def policy_proposals_get(topic_id: str) -> list:
    cols = ["id", "topic_id", "title", "summary", "cost_estimate", "effect_prediction",
            "pros", "cons", "expert_sources", "rank", "vote_count"]
    rows, _, _ = _execute(
        f"SELECT {', '.join(cols)} FROM policy_proposals WHERE topic_id = ? ORDER BY rank",
        (topic_id,),
    )
    result = []
    for row in rows:
        d = dict(zip(cols, row))
        for k in ("pros", "cons", "expert_sources"):
            try:
                d[k] = json.loads(d[k]) if d[k] else []
            except Exception:
                d[k] = []
        result.append(d)
    return result


def policy_topics_get_active() -> list:
    cols = ["id", "title", "description", "status", "generated_at", "expert_analyses"]
    rows, _, _ = _execute(
        f"SELECT {', '.join(cols)} FROM policy_topics WHERE status = 'active' "
        "ORDER BY generated_at DESC"
    )
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        try:
            d["expert_analyses"] = json.loads(d["expert_analyses"]) if d.get("expert_analyses") else []
        except Exception:
            d["expert_analyses"] = []
        result.append(d)
    return result


def policy_vote_increment(proposal_id: str) -> int:
    _execute(
        "UPDATE policy_proposals SET vote_count = vote_count + 1 WHERE id = ?",
        (proposal_id,),
    )
    _commit()
    rows, _, _ = _execute(
        "SELECT vote_count FROM policy_proposals WHERE id = ?", (proposal_id,)
    )
    return rows[0][0] if rows else 0


def policy_vote_counts_get(topic_id: str) -> dict:
    rows, _, _ = _execute(
        "SELECT id, vote_count FROM policy_proposals WHERE topic_id = ?", (topic_id,)
    )
    return {r[0]: r[1] for r in rows}


# --- 偉人への相談 ---

def consultation_save(
    cid: str,
    question: str,
    source: str,
    source_user: str | None,
    persona_id: int,
    persona_name: str,
    persona_emoji: str,
    answer: str,
    published_at,
) -> None:
    _execute(
        """
        INSERT INTO consultations
            (id, question, source, source_user, persona_id, persona_name,
             persona_emoji, answer, published_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cid, question, source, source_user, persona_id, persona_name,
            persona_emoji, answer, _dt_to_text(published_at),
        ),
    )
    _commit()


def _metrics_cols() -> list:
    return ["id", "category", "subcategory", "name", "value", "unit",
            "year", "month", "region", "source", "source_url", "updated_at"]


def metrics_upsert(rows: list) -> int:
    if not rows:
        return 0
    count = 0
    for r in rows:
        if hasattr(r, "_asdict"):
            d = r._asdict()
        elif isinstance(r, dict):
            d = r
        else:
            continue
        try:
            _, rowcount, _ = _execute(
                """
                INSERT INTO metrics
                    (category, subcategory, name, value, unit, year, month,
                     region, source, source_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT DO NOTHING
                """,
                (
                    d.get("category", ""),
                    d.get("subcategory"),
                    d.get("name", ""),
                    d.get("value"),
                    d.get("unit"),
                    d.get("year"),
                    d.get("month"),
                    d.get("region"),
                    d.get("source"),
                    d.get("source_url"),
                ),
            )
            count += max(0, rowcount or 0)
        except Exception as e:
            logger.warning("turso metrics_upsert: 行スキップ(%s): %s", d.get("name"), e)
    if count:
        _commit()
    return count


def metrics_query(
    *,
    category: str = "",
    subcategory: str = "",
    name: str = "",
    year: Optional[int] = None,
    limit: int = 200,
    offset: int = 0,
) -> list:
    conds = []
    params: list = []
    if category:
        conds.append("category = ?")
        params.append(category)
    if subcategory:
        conds.append("subcategory = ?")
        params.append(subcategory)
    if name:
        # SQLite の LIKE は ASCII について大文字小文字を区別しない（ILIKE 相当）
        conds.append("name LIKE ?")
        params.append(f"%{name}%")
    if year is not None:
        conds.append("year = ?")
        params.append(year)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params += [limit, offset]
    cols = _metrics_cols()
    rows, _, _ = _execute(
        f"SELECT {', '.join(cols)} FROM metrics {where} "
        "ORDER BY year DESC, id DESC LIMIT ? OFFSET ?",
        tuple(params),
    )
    return [dict(zip(cols, r)) for r in rows]


def metrics_search(q: str, limit: int = 100) -> list:
    cols = _metrics_cols()
    like = f"%{q}%"
    rows, _, _ = _execute(
        f"SELECT {', '.join(cols)} FROM metrics "
        "WHERE name LIKE ? OR category LIKE ? OR subcategory LIKE ? "
        "ORDER BY year DESC, id DESC LIMIT ?",
        (like, like, like, limit),
    )
    return [dict(zip(cols, r)) for r in rows]


def metrics_categories() -> list:
    rows, _, _ = _execute(
        "SELECT category, COUNT(*) as cnt FROM metrics GROUP BY category ORDER BY cnt DESC"
    )
    return [{"category": r[0], "count": r[1]} for r in rows]


def consultations_get(limit: int = 30) -> list:
    cols = ["id", "question", "source", "source_user", "persona_id",
            "persona_name", "persona_emoji", "answer", "published_at"]
    rows, _, _ = _execute(
        f"SELECT {', '.join(cols)} FROM consultations "
        "ORDER BY published_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(zip(cols, r)) for r in rows]
