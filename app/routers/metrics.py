"""メトリクス API - 統計データの検索・取得エンドポイント。

GET /metrics               カテゴリ/年度でフィルタ
GET /metrics/search        全文検索
GET /metrics/category      カテゴリ一覧
GET /metrics/{id}          1件取得（将来拡張用）
"""
import logging
from typing import Optional

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)
router = APIRouter(tags=["metrics"])


def _use_neon() -> bool:
    try:
        from app.services.neon_store import use_neon
        return use_neon()
    except Exception:
        return False


@router.get("/metrics/category")
async def metrics_category():
    """カテゴリ一覧と件数を返す。"""
    try:
        if _use_neon():
            from app.services.neon_store import neon_metrics_categories
            return {"categories": neon_metrics_categories()}
        else:
            from app.services.metrics_service import sqlite_metrics_categories
            return {"categories": sqlite_metrics_categories()}
    except Exception as e:
        logger.warning("metrics_category error: %s", e)
        return {"categories": [], "error": str(e)}


@router.get("/metrics/search")
async def metrics_search(
    q: str = Query("", description="検索キーワード"),
    limit: int = Query(100, ge=1, le=500),
):
    """name/category/subcategory を横断検索する。"""
    if not q.strip():
        return {"rows": [], "total": 0}
    try:
        if _use_neon():
            from app.services.neon_store import neon_metrics_search
            rows = neon_metrics_search(q, limit=limit)
        else:
            from app.services.metrics_service import sqlite_metrics_search
            rows = sqlite_metrics_search(q, limit=limit)
        return {"rows": _serialize_rows(rows), "total": len(rows)}
    except Exception as e:
        logger.warning("metrics_search error: %s", e)
        return {"rows": [], "total": 0, "error": str(e)}


@router.get("/metrics")
async def metrics_list(
    category:    str           = Query("",   description="カテゴリ"),
    subcategory: str           = Query("",   description="サブカテゴリ"),
    name:        str           = Query("",   description="指標名（部分一致）"),
    year:        Optional[int] = Query(None, description="年度"),
    limit:       int           = Query(200,  ge=1, le=1000),
    offset:      int           = Query(0,    ge=0),
):
    """条件指定でメトリクスを取得する。"""
    try:
        if _use_neon():
            from app.services.neon_store import neon_metrics_query
            rows = neon_metrics_query(
                category=category,
                subcategory=subcategory,
                name=name,
                year=year,
                limit=limit,
                offset=offset,
            )
        else:
            from app.services.metrics_service import sqlite_metrics_query
            rows = sqlite_metrics_query(
                category=category,
                subcategory=subcategory,
                name=name,
                year=year,
                limit=limit,
                offset=offset,
            )
        return {"rows": _serialize_rows(rows), "total": len(rows), "offset": offset, "limit": limit}
    except Exception as e:
        logger.warning("metrics_list error: %s", e)
        return {"rows": [], "total": 0, "error": str(e)}


def _serialize_rows(rows: list) -> list:
    """updated_at などの datetime を文字列に変換。"""
    out = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        out.append(d)
    return out
