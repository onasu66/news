"""Sitemap generation and snapshot storage."""
from __future__ import annotations

import logging
from datetime import datetime
from html import escape
from pathlib import Path
from threading import Lock

from app.config import settings

logger = logging.getLogger(__name__)

_SITEMAP_LOCK = Lock()
_SITEMAP_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "sitemap.xml"

_STATIC_URLS = [
    ("/", "hourly", "1.0"),
    ("/news", "hourly", "1.0"),
    ("/trend", "hourly", "0.8"),
    ("/ai", "daily", "0.7"),
    ("/about", "monthly", "0.5"),
    ("/personas", "monthly", "0.5"),
]
_CATEGORY_SLUGS = ["ai", "tech", "science", "world", "social", "sports", "entertainment"]


def _high_intent_keywords() -> tuple[str, ...]:
    try:
        from app.services.seo_keywords_config import get_sitemap_high_intent

        return get_sitemap_high_intent()
    except Exception:
        return ("AI", "論文", "研究")


def _low_intent_keywords() -> tuple[str, ...]:
    try:
        from app.services.seo_keywords_config import get_sitemap_low_intent

        return get_sitemap_low_intent()
    except Exception:
        return ("占い", "ゴシップ")


def _setting_int(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(getattr(settings, name, default) or default)
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


_SITEMAP_ARTICLE_LIMIT = _setting_int("SITEMAP_ARTICLE_LIMIT", 1000, 50, 1000)
_SITEMAP_MIN_TEXT_LENGTH = _setting_int("SITEMAP_MIN_TEXT_LENGTH", 180, 80, 600)


def sitemap_snapshot_path() -> Path:
    return _SITEMAP_PATH


def _default_site_url() -> str:
    return (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")


def _xml(value: str) -> str:
    return escape(value or "", quote=True)


def _article_lastmod(article, today: str) -> str:
    for attr in ("added_at", "published"):
        dt = getattr(article, attr, None)
        if not dt:
            continue
        try:
            if hasattr(dt, "date"):
                return dt.date().isoformat()
            if hasattr(dt, "isoformat"):
                return str(dt)[:10]
            text = str(dt).strip()
            if len(text) >= 10:
                return text[:10]
        except Exception:
            continue
    return today


def _slugify_title(title: str, max_len: int = 55) -> str:
    import re as _re

    s = (title or "").strip()
    s = _re.sub(
        r'[「」『』【】〈〉《》\[\]{}()（）<>""\'\'`！!？?。、，,．\.。:;：；・＊*＋+＝=＆&＠@＃#｜|＼\\／/]',
        "",
        s,
    )
    s = _re.sub(r"[\s\u3000　]+", "-", s)
    s = _re.sub(r"-+", "-", s).strip("-")
    return s[:max_len] if s else ""


def _article_url_path(article) -> str:
    """記事URLパス（routers.news への依存なし）。"""
    if isinstance(article, dict):
        article_id = str(article.get("id") or "")
        title = str(article.get("title") or "")
    else:
        article_id = getattr(article, "id", "") or ""
        title = getattr(article, "title", "") or ""
    slug = _slugify_title(title)
    if slug:
        suffix = article_id[-6:] if len(article_id) >= 6 else article_id
        return f"/topic/{slug}-{suffix}" if suffix else f"/topic/{slug}"
    return f"/topic/{article_id}"


def _article_text(article) -> str:
    parts = [
        getattr(article, "title", "") or "",
        getattr(article, "summary", "") or "",
        getattr(article, "category", "") or "",
        getattr(article, "source", "") or "",
    ]
    return " ".join(str(part).strip() for part in parts if part).strip()


def _article_score(article) -> int:
    title = str(getattr(article, "title", "") or "").strip()
    summary = str(getattr(article, "summary", "") or "").strip()
    category = str(getattr(article, "category", "") or "").strip()
    haystack = _article_text(article)

    score = 0
    if len(title) >= 14:
        score += 2
    if len(summary) >= 120:
        score += 3
    if any(keyword in haystack for keyword in _high_intent_keywords()):
        score += 5
    if "研究" in category or "論文" in category:
        score += 4
    if "テクノロジ" in category or "科学" in category:
        score += 2
    if any(keyword in haystack for keyword in _low_intent_keywords()):
        score -= 6
    return score


def is_sitemap_article(article) -> bool:
    article_id = str(getattr(article, "id", "") or "").strip()
    title = str(getattr(article, "title", "") or "").strip()
    if not article_id or not title:
        return False
    if len(_article_text(article)) < _SITEMAP_MIN_TEXT_LENGTH:
        return False
    return _article_score(article) >= 4


def select_sitemap_articles(articles: list) -> list:
    today = datetime.now().date().isoformat()
    ranked = []
    for index, article in enumerate(articles or []):
        if not is_sitemap_article(article):
            continue
        ranked.append(
            (
                _article_score(article),
                _article_lastmod(article, today),
                -index,
                article,
            )
        )
    ranked.sort(key=lambda item: item[:3], reverse=True)
    return [article for _, _, _, article in ranked[:_SITEMAP_ARTICLE_LIMIT]]


def build_sitemap_xml(site_url: str, articles: list) -> str:
    base_url = (site_url or "").strip().rstrip("/")
    today = datetime.now().date().isoformat()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]

    for path, changefreq, priority in _STATIC_URLS:
        lines.append(
            f"  <url><loc>{_xml(base_url + path)}</loc><lastmod>{today}</lastmod>"
            f"<changefreq>{changefreq}</changefreq><priority>{priority}</priority></url>"
        )

    for slug in _CATEGORY_SLUGS:
        lines.append(
            f"  <url><loc>{_xml(f'{base_url}/topics/{slug}')}</loc><lastmod>{today}</lastmod>"
            f"<changefreq>daily</changefreq><priority>0.7</priority></url>"
        )

    for article in select_sitemap_articles(articles):
        lastmod = _article_lastmod(article, today)
        category = getattr(article, "category", "") or ""
        priority = "0.9" if ("研究" in category or "論文" in category) else "0.8"
        loc = f"{base_url}{_article_url_path(article)}"
        lines.append(
            f"  <url><loc>{_xml(loc)}</loc><lastmod>{lastmod}</lastmod>"
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
            logger.warning("sitemap snapshot write failed: %s", e)
    return xml


def read_sitemap_snapshot() -> str | None:
    with _SITEMAP_LOCK:
        if not _SITEMAP_PATH.exists():
            return None
        try:
            return _SITEMAP_PATH.read_text(encoding="utf-8")
        except Exception:
            return None
