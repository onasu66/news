from __future__ import annotations

import os
from pathlib import Path

from app.services.sitemap_xml import build_sitemap_xml


def _site_url() -> str:
    return (os.getenv("SITE_URL", "") or "https://example.com").strip().rstrip("/")


def generate_sitemap_xml() -> str:
    return build_sitemap_xml(_site_url())


def main() -> None:
    xml = generate_sitemap_xml()
    out = Path(__file__).resolve().parent.parent / "app" / "static" / "sitemap.xml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(xml, encoding="utf-8")
    print(f"[sitemap] generated: {out}")


if __name__ == "__main__":
    main()
