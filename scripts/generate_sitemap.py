from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from app.services.news_aggregator import NewsAggregator


def _site_url() -> str:
    return (os.getenv("SITE_URL", "") or "https://example.com").strip().rstrip("/")


def generate_sitemap_xml() -> str:
    site_url = _site_url()
    articles = NewsAggregator.get_news()
    today = datetime.now().date().isoformat()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f"  <url><loc>{site_url}/</loc><lastmod>{today}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>",
        f"  <url><loc>{site_url}/news</loc><lastmod>{today}</lastmod><changefreq>hourly</changefreq><priority>0.95</priority></url>",
        f"  <url><loc>{site_url}/ai</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>0.7</priority></url>",
        f"  <url><loc>{site_url}/search</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>0.6</priority></url>",
    ]
    for a in articles[:5000]:
        lastmod = today
        try:
            if getattr(a, "published", None) and hasattr(a.published, "date"):
                lastmod = a.published.date().isoformat()
        except Exception:
            pass
        lines.append(
            f"  <url><loc>{site_url}/topic/{a.id}</loc><lastmod>{lastmod}</lastmod><changefreq>weekly</changefreq><priority>0.8</priority></url>"
        )
    lines.append("</urlset>")
    return "\n".join(lines)


def main() -> None:
    xml = generate_sitemap_xml()
    out = Path(__file__).resolve().parent.parent / "app" / "static" / "sitemap.xml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(xml, encoding="utf-8")
    print(f"[sitemap] generated: {out}")


if __name__ == "__main__":
    main()
