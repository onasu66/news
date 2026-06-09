"""投票サービス - キャラ投票・政策提案投票の集計。

設計方針:
- 書き込み: DB に atomic increment（即時）
- 読み取り: in-memory キャッシュを返す（DB クエリなし）
- キャッシュ更新: refresh_vote_cache() を記事スケジューラ完了時に呼ぶ
- 重複制限: localStorage/Cookie でクライアント側管理（サーバーは集計専用）
- SQLite フォールバック: Neon 未設定時は data/votes.db に保存
"""
import json
import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# in-memory キャッシュ: {persona_id(int): count(int)}
_persona_cache: dict[int, int] = {}
# in-memory キャッシュ: {proposal_id(str): count(int)}
_policy_cache: dict[str, int] = {}

_SQLITE_DB = Path(__file__).resolve().parent.parent.parent / "data" / "votes.db"


# ---------- ユーティリティ ----------

def _use_neon() -> bool:
    try:
        from .neon_store import use_neon
        return use_neon()
    except Exception:
        return False


def _sqlite_conn():
    _SQLITE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_SQLITE_DB), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS persona_vote_counts (
            persona_id INTEGER PRIMARY KEY,
            vote_count  INTEGER DEFAULT 0,
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS policy_proposals (
            id          TEXT PRIMARY KEY,
            topic_id    TEXT NOT NULL,
            title       TEXT,
            summary     TEXT,
            cost_estimate TEXT,
            effect_prediction TEXT,
            pros        TEXT,
            cons        TEXT,
            expert_sources TEXT,
            rank        INTEGER DEFAULT 0,
            vote_count  INTEGER DEFAULT 0,
            generated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS policy_topics (
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            description  TEXT,
            status       TEXT DEFAULT 'active',
            expert_analyses TEXT,
            generated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(policy_topics)").fetchall()}
    if "expert_analyses" not in cols:
        conn.execute("ALTER TABLE policy_topics ADD COLUMN expert_analyses TEXT")
    conn.commit()
    return conn


# ---------- キャラ投票 ----------

def increment_persona_vote(persona_id: int) -> int:
    """指定キャラの票数を +1 して新しい票数を返す。"""
    try:
        if _use_neon():
            from .neon_store import neon_persona_vote_increment
            new_count = neon_persona_vote_increment(persona_id)
        else:
            with _sqlite_conn() as conn:
                conn.execute(
                    "INSERT INTO persona_vote_counts (persona_id, vote_count) VALUES (?, 1) "
                    "ON CONFLICT(persona_id) DO UPDATE SET vote_count = vote_count + 1, updated_at = datetime('now')",
                    (persona_id,),
                )
                row = conn.execute(
                    "SELECT vote_count FROM persona_vote_counts WHERE persona_id = ?", (persona_id,)
                ).fetchone()
                new_count = row[0] if row else 1
    except Exception as e:
        logger.warning("increment_persona_vote 失敗: %s", e)
        new_count = _persona_cache.get(persona_id, 0) + 1

    with _lock:
        _persona_cache[persona_id] = new_count
    return new_count


def get_persona_vote_counts() -> dict[int, int]:
    """全キャラの票数キャッシュ {persona_id: count} を返す。"""
    with _lock:
        return dict(_persona_cache)


def refresh_vote_cache() -> None:
    """DB から全票数を読み込みキャッシュを更新する。記事更新スケジューラ完了時に呼ぶ。"""
    global _persona_cache, _policy_cache
    try:
        if _use_neon():
            from .neon_store import neon_persona_vote_get_all
            new_persona = neon_persona_vote_get_all()
        else:
            conn = _sqlite_conn()
            rows = conn.execute("SELECT persona_id, vote_count FROM persona_vote_counts").fetchall()
            conn.close()
            new_persona = {r[0]: r[1] for r in rows}
        with _lock:
            _persona_cache = new_persona
        logger.debug("vote_cache refresh: persona=%d エントリ", len(new_persona))
    except Exception as e:
        logger.warning("refresh_vote_cache (persona) 失敗: %s", e)

    try:
        if _use_neon():
            from .neon_store import neon_policy_topics_get_active, neon_policy_vote_counts_get
            topics = neon_policy_topics_get_active()
            new_policy: dict[str, int] = {}
            for t in topics:
                counts = neon_policy_vote_counts_get(t["id"])
                new_policy.update(counts)
        else:
            conn = _sqlite_conn()
            rows = conn.execute("SELECT id, vote_count FROM policy_proposals").fetchall()
            conn.close()
            new_policy = {r[0]: r[1] for r in rows}
        with _lock:
            _policy_cache = new_policy
        logger.debug("vote_cache refresh: policy=%d エントリ", len(new_policy))
    except Exception as e:
        logger.warning("refresh_vote_cache (policy) 失敗: %s", e)


# ---------- 政策投票 ----------

def increment_policy_vote(proposal_id: str) -> int:
    """指定提案の票数を +1 して新しい票数を返す。"""
    try:
        if _use_neon():
            from .neon_store import neon_policy_vote_increment
            new_count = neon_policy_vote_increment(proposal_id)
        else:
            with _sqlite_conn() as conn:
                conn.execute(
                    "UPDATE policy_proposals SET vote_count = vote_count + 1 WHERE id = ?",
                    (proposal_id,),
                )
                row = conn.execute(
                    "SELECT vote_count FROM policy_proposals WHERE id = ?", (proposal_id,)
                ).fetchone()
                new_count = row[0] if row else 0
    except Exception as e:
        logger.warning("increment_policy_vote 失敗: %s", e)
        new_count = _policy_cache.get(proposal_id, 0) + 1

    with _lock:
        _policy_cache[proposal_id] = new_count
    return new_count


def get_policy_vote_counts(topic_id: str | None = None) -> dict[str, int]:
    """政策票数キャッシュを返す。topic_id 指定時はそのトピックの分だけ。"""
    with _lock:
        if topic_id:
            return {k: v for k, v in _policy_cache.items() if k.startswith(topic_id + "_")}
        return dict(_policy_cache)


# ---------- 政策 CRUD（SQLite フォールバック用） ----------

def sqlite_policy_topic_upsert(
    topic_id: str,
    title: str,
    description: str = "",
    expert_analyses: list | None = None,
) -> None:
    conn = _sqlite_conn()
    analyses_json = json.dumps(expert_analyses or [], ensure_ascii=False)
    conn.execute(
        "INSERT INTO policy_topics (id, title, description, expert_analyses) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET title=excluded.title, description=excluded.description, "
        "expert_analyses=excluded.expert_analyses",
        (topic_id, title, description, analyses_json),
    )
    conn.commit()
    conn.close()


def sqlite_policy_proposals_save(topic_id: str, proposals: list) -> None:
    conn = _sqlite_conn()
    conn.execute("DELETE FROM policy_proposals WHERE topic_id = ?", (topic_id,))
    for p in proposals:
        proposal_id = f"{topic_id}_{p.get('rank', 0)}"
        conn.execute(
            "INSERT INTO policy_proposals "
            "(id, topic_id, title, summary, cost_estimate, effect_prediction, pros, cons, expert_sources, rank, vote_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, summary=excluded.summary, "
            "cost_estimate=excluded.cost_estimate, effect_prediction=excluded.effect_prediction, "
            "pros=excluded.pros, cons=excluded.cons, expert_sources=excluded.expert_sources, rank=excluded.rank",
            (
                proposal_id,
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
    conn.commit()
    conn.close()


def sqlite_policy_proposals_get(topic_id: str) -> list:
    conn = _sqlite_conn()
    rows = conn.execute(
        "SELECT id, topic_id, title, summary, cost_estimate, effect_prediction, pros, cons, expert_sources, rank, vote_count "
        "FROM policy_proposals WHERE topic_id = ? ORDER BY rank",
        (topic_id,),
    ).fetchall()
    conn.close()
    cols = ["id", "topic_id", "title", "summary", "cost_estimate", "effect_prediction",
            "pros", "cons", "expert_sources", "rank", "vote_count"]
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


def sqlite_policy_topics_get_active() -> list:
    conn = _sqlite_conn()
    rows = conn.execute(
        "SELECT id, title, description, status, generated_at, expert_analyses "
        "FROM policy_topics WHERE status = 'active' ORDER BY generated_at DESC"
    ).fetchall()
    conn.close()
    cols = ["id", "title", "description", "status", "generated_at", "expert_analyses"]
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        try:
            d["expert_analyses"] = json.loads(d["expert_analyses"]) if d.get("expert_analyses") else []
        except Exception:
            d["expert_analyses"] = []
        result.append(d)
    return result


# ---------- 統一 API（Neon / SQLite 自動選択） ----------

def save_policy_topic(
    topic_id: str,
    title: str,
    description: str = "",
    expert_analyses: list | None = None,
) -> None:
    if _use_neon():
        from .neon_store import neon_policy_topic_upsert
        neon_policy_topic_upsert(topic_id, title, description, expert_analyses)
    else:
        sqlite_policy_topic_upsert(topic_id, title, description, expert_analyses)


def save_policy_proposals(topic_id: str, proposals: list) -> None:
    if _use_neon():
        from .neon_store import neon_policy_proposals_save
        neon_policy_proposals_save(topic_id, proposals)
    else:
        sqlite_policy_proposals_save(topic_id, proposals)
    # キャッシュも即時更新
    for p in proposals:
        pid = f"{topic_id}_{p.get('rank', 0)}"
        with _lock:
            _policy_cache[pid] = p.get("vote_count", 0)


def get_policy_proposals(topic_id: str) -> list:
    if _use_neon():
        from .neon_store import neon_policy_proposals_get
        return neon_policy_proposals_get(topic_id)
    else:
        return sqlite_policy_proposals_get(topic_id)


def get_active_topics() -> list:
    if _use_neon():
        from .neon_store import neon_policy_topics_get_active
        return neon_policy_topics_get_active()
    else:
        return sqlite_policy_topics_get_active()
