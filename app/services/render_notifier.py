"""Render 本番へ「新着反映」通知を送る（案A: ポーリングを減らす）。"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)


def notify_render_cache_refresh(*, reason: str = "articles_added", timeout_sec: float = 8.0) -> bool:
    """Render（SITE_URL）へキャッシュ更新通知を送る。

    成功すると Render 側が NewsAggregator.sync_list_cache_from_db(force=True) などを実行して
    メモリキャッシュを更新する。失敗しても記事自体は Firestore に保存済みなので致命的ではない。
    """
    site_url = (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
    secret = (getattr(settings, "ADMIN_SECRET", "") or "").strip()
    if not site_url or not secret:
        return False
    url = site_url + "/api/admin/cache/refresh"
    try:
        import httpx

        with httpx.Client(timeout=timeout_sec, follow_redirects=True) as client:
            resp = client.post(url, headers={"X-Admin-Secret": secret}, json={"reason": reason})
        if resp.status_code >= 200 and resp.status_code < 300:
            logger.info("Render 通知成功: %s (%s)", url, reason)
            return True
        logger.warning("Render 通知失敗: %s status=%d body=%s", url, resp.status_code, (resp.text or "")[:200])
        return False
    except Exception as e:
        logger.warning("Render 通知エラー: %s", e)
        return False

