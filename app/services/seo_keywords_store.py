"""SEO 動的キーワードストア（auto_boost / performance / log）。

永続化優先順:
  1) Turso（seo_state テーブル）
  2) data/seo_state.json
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_JSON_PATH = _ROOT / "data" / "seo_state.json"
_LOCK = threading.RLock()

_STATE_ID = "latest"

_EMPTY: dict[str, Any] = {
    "auto_boost": [],  # [{keyword, hits, sources, updated_at}]
    "promoted": [],  # 手動で固定扱いに昇格した語
    "performance": [],  # [{query, clicks, impressions, ctr, position}]
    "blocked": [],  # auto_boost から除外する語
    "optimization_log": [],  # [{at, action, keyword, detail}]
    "health": {},  # sitemap / indexnow 等
    "last_run_at": None,
    "last_candidates": [],  # 直近ジョブの候補一覧
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_shape(data: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(_EMPTY)
    if not isinstance(data, dict):
        return out
    for key in _EMPTY:
        if key in data:
            out[key] = data[key]
    for list_key in ("auto_boost", "promoted", "performance", "blocked", "optimization_log", "last_candidates"):
        if not isinstance(out[list_key], list):
            out[list_key] = []
    if not isinstance(out.get("health"), dict):
        out["health"] = {}
    return out


def _read_json() -> dict[str, Any]:
    if not _JSON_PATH.exists():
        return _ensure_shape(None)
    try:
        return _ensure_shape(json.loads(_JSON_PATH.read_text(encoding="utf-8")))
    except Exception as e:
        logger.warning("seo_state.json 読込失敗: %s", e)
        return _ensure_shape(None)


def _write_json(state: dict[str, Any]) -> None:
    _JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _JSON_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_JSON_PATH)


def _turso_available() -> bool:
    try:
        from app.services.turso_store import use_turso

        return bool(use_turso())
    except Exception:
        return False


def _ensure_turso_table() -> None:
    from app.services.turso_store import turso_conn

    with turso_conn("seo_state_init") as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seo_state (
                id TEXT PRIMARY KEY CHECK (id = 'latest'),
                payload TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _read_turso() -> dict[str, Any] | None:
    try:
        from app.services.turso_store import turso_conn

        _ensure_turso_table()
        with turso_conn("seo_state_get") as conn:
            row = conn.execute(
                "SELECT payload FROM seo_state WHERE id = ?",
                (_STATE_ID,),
            ).fetchone()
        if not row:
            return None
        payload = row[0] if not isinstance(row, dict) else row.get("payload")
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        return _ensure_shape(json.loads(payload))
    except Exception as e:
        logger.debug("seo_state Turso 読込スキップ: %s", e)
        return None


def _write_turso(state: dict[str, Any]) -> bool:
    try:
        from app.services.turso_store import turso_conn

        _ensure_turso_table()
        blob = json.dumps(state, ensure_ascii=False)
        with turso_conn("seo_state_save") as conn:
            conn.execute(
                """
                INSERT INTO seo_state (id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (_STATE_ID, blob, _now_iso()),
            )
        return True
    except Exception as e:
        logger.warning("seo_state Turso 保存失敗: %s", e)
        return False


def load_state() -> dict[str, Any]:
    with _LOCK:
        if _turso_available():
            data = _read_turso()
            if data is not None:
                return data
        return _read_json()


def save_state(state: dict[str, Any]) -> dict[str, Any]:
    state = _ensure_shape(state)
    with _LOCK:
        if _turso_available():
            if not _write_turso(state):
                _write_json(state)
        else:
            _write_json(state)
    return state


def get_auto_boost_keywords() -> list[str]:
    state = load_state()
    blocked = {str(x).strip().lower() for x in state.get("blocked") or []}
    out: list[str] = []
    for item in state.get("auto_boost") or []:
        if isinstance(item, dict):
            kw = str(item.get("keyword") or "").strip()
        else:
            kw = str(item).strip()
        if kw and kw.lower() not in blocked:
            out.append(kw)
    return out


def get_promoted_keywords() -> list[str]:
    state = load_state()
    return [str(x).strip() for x in (state.get("promoted") or []) if str(x).strip()]


def get_performance_keywords() -> list[dict[str, Any]]:
    state = load_state()
    return [x for x in (state.get("performance") or []) if isinstance(x, dict)]


def get_performance_queries() -> list[str]:
    return [
        str(x.get("query") or "").strip()
        for x in get_performance_keywords()
        if str(x.get("query") or "").strip()
    ]


def append_log(action: str, keyword: str = "", detail: str = "") -> None:
    state = load_state()
    logs = list(state.get("optimization_log") or [])
    logs.insert(
        0,
        {
            "at": _now_iso(),
            "action": action,
            "keyword": keyword,
            "detail": detail,
        },
    )
    state["optimization_log"] = logs[:200]
    save_state(state)


def set_health(health: dict[str, Any]) -> None:
    state = load_state()
    cur = dict(state.get("health") or {})
    cur.update(health)
    cur["checked_at"] = _now_iso()
    state["health"] = cur
    save_state(state)


def update_auto_boost(entries: list[dict[str, Any]], *, log_changes: bool = True) -> dict[str, Any]:
    """auto_boost を差し替え。entries: keyword/hits/sources/updated_at。"""
    state = load_state()
    blocked = {str(x).strip().lower() for x in state.get("blocked") or []}
    old = {
        (str(i.get("keyword") or "").strip().lower() if isinstance(i, dict) else str(i).strip().lower())
        for i in (state.get("auto_boost") or [])
    }
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        kw = str(item.get("keyword") or "").strip()
        if not kw or kw.lower() in blocked or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        cleaned.append(
            {
                "keyword": kw,
                "hits": int(item.get("hits") or 0),
                "sources": list(item.get("sources") or []),
                "updated_at": item.get("updated_at") or _now_iso(),
            }
        )
    new = {c["keyword"].lower() for c in cleaned}
    if log_changes:
        logs = list(state.get("optimization_log") or [])
        for kw in sorted(new - old):
            logs.insert(0, {"at": _now_iso(), "action": "boost_add", "keyword": kw, "detail": "auto"})
        for kw in sorted(old - new):
            logs.insert(0, {"at": _now_iso(), "action": "boost_remove", "keyword": kw, "detail": "auto"})
        state["optimization_log"] = logs[:200]
    state["auto_boost"] = cleaned
    state["last_run_at"] = _now_iso()
    return save_state(state)


def set_last_candidates(candidates: list[dict[str, Any]]) -> None:
    state = load_state()
    state["last_candidates"] = candidates[:100]
    save_state(state)


def block_keyword(keyword: str) -> dict[str, Any]:
    kw = (keyword or "").strip()
    if not kw:
        return load_state()
    state = load_state()
    blocked = [str(x).strip() for x in (state.get("blocked") or []) if str(x).strip()]
    if kw not in blocked:
        blocked.append(kw)
    state["blocked"] = blocked
    # auto_boost からも除去
    state["auto_boost"] = [
        i
        for i in (state.get("auto_boost") or [])
        if isinstance(i, dict) and str(i.get("keyword") or "").strip().lower() != kw.lower()
    ]
    logs = list(state.get("optimization_log") or [])
    logs.insert(0, {"at": _now_iso(), "action": "block", "keyword": kw, "detail": "manual"})
    state["optimization_log"] = logs[:200]
    return save_state(state)


def promote_keyword(keyword: str) -> dict[str, Any]:
    """auto / 候補を固定扱い（promoted）へ昇格。"""
    kw = (keyword or "").strip()
    if not kw:
        return load_state()
    state = load_state()
    promoted = [str(x).strip() for x in (state.get("promoted") or []) if str(x).strip()]
    if not any(p.lower() == kw.lower() for p in promoted):
        promoted.append(kw)
    state["promoted"] = promoted
    logs = list(state.get("optimization_log") or [])
    logs.insert(0, {"at": _now_iso(), "action": "promote", "keyword": kw, "detail": "manual"})
    state["optimization_log"] = logs[:200]
    return save_state(state)


def unpromote_keyword(keyword: str) -> dict[str, Any]:
    kw = (keyword or "").strip()
    state = load_state()
    state["promoted"] = [
        str(x).strip()
        for x in (state.get("promoted") or [])
        if str(x).strip() and str(x).strip().lower() != kw.lower()
    ]
    logs = list(state.get("optimization_log") or [])
    logs.insert(0, {"at": _now_iso(), "action": "unpromote", "keyword": kw, "detail": "manual"})
    state["optimization_log"] = logs[:200]
    return save_state(state)


def parse_performance_paste(text: str) -> list[dict[str, Any]]:
    """GSC風の貼り付けをパース。

    対応:
      - 1行1クエリ
      - TSV/CSV: query, clicks, impressions, ctr, position
    """
    rows: list[dict[str, Any]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # ヘッダ行スキップ
        lower = line.lower()
        if lower.startswith("query") or lower.startswith("クエリ") or lower.startswith("検索クエリ"):
            continue
        parts = [p.strip() for p in re_split_csv(line)]
        if not parts:
            continue
        query = parts[0].strip().strip('"')
        if not query:
            continue
        def _num(i: int, default: float = 0.0) -> float:
            if i >= len(parts):
                return default
            s = parts[i].replace("%", "").replace(",", "").strip()
            try:
                return float(s)
            except Exception:
                return default

        rows.append(
            {
                "query": query,
                "clicks": int(_num(1)),
                "impressions": int(_num(2)),
                "ctr": _num(3),
                "position": _num(4),
            }
        )
    # クエリ重複は clicks 優先でマージ
    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r["query"].lower()
        if key not in merged or r["clicks"] >= merged[key]["clicks"]:
            merged[key] = r
    return list(merged.values())


def re_split_csv(line: str) -> list[str]:
    if "\t" in line:
        return line.split("\t")
    if "," in line:
        return line.split(",")
    return [line]


def save_performance_from_paste(text: str) -> dict[str, Any]:
    rows = parse_performance_paste(text)
    state = load_state()
    state["performance"] = rows[:500]
    logs = list(state.get("optimization_log") or [])
    logs.insert(
        0,
        {
            "at": _now_iso(),
            "action": "performance_import",
            "keyword": "",
            "detail": f"{len(rows)} queries",
        },
    )
    state["optimization_log"] = logs[:200]
    return save_state(state)
