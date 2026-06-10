"""カテゴリ別の固定画像URL（picsum フォールバック廃止）。"""
from __future__ import annotations

# カテゴリ名 → ファイルスラッグ
_CATEGORY_SLUGS: dict[str, str] = {
    "テクノロジー": "tech",
    "国内": "domestic",
    "国際": "international",
    "政治・社会": "politics",
    "研究・論文": "research",
    "エンタメ": "entertainment",
    "スポーツ": "sports",
}

_DEFAULT_SLUG = "default"


def category_slug(category: str | None) -> str:
    c = (category or "").strip()
    return _CATEGORY_SLUGS.get(c, _DEFAULT_SLUG)


def category_og_path(category: str | None) -> str:
    """OGP用（1200×630）の静的パス。"""
    return f"/static/og/og-{category_slug(category)}.jpg"


def category_card_path(category: str | None) -> str:
    """カード用（400×225）の静的パス。"""
    return f"/static/og/card-{category_slug(category)}.jpg"


def default_og_path() -> str:
    return category_og_path(None)


def default_card_path() -> str:
    return category_card_path(None)


def _absolute(url: str, site_url: str = "") -> str:
    if not url:
        return ""
    if url.startswith("http"):
        return url
    base = (site_url or "").rstrip("/")
    return f"{base}{url}" if base else url


def is_placeholder_image(url: str | None) -> bool:
    """picsum や空URLなど、実画像でないものか。"""
    u = (url or "").strip().lower()
    if not u:
        return True
    if u.startswith("/static/og/"):
        return False
    return "picsum.photos" in u


def resolve_item_image_url(
    *,
    image_url: str | None,
    category: str | None,
    item_id: str | None = None,
    width: int = 400,
    height: int = 225,
    site_url: str = "",
    og: bool = False,
) -> str:
    """
    記事の表示用画像URLを決定する。
    実画像（外部URL）があれば優先、なければカテゴリ別固定画像。
    """
    raw = (image_url or "").strip()
    if raw.startswith("http") and "picsum.photos" not in raw.lower():
        return raw
    if raw.startswith("/static/"):
        return _absolute(raw, site_url)

    path = category_og_path(category) if og or (width >= 800 and height >= 600) else category_card_path(category)
    return _absolute(path, site_url)


def get_image_url(
    path: str,
    width: int = 800,
    height: int = 450,
    category: str | None = None,
    site_url: str = "",
) -> str:
    """
    後方互換の get_image_url 置き換え。
    path が URL ならそのまま、否则カテゴリ画像（なければ default）。
    """
    raw = (path or "").strip()
    if raw.startswith("http") and "picsum.photos" not in raw.lower():
        return raw
    if raw.startswith("/static/"):
        return _absolute(raw, site_url)

    use_og = width >= 800 and height >= 450
    p = category_og_path(category) if use_og else category_card_path(category)
    return _absolute(p, site_url)
