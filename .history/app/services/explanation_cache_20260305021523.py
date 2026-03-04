"""AI解説・人格意見の永続キャッシュ（SQLite / Firestore）。Firestore 利用時はメモリキャッシュで読み取り削減"""
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

# Firestore 時のメモリキャッシュ（無料枠 5万読/日 対策）
# 永続化は Firestore、読み取り削減はこのメモリキャッシュで行う（Render側に多めに持つと読取回数が減る）
_ids_cache: Optional[tuple[float, set[str]]] = None  # (cached_at, set of ids)
_ids_cache_ttl_sec = 300  # 5分（60秒→延長して Firestore の get_cached_article_ids 読取を減らす）
_explanation_cache: dict[str, dict] = {}  # article_id -> 解説 dict
_explanation_cache_max = 400  # 200→400 に増やし、記事詳細の Firestore 読取を減らす（メモリ許容範囲で）

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


PERSONAS_COUNT = 14  # ai_service.PERSONAS の長さ


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
        try:
            conn.execute("ALTER TABLE explanation_cache ADD COLUMN personas TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE explanation_cache ADD COLUMN display_persona_ids TEXT")
        except Exception:
            pass
        conn.commit()


def invalidate_ids_cache() -> None:
    """Firestore 用のメモリキャッシュ（get_cached_article_ids）を無効化。同期API実行後に呼ぶ"""
    global _ids_cache
    _ids_cache = None


def get_cached_article_ids() -> set[str]:
    """AI処理済み（ミドルマン解説あり）のarticle_id一覧。Firestore 時はメモリで TTL キャッシュ（読取削減）"""
    global _ids_cache
    if _use_firestore():
        now = time.monotonic()
        if _ids_cache is not None and (now - _ids_cache[0]) < _ids_cache_ttl_sec:
            return _ids_cache[1]
        from .firestore_store import firestore_get_cached_article_ids
        ids = firestore_get_cached_article_ids()
        _ids_cache = (now, ids)
        return ids
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
    """キャッシュから取得。なければNone。Firestore 時は Render 側メモリキャッシュで同一記事の再読を削減"""
    global _explanation_cache
    if _use_firestore():
        if article_id in _explanation_cache:
            return _explanation_cache[article_id]
        from .firestore_store import firestore_get_cached
        result = firestore_get_cached(article_id)
        if result is not None:
            if len(_explanation_cache) >= _explanation_cache_max:
                oldest = next(iter(_explanation_cache))
                del _explanation_cache[oldest]
            _explanation_cache[article_id] = result
        return result
    _init_db()
    with _get_conn() as conn:
        try:
            row = conn.execute(
                "SELECT inline_blocks, persona_0, persona_1, persona_2, persona_3, persona_4, personas, display_persona_ids FROM explanation_cache WHERE article_id = ?",
                (article_id,),
            ).fetchone()
        except Exception:
            try:
                row = conn.execute(
                    "SELECT inline_blocks, persona_0, persona_1, persona_2, persona_3, persona_4, personas FROM explanation_cache WHERE article_id = ?",
                    (article_id,),
                ).fetchone()
            except Exception:
                row = conn.execute(
                    "SELECT inline_blocks, persona_0, persona_1, persona_2, persona_3, persona_4 FROM explanation_cache WHERE article_id = ?",
                    (article_id,),
                ).fetchone()
    if not row:
        return None
    try:
        row = dict(row)
    except Exception:
        row = {k: row[k] for k in row.keys()}
    blocks = json.loads(row["inline_blocks"])
    if _is_bad_fallback_cache(blocks):
        return None
    try:
        display_persona_ids = json.loads(row["display_persona_ids"]) if row.get("display_persona_ids") else None
    except Exception:
        display_persona_ids = None
    try:
        personas = json.loads(row["personas"]) if row.get("personas") else None
    except Exception:
        personas = None
    if display_persona_ids is not None and isinstance(display_persona_ids, list) and len(display_persona_ids) == 3 and isinstance(personas, list) and len(personas) == 3:
        result = {"blocks": blocks, "personas": personas, "display_persona_ids": display_persona_ids}
    else:
        if isinstance(personas, list) and len(personas) >= PERSONAS_COUNT:
            personas = personas[:PERSONAS_COUNT]
        else:
            personas = [row.get("persona_0") or "", row.get("persona_1") or "", row.get("persona_2") or "", row.get("persona_3") or "", row.get("persona_4") or ""] + [""] * (PERSONAS_COUNT - 5)
        result = {"blocks": blocks, "personas": personas}
    extra = _get_extra(article_id)
    if extra:
        result["quick_understand"] = extra.get("quick_understand", {})
        result["vote_data"] = extra.get("vote_data", {})
    return result


def delete_cache(article_id: str) -> bool:
    """指定記事の解説キャッシュを削除。存在したらTrue"""
    global _ids_cache, _explanation_cache
    if _use_firestore():
        from .firestore_store import firestore_delete_cache
        out = firestore_delete_cache(article_id)
        _ids_cache = None
        _explanation_cache.pop(article_id, None)
        return out
    _init_db()
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM explanation_cache WHERE article_id = ?", (article_id,))
        conn.commit()
    return cur.rowcount > 0


def _get_extra_db_path():
    return Path(__file__).resolve().parent.parent.parent / "data" / "explanation_extra.db"


def _get_extra_conn():
    p = _get_extra_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS explanation_extra (
        article_id TEXT PRIMARY KEY,
        data TEXT NOT NULL
    )""")
    conn.commit()
    return conn


def _get_extra(article_id: str) -> Optional[dict]:
    if _use_firestore():
        return None
    try:
        with _get_extra_conn() as conn:
            row = conn.execute("SELECT data FROM explanation_extra WHERE article_id = ?", (article_id,)).fetchone()
        return json.loads(row["data"]) if row else None
    except Exception:
        return None


def _save_extra(article_id: str, data: dict):
    if _use_firestore():
        return
    try:
        with _get_extra_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO explanation_extra (article_id, data) VALUES (?, ?)",
                (article_id, json.dumps(data, ensure_ascii=False)),
            )
            conn.commit()
    except Exception:
        pass


def save_cache(article_id: str, blocks: list, personas: list[str], *, display_persona_ids: list[int] | None = None, quick_understand: dict | None = None, vote_data: dict | None = None):
    """キャッシュに保存。display_persona_ids あり時は personas は3件のみ。"""
    global _ids_cache
    if _use_firestore():
        from .firestore_store import firestore_save_cache
        firestore_save_cache(article_id, blocks, personas, display_persona_ids=display_persona_ids, quick_understand=quick_understand, vote_data=vote_data)
        _ids_cache = None  # 次回 get_cached_article_ids で再取得
        return
    _init_db()
    if display_persona_ids is not None and len(display_persona_ids) == 3 and len(personas) == 3:
        personas_json = json.dumps(personas, ensure_ascii=False)
        ids_json = json.dumps(display_persona_ids, ensure_ascii=False)
        p0, p1, p2 = (personas[0], personas[1], personas[2]) if len(personas) >= 3 else ("", "", "")
        with _get_conn() as conn:
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO explanation_cache
                    (article_id, inline_blocks, persona_0, persona_1, persona_2, persona_3, persona_4, personas, display_persona_ids)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (article_id, json.dumps(blocks, ensure_ascii=False), p0, p1, p2, "", "", personas_json, ids_json),
                )
            except Exception:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO explanation_cache
                    (article_id, inline_blocks, persona_0, persona_1, persona_2, persona_3, persona_4, personas)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (article_id, json.dumps(blocks, ensure_ascii=False), p0, p1, p2, "", "", personas_json),
                )
            conn.commit()
    else:
        while len(personas) < PERSONAS_COUNT:
            personas.append("")
        personas = personas[:PERSONAS_COUNT]
        with _get_conn() as conn:
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO explanation_cache
                    (article_id, inline_blocks, persona_0, persona_1, persona_2, persona_3, persona_4, personas)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (article_id, json.dumps(blocks, ensure_ascii=False), personas[0], personas[1], personas[2], personas[3], personas[4], json.dumps(personas, ensure_ascii=False)),
                )
            except Exception:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO explanation_cache
                    (article_id, inline_blocks, persona_0, persona_1, persona_2, persona_3, persona_4)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (article_id, json.dumps(blocks, ensure_ascii=False), personas[0], personas[1], personas[2], personas[3], personas[4]),
                )
            conn.commit()
    extra = {}
    if quick_understand:
        extra["quick_understand"] = quick_understand
    if vote_data:
        extra["vote_data"] = vote_data
    if extra:
        _save_extra(article_id, extra)
