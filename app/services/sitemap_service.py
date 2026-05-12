"""Sitemap generation and snapshot storage."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Lock

from app.config import settings

_SITEMAP_LOCK = Lock()
_SITEMAP_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "sitemap.xml"


def sitemap_snapshot_path() -> Path:
    return _SITEMAP_PATH


def _default_site_url() -> str:
    return (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")


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
    for article in list(articles or [])[:5000]:
        try:
            lastmod = (
                article.published.date().isoformat()
                if getattr(article, "published", None) and hasattr(article.published, "date")
                else today
            )
        except Exception:
            lastmod = today
        priority = "0.9" if getattr(article, "category", "") == "研究・論文" else "0.8"
        lines.append(
            f"  <url><loc>{base_url}/topic/{article.id}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>never</changefreq><priority>{priority}</priority></url>"
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


def read_sitemap_snapshot() -> str | None:
    with _SITEMAP_LOCK:
        if not _SITEMAP_PATH.exists():
            return None
        try:
            return _SITEMAP_PATH.read_text(encoding="utf-8")
        except Exception:
            return None
