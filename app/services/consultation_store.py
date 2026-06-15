"""偉人への相談 - ストレージ（Neon Postgres / SQLite フォールバック）"""
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "consultations.db"


def _use_neon() -> bool:
    try:
        from .neon_store import use_neon
        return use_neon()
    except Exception:
        return False


def _get_sqlite_conn():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _init_sqlite():
    with _get_sqlite_conn() as conn:
        conn.execute("""
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
        """)
        conn.commit()


def save_consultation(
    question: str,
    persona_id: int,
    persona_name: str,
    persona_emoji: str,
    answer: str,
    source: str = "line",
    source_user: str | None = None,
) -> str:
    cid = str(uuid.uuid4())[:8]
    now = datetime.now()
    if _use_neon():
        from .neon_store import _conn
        with _conn("save_consultation") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO consultations
                       (id, question, source, source_user, persona_id, persona_name, persona_emoji, answer, published_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (cid, question, source, source_user, persona_id, persona_name, persona_emoji, answer, now),
                )
        return cid
    _init_sqlite()
    with _get_sqlite_conn() as conn:
        conn.execute(
            """INSERT INTO consultations
               (id, question, source, source_user, persona_id, persona_name, persona_emoji, answer, published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cid, question, source, source_user, persona_id, persona_name, persona_emoji, answer, now.isoformat()),
        )
        conn.commit()
    return cid


def get_consultations(limit: int = 30) -> list[dict]:
    if _use_neon():
        from .neon_store import _conn
        with _conn("get_consultations") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, question, source, source_user, persona_id, persona_name, persona_emoji, answer, published_at "
                    "FROM consultations ORDER BY published_at DESC LIMIT %s",
                    (limit,),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    _init_sqlite()
    with _get_sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT id, question, source, source_user, persona_id, persona_name, persona_emoji, answer, published_at "
            "FROM consultations ORDER BY published_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
