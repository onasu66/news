"""偉人への相談 - ルーター"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/consultation", response_class=HTMLResponse)
async def consultation_page(request: Request):
    from app.services.consultation_store import get_consultations
    consultations = get_consultations(limit=30)
    return templates.TemplateResponse(
        "consultation.html",
        {"request": request, "consultations": consultations},
    )
