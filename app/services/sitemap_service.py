"""Sitemap generation and snapshot storage."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from threading import Lock

from app.config import settings


def _slugify_for_sitemap(title: str, article_id: str, max_len: int = 55) -> str:
    """sitemap_service 内部用スラッグ生成（routers/news の同関数と同一ロジック）。"""
    s = (title or "").strip()
    s = re.sub(r'[「」『』【】〈〉《》\[\]{}()（）<>""\'\'`！!？?。、，,．\.。:;：；・＊*＋+＝=＆&＠@＃#｜|＼\\／/]', '', s)
    s = re.sub(r'[\s\u3000　]+', '-', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    slug = s[:max_len] if s else ""
    if slug:
        suffix = article_id[-6:] if len(article_id) >= 6 else article_id
        return f"{slug}-{suffix}"
    return article_id

logger = logging.getLogger(__name__)

_SITEMAP_LOCK = Lock()
_SITEMAP_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "sitemap.xml"


def sitemap_snapshot_path() -> Path:
    return _SITEMAP_PATH


def _default_site_url() -> str:
    return (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")


def _article_lastmod(article, today: str) -> str:
    """新規掲載日（added_at）を優先し、なければ published。"""
    for attr in ("added_at", "published"):
        dt = getattr(article, attr, None)
        if not dt:
            continue
        try:
            if hasattr(dt, "date"):
                return dt.date().isoformat()
            if hasattr(dt, "isoformat"):
                return str(dt)[:10]
        except Exception:
            continue
    return today


_CATEGORY_SLUGS = ["ai", "tech", "science", "world", "social", "sports", "entertainment"]


def build_sitemap_xml(site_url: str, articles: list) -> str:
    base_url = (site_url or "").strip().rstrip("/")
    today = datetime.now().date().isoformat()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f"  <url><loc>{base_url}/</loc><lastmod>{today}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>",
        f"  <url><loc>{base_url}/news</loc><lastmod>{today}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>",
        f"  <url><loc>{base_url}/trend</loc><lastmod>{today}</lastmod><changefreq>hourly</changefreq><priority>0.8</priority></url>",
        f"  <url><loc>{base_url}/search</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>0.7</priority></url>",
        f"  <url><loc>{base_url}/ai</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>0.6</priority></url>",
        f"  <url><loc>{base_url}/about</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.5</priority></url>",
        f"  <url><loc>{base_url}/personas</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.5</priority></url>",
    ]
    for slug in _CATEGORY_SLUGS:
        lines.append(
            f"  <url><loc>{base_url}/topics/{slug}</loc><lastmod>{today}</lastmod>"
            f"<changefreq>hourly</changefreq><priority>0.8</priority></url>"
        )

    for article in list(articles or [])[:5000]:
        lastmod = _article_lastmod(article, today)
        priority = "0.9" if getattr(article, "category", "") == "研究・論文" else "0.8"
        article_id = getattr(article, 'id', '') or ''
        title = getattr(article, 'title', '') or ''
        slug = _slugify_for_sitemap(title, article_id)
        lines.append(
            f"  <url><loc>{base_url}/topic/{slug}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>weekly</changefreq><priority>{priority}</priority></url>"
        )
    lines.append("</urlset>")
    return "\n".join(lines)


def write_sitemap_snapshot(articles: list, site_url: str | None = None) -> str | None:
    base_url = (site_url or _default_site_url()).strip().rstrip("/")
    if not base_url:
        return None
    xml = build_sitemap_xml(base_url, articles)
    with _SITEMAP_LOCK:
        _SITEMAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SITEMAP_PATH.write_text(xml, encoding="utf-8")
    return xml


def render_sitemap(site_url: str, articles: list, *, persist: bool = True) -> str | None:
    """メモリ上の記事一覧から sitemap を生成し、必要ならスナップショットも更新。"""
    base_url = (site_url or "").strip().rstrip("/")
    if not base_url:
        return None
    xml = build_sitemap_xml(base_url, articles)
    if persist:
        try:
            with _SITEMAP_LOCK:
                _SITEMAP_PATH.parent.mkdir(parents=True, exist_ok=True)
                _SITEMAP_PATH.write_text(xml, encoding="utf-8")
        except Exception as e:
            logger.warning("sitemap スナップショット書き込み失敗: %s", e)
    return xml


def read_sitemap_snapshot() -> str | None:
    with _SITEMAP_LOCK:
        if not _SITEMAP_PATH.exists():
            return None
        try:
            return _SITEMAP_PATH.read_text(encoding="utf-8")
        except Exception:
            return None
