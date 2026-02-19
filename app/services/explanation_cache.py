"""AI解説・人格意見の永続キャッシュ（SQLite / Firestore）"""
import json
import sqlite3
from pathlib import Path
from typing import Optional

def _use_firestore():
    try:
        from .firestore_store import use_firestore
        return use_firestore()
    except Exception:
        return False

_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "explanations.db"


def _get_conn():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS explanation_cache (
                article_id TEXT PRIMARY KEY,
                inline_blocks TEXT NOT NULL,
                persona_0 TEXT,
                persona_1 TEXT,
                persona_2 TEXT,
                persona_3 TEXT,
                persona_4 TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def get_cached_article_ids() -> set[str]:
    """AI処理済み（ミドルマン解説あり）のarticle_id一覧"""
    if _use_firestore():
        from .firestore_store import firestore_get_cached_article_ids
        return firestore_get_cached_article_ids()
    _init_db()
    with _get_conn() as conn:
        rows = conn.execute("SELECT article_id FROM explanation_cache").fetchall()
    return {r[0] for r in rows}


def _is_bad_fallback_cache(blocks: list) -> bool:
    """構造化失敗時のフォールバック結果か（再生成対象）"""
    if not blocks or len(blocks) != 2:
        return False
    types = [b.get("type") for b in blocks if isinstance(b, dict)]
    if types != ["text", "explain"]:
        return False
    explain_content = next((b.get("content", "") for b in blocks if isinstance(b, dict) and b.get("type") == "explain"), "")
    bad_phrases = ("構造化に失敗", "通常の解説を表示", "しばらくしてから再度")
    return any(p in explain_content for p in bad_phrases)


def get_cached(article_id: str) -> Optional[dict]:
    """キャッシュから取得。なければNone。壊れたフォールバック結果はNone扱いで再生成させる"""
    if _use_firestore():
        from .firestore_store import firestore_get_cached
        return firestore_get_cached(article_id)
    _init_db()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT inline_blocks, persona_0, persona_1, persona_2, persona_3, persona_4 FROM explanation_cache WHERE article_id = ?",
            (article_id,),
        ).fetchone()
    if not row:
        return None
    blocks = json.loads(row["inline_blocks"])
    if _is_bad_fallback_cache(blocks):
        return None
    return {
        "blocks": blocks,
        "personas": [
            row["persona_0"] or "",
            row["persona_1"] or "",
            row["persona_2"] or "",
            row["persona_3"] or "",
            row["persona_4"] or "",
        ],
    }


def delete_cache(article_id: str) -> bool:
    """指定記事の解説キャッシュを削除。存在したらTrue"""
    if _use_firestore():
        from .firestore_store import firestore_delete_cache
        return firestore_delete_cache(article_id)
    _init_db()
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM explanation_cache WHERE article_id = ?", (article_id,))
        conn.commit()
    return cur.rowcount > 0


def save_cache(article_id: str, blocks: list, personas: list[str]):
    """キャッシュに保存"""
    if _use_firestore():
        from .firestore_store import firestore_save_cache
        firestore_save_cache(article_id, blocks, personas)
        return
    _init_db()
    while len(personas) < 5:
        personas.append("")
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO explanation_cache
            (article_id, inline_blocks, persona_0, persona_1, persona_2, persona_3, persona_4)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (article_id, json.dumps(blocks, ensure_ascii=False), *personas[:5]),
        )
        conn.commit()
