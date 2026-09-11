"""GSC 実績から「次に狙うキーワード」を算出する。

考え方:
  potential = impressions * ctr_target - clicks
  （1位への近さではなく「取り逃しているクリック数」で優先度を決める）

分類:
  almost   … 2〜10位。あと少しで上位。改善効果が最も出やすい
  reach    … 11〜20位。表示は多いが2ページ目。伸びしろ枠
  ctr      … 3位以内なのに CTR が低い。順位ではなくタイトル/説明文の問題
  untapped … 表示はあるが未登録の語。狙いKW への追加候補
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# 上位表示時に期待できる CTR。potential の係数。
DEFAULT_CTR_TARGET = 0.25
# ノイズ除去。これ未満の表示回数は母数が小さすぎて判断できない。
DEFAULT_MIN_IMPRESSIONS = 10

LABELS = {
    "almost": "あと一歩（2〜10位）",
    "reach": "伸びしろ（11〜20位）",
    "ctr": "CTR改善（上位なのに未クリック）",
    "untapped": "未登録（狙いKW候補）",
}


def _ctr_target() -> float:
    try:
        v = float(os.getenv("GSC_CTR_TARGET", "") or DEFAULT_CTR_TARGET)
    except ValueError:
        return DEFAULT_CTR_TARGET
    return v if 0 < v <= 1 else DEFAULT_CTR_TARGET


def _min_impressions() -> int:
    try:
        return max(1, int(os.getenv("GSC_MIN_IMPRESSIONS", "") or DEFAULT_MIN_IMPRESSIONS))
    except ValueError:
        return DEFAULT_MIN_IMPRESSIONS


def _known_keywords() -> set[str]:
    """すでに狙っている語（YAML固定 + 手動昇格 + auto_boost）。"""
    known: set[str] = set()
    try:
        from app.services.seo_keywords_config import get_target_high_value

        known |= {str(k).strip().lower() for k in get_target_high_value()}
    except Exception as e:
        logger.debug("target_high_value 取得失敗: %s", e)
    try:
        from app.services.seo_keywords_store import (
            get_auto_boost_keywords,
            get_promoted_keywords,
        )

        known |= {k.lower() for k in get_promoted_keywords()}
        known |= {k.lower() for k in get_auto_boost_keywords()}
    except Exception as e:
        logger.debug("動的キーワード取得失敗: %s", e)
    return {k for k in known if k}


def _is_known(query: str, known: set[str]) -> bool:
    q = query.strip().lower()
    if not q:
        return False
    if q in known:
        return True
    # 「AI 論文 解説」のように狙いKWを含む複合クエリも既知扱い
    return any(k in q for k in known if len(k) >= 2)


def _classify(position: float, ctr: float, known: bool) -> str:
    if 0 < position <= 3 and ctr < 5.0:
        return "ctr"
    if not known:
        return "untapped"
    if 2 <= position <= 10:
        return "almost"
    if 10 < position <= 20:
        return "reach"
    return "almost" if 0 < position < 2 else "reach"


def _position_deltas(days: int = 7) -> dict[str, float]:
    """days 日前のスナップショットと比べた順位変化（負 = 順位が上がった）。"""
    try:
        from app.services.seo_keywords_store import get_rank_history

        history = get_rank_history()
    except Exception as e:
        logger.debug("rank_history 取得失敗: %s", e)
        return {}
    if len(history) < 2:
        return {}

    latest = history[-1]
    # days 日ぶん遡った位置にある一番新しいスナップショットを基準にする
    baseline = history[max(0, len(history) - 1 - days)]
    if baseline.get("date") == latest.get("date"):
        return {}

    def _by_query(snap: dict[str, Any]) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in snap.get("rows") or []:
            q = str(r.get("query") or "").strip().lower()
            pos = float(r.get("position") or 0)
            if q and pos > 0:
                out[q] = pos
        return out

    now, before = _by_query(latest), _by_query(baseline)
    return {q: round(now[q] - before[q], 1) for q in now if q in before}


def build_opportunities(
    performance: list[dict[str, Any]] | None = None,
    *,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """potential 降順の改善機会リスト。"""
    if performance is None:
        try:
            from app.services.seo_keywords_store import get_performance_keywords

            performance = get_performance_keywords()
        except Exception as e:
            logger.warning("performance 取得失敗: %s", e)
            return []

    ctr_target = _ctr_target()
    min_imps = _min_impressions()
    known = _known_keywords()
    deltas = _position_deltas()

    items: list[dict[str, Any]] = []
    for row in performance or []:
        if not isinstance(row, dict):
            continue
        query = str(row.get("query") or "").strip()
        if not query:
            continue
        impressions = int(row.get("impressions") or 0)
        if impressions < min_imps:
            continue
        clicks = int(row.get("clicks") or 0)
        ctr = float(row.get("ctr") or 0.0)
        position = float(row.get("position") or 0.0)

        potential = impressions * ctr_target - clicks
        if potential <= 0:
            continue

        is_known = _is_known(query, known)
        items.append(
            {
                "query": query,
                "clicks": clicks,
                "impressions": impressions,
                "ctr": round(ctr, 2),
                "position": round(position, 1),
                "potential": round(potential, 1),
                "category": _classify(position, ctr, is_known),
                "known": is_known,
                "delta": deltas.get(query.lower()),
            }
        )

    items.sort(key=lambda x: x["potential"], reverse=True)
    for item in items:
        item["label"] = LABELS.get(item["category"], item["category"])
    return items[:limit]


def recommended_keywords(limit: int = 20) -> list[dict[str, Any]]:
    """狙いKW に未登録で、実際に表示が出ている語＝追加のおすすめ。"""
    return [
        o
        for o in build_opportunities(limit=200)
        if o["category"] == "untapped"
    ][:limit]


def summarize(opportunities: list[dict[str, Any]]) -> dict[str, Any]:
    """カテゴリ別の件数と、取り逃しクリックの合計。"""
    counts: dict[str, int] = {k: 0 for k in LABELS}
    for o in opportunities:
        counts[o["category"]] = counts.get(o["category"], 0) + 1
    return {
        "counts": counts,
        "labels": LABELS,
        "total_potential": round(sum(o["potential"] for o in opportunities), 1),
        "ctr_target": _ctr_target(),
        "min_impressions": _min_impressions(),
    }
