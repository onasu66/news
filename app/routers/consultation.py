"""偉人への相談 - ルーター"""
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

_cache: list = []
_cache_at: float = 0.0
_CACHE_TTL = 3600  # 1時間


def _get_consultations_cached() -> list:
    global _cache, _cache_at
    if _cache and (time.time() - _cache_at) < _CACHE_TTL:
        return _cache
    from app.services.consultation_store import get_consultations
    _cache = get_consultations(limit=30)
    _cache_at = time.time()
    return _cache


def invalidate_consultation_cache() -> None:
    """新規掲載後などに呼んでキャッシュを即時破棄する。"""
    global _cache_at
    _cache_at = 0.0


@router.get("/consultation", response_class=HTMLResponse)
async def consultation_page(request: Request):
    consultations = _get_consultations_cached()
    return templates.TemplateResponse(
        "consultation.html",
        {"request": request, "consultations": consultations},
    )
