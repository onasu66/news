"""IndexNow: 新規・更新 URL を Bing 等の検索エンジンへ通知する。"""
from __future__ import annotations

import logging
import re
import threading
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger(__name__)

_INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
_KEY_RE = re.compile(r"^[a-zA-Z0-9-]{8,128}$")
_pending_lock = threading.Lock()
_pending_ids: set[str] = set()


def indexnow_key() -> str:
    """IndexNow 用キー（環境変数 INDEXNOW_KEY）。未設定なら空。"""
    return (getattr(settings, "INDEXNOW_KEY", "") or "").strip()


def is_indexnow_enabled() -> bool:
    if str(getattr(settings, "INDEXNOW_ENABLED", "true")).strip().lower() in ("0", "false", "no"):
        return False
    return bool(indexnow_key() and _site_url())


def _site_url() -> str:
    return (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")


def topic_absolute_url(article_id: str) -> str | None:
    base = _site_url()
    aid = (article_id or "").strip()
    if not base or not aid:
        return None
    return f"{base}/topic/{aid}"


def queue_indexnow_article(article_id: str) -> None:
    """記事 ID を通知キューに追加（バッチ終了時にまとめて送る）。"""
    if not is_indexnow_enabled():
        return
    aid = (article_id or "").strip()
    if not aid:
        return
    with _pending_lock:
        _pending_ids.add(aid)


def flush_indexnow_queue() -> bool:
    """キュー内の記事 URL を IndexNow API へ送信。"""
    with _pending_lock:
        ids = list(_pending_ids)
        _pending_ids.clear()
    if not ids:
        return False
    urls = []
    for aid in ids:
        u = topic_absolute_url(aid)
        if u:
            urls.append(u)
    return submit_indexnow_urls(urls)


def notify_indexnow_article(article_id: str) -> None:
    """1件を非同期で IndexNow 通知。"""
    if not is_indexnow_enabled():
        return
    u = topic_absolute_url(article_id)
    if not u:
        return
    threading.Thread(
        target=submit_indexnow_urls,
        args=([u],),
        daemon=True,
        name="indexnow-single",
    ).start()


def notify_indexnow_articles(article_ids: list[str]) -> None:
    """複数件を非同期でまとめて通知。"""
    if not is_indexnow_enabled():
        return
    urls = []
    for aid in article_ids or []:
        u = topic_absolute_url(aid)
        if u:
            urls.append(u)
    if not urls:
        return
    threading.Thread(
        target=submit_indexnow_urls,
        args=(urls,),
        daemon=True,
        name="indexnow-batch",
    ).start()


def submit_indexnow_urls(urls: list[str]) -> bool:
    """IndexNow API へ URL 一覧を POST（同期）。"""
    key = indexnow_key()
    base = _site_url()
    if not key or not base or not _KEY_RE.match(key):
        return False
    clean = []
    seen: set[str] = set()
    for u in urls or []:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        clean.append(u)
    if not clean:
        return False
    parsed = urlparse(base)
    host = parsed.netloc or parsed.path
    if not host:
        return False
    payload = {
        "host": host,
        "key": key,
        "keyLocation": f"{base}/{key}.txt",
        "urlList": clean[:10000],
    }
    try:
        import httpx

        with httpx.Client(timeout=12.0) as client:
            resp = client.post(
                _INDEXNOW_ENDPOINT,
                json=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
        if resp.status_code in (200, 202):
            logger.info("IndexNow 送信成功: %d URL (例: %s)", len(clean), clean[0][:80])
            return True
        logger.warning(
            "IndexNow 送信失敗: status=%s body=%s",
            resp.status_code,
            (resp.text or "")[:300],
        )
        return False
    except Exception as e:
        logger.warning("IndexNow 送信エラー: %s", e)
        return False
