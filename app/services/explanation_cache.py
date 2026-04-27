"""AI解説・人格意見の永続キャッシュ（SQLite / Firestore）。Firestore 利用時はメモリキャッシュで読み取り削減"""
import json
import sqlite3
import time
import threading
from pathlib import Path
from typing import Optional

# Firestore 時のメモリキャッシュ（無料枠 5万読/日 対策）
_ids_cache: Optional[tuple[float, set[str]]] = None  # (cached_at, set of ids)
_ids_cache_ttl_sec = 60
_explanation_cache: dict[str, dict] = {}  # article_id -> 解説 dict
_explanation_cache_max = 200
_explanation_cache_lock = threading.Lock()

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
    """AI処理済み（ミドルマン解説あり）のarticle_id一覧。Firestore 時はメモリで 60 秒キャッシュ"""
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
    """キャッシュから取得。なければNone。Firestore 時はメモリキャッシュ（最大200件）で同一記事の再読を削減。
    Firestore のクォータ超過・エラー時は例外を握りつぶして None を返す（500エラーを防ぐ）。"""
    global _explanation_cache
    if _use_firestore():
        with _explanation_cache_lock:
            cached = _explanation_cache.get(article_id)
        if cached is not None:
            return cached
        from .firestore_store import firestore_get_cached
        try:
            result = firestore_get_cached(article_id)
        except Exception as e:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "get_cached: Firestore 読み取り失敗（クォータ超過の可能性）article_id=%s: %s",
                article_id, e,
            )
            return None
        if result is not None:
            with _explanation_cache_lock:
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
        result["paper_graph"] = extra.get("paper_graph", {})
        result["paper_quiz"] = extra.get("paper_quiz", {})
        result["deep_insights"] = extra.get("deep_insights", {})
    return result


def get_cached_many(article_ids: list[str]) -> dict[str, dict]:
    """
    複数 article_id をまとめてキャッシュから取得（SQLite 用の直列削減）
    Firestore は既存 get_cached がメモリキャッシュを持っているため、ここでは利用者側で呼び分ける前提。
    """
    if not article_ids:
        return {}

    # Firestore は別途行う（必要なら caller 側で Firestore バルクリードを使う）
    if _use_firestore():
        out: dict[str, dict] = {}
        for aid in article_ids:
            d = get_cached(aid)
            if d is not None:
                out[str(aid)] = d
        return out

    _init_db()
    # SQLite では最大パラメータ数があるので分割
    # （ただし今回の用途は papers カード数=せいぜい数十件なので基本的に安全）
    ids = list({str(x) for x in article_ids if x})
    if not ids:
        return {}

    out: dict[str, dict] = {}
    chunk_size = 500
    with _get_conn() as conn:
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            placeholders = ",".join(["?"] * len(chunk))
            rows = conn.execute(
                f"""
                SELECT article_id, inline_blocks, persona_0, persona_1, persona_2, persona_3, persona_4,
                       personas, display_persona_ids
                FROM explanation_cache
                WHERE article_id IN ({placeholders})
                """,
                tuple(chunk),
            ).fetchall()
            for row in rows:
                try:
                    d = dict(row)
                    blocks = json.loads(d.get("inline_blocks", "[]"))
                    if _is_bad_fallback_cache(blocks):
                        continue
                    display_persona_ids = json.loads(d["display_persona_ids"]) if d.get("display_persona_ids") else None
                    personas = json.loads(d["personas"]) if d.get("personas") else None
                    if display_persona_ids is not None and isinstance(display_persona_ids, list) and len(display_persona_ids) == 3 and isinstance(personas, list) and len(personas) == 3:
                        result = {"blocks": blocks, "personas": personas, "display_persona_ids": display_persona_ids}
                    else:
                        if isinstance(personas, list) and len(personas) >= PERSONAS_COUNT:
                            personas = personas[:PERSONAS_COUNT]
                        elif not isinstance(personas, list):
                            personas = [d.get("persona_0", "") or "", d.get("persona_1", "") or "", d.get("persona_2", "") or "", d.get("persona_3", "") or "", d.get("persona_4", "") or ""] + [""] * (PERSONAS_COUNT - 5)
                        result = {"blocks": blocks, "personas": personas}
                    extra = _get_extra(row["article_id"])
                    if extra:
                        result["quick_understand"] = extra.get("quick_understand", {})
                        result["vote_data"] = extra.get("vote_data", {})
                        result["paper_graph"] = extra.get("paper_graph", {})
                        result["paper_quiz"] = extra.get("paper_quiz", {})
                        result["deep_insights"] = extra.get("deep_insights", {})
                    out[str(row["article_id"])] = result
                except Exception:
                    continue

    return out


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


def save_cache(
    article_id: str,
    blocks: list,
    personas: list[str],
    *,
    display_persona_ids: list[int] | None = None,
    quick_understand: dict | None = None,
    vote_data: dict | None = None,
    paper_graph: dict | None = None,
    paper_quiz: dict | None = None,
    deep_insights: dict | None = None,
):
    """キャッシュに保存。display_persona_ids あり時は personas は3件のみ。"""
    global _ids_cache
    if _use_firestore():
        from .firestore_store import firestore_save_cache
        firestore_save_cache(
            article_id,
            blocks,
            personas,
            display_persona_ids=display_persona_ids,
            quick_understand=quick_understand,
            vote_data=vote_data,
            paper_graph=paper_graph,
            paper_quiz=paper_quiz,
            deep_insights=deep_insights,
        )
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
    if paper_graph:
        extra["paper_graph"] = paper_graph
    if paper_quiz:
        extra["paper_quiz"] = paper_quiz
    if deep_insights:
        extra["deep_insights"] = deep_insights
    if extra:
        _save_extra(article_id, extra)
