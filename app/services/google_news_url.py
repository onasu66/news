"""Google News のラッパー URL を元記事 URL に解決する。

2024年以降の `news.google.com/rss/articles/CBMi...` 形式は
単純な HTTP リダイレクトでは解決できず、batchexecute デコードが必要。
"""
from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)

_GOOGLE_NEWS_ARTICLE_RE = re.compile(
    r"^https?://news\.google\.com/(?:rss/)?articles/[^?\s]+",
    re.I,
)

_last_decode_at = 0.0
_DECODE_MIN_INTERVAL_SEC = 0.35


def is_google_news_article_url(url: str) -> bool:
    u = (url or "").strip()
    return bool(u and _GOOGLE_NEWS_ARTICLE_RE.match(u))


def _throttle_decode() -> None:
    global _last_decode_at
    now = time.monotonic()
    wait = _DECODE_MIN_INTERVAL_SEC - (now - _last_decode_at)
    if wait > 0:
        time.sleep(wait)
    _last_decode_at = time.monotonic()


def _decode_via_googlenewsdecoder(url: str) -> str | None:
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        logger.warning("googlenewsdecoder 未インストール。pip install googlenewsdecoder")
        return None
    _throttle_decode()
    try:
        result = gnewsdecoder(url, interval=0)
        if result.get("status") and result.get("decoded_url"):
            decoded = str(result["decoded_url"]).strip()
            if decoded.startswith("http") and "news.google.com" not in decoded:
                return decoded
    except Exception as e:
        logger.debug("googlenewsdecoder 失敗 %s: %s", url[:60], e)
    return None


def _decode_via_httpx_redirect(url: str, timeout: float = 10.0) -> str | None:
    """旧形式 URL 向けフォールバック（リダイレクトが効く場合のみ）。"""
    try:
        import httpx

        resp = httpx.get(
            url,
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ja,en;q=0.9",
            },
        )
        final = str(resp.url).strip()
        if final and "news.google.com" not in final:
            return final
    except Exception as e:
        logger.debug("Google News リダイレクト失敗 %s: %s", url[:60], e)
    return None


def resolve_google_news_url(url: str, *, timeout: float = 15.0) -> str:
    """Google News ラッパーなら元記事 URL を返す。失敗時は入力 URL をそのまま返す。"""
    u = (url or "").strip()
    if not is_google_news_article_url(u):
        return u

    decoded = _decode_via_googlenewsdecoder(u)
    if decoded:
        logger.info("Google News URL 解決: %s → %s", u[:55], decoded[:80])
        return decoded

    redirected = _decode_via_httpx_redirect(u, timeout=timeout)
    if redirected:
        logger.info("Google News リダイレクト解決: %s → %s", u[:55], redirected[:80])
        return redirected

    logger.warning("Google News URL を解決できませんでした: %s", u[:80])
    return u
