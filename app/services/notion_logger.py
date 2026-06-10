"""Claude 記事選定ログを Notion データベースに記録する。

DB 構造:
  タイトル (title) / URL (url) / 選定理由 (rich_text)
  スロット (select) / 日時 (date) / 処理結果 (select) / カテゴリ (select) / ソース (rich_text)
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Literal

logger = logging.getLogger(__name__)

_NOTION_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"

ResultKind = Literal["成功", "スキップ", "失敗"]


def _get_credentials() -> tuple[str, str] | None:
    """(token, database_id) を返す。未設定なら None。"""
    try:
        from app.config import settings
        token = getattr(settings, "NOTION_API_KEY", "") or os.getenv("NOTION_API_KEY", "")
        db_id = getattr(settings, "NOTION_DATABASE_ID", "") or os.getenv("NOTION_DATABASE_ID", "")
    except Exception:
        token = os.getenv("NOTION_API_KEY", "")
        db_id = os.getenv("NOTION_DATABASE_ID", "")
    if not token or not db_id:
        return None
    return token.strip(), db_id.strip()


def _notion_request(path: str, payload: dict, token: str) -> dict:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        f"{_NOTION_API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": _NOTION_VERSION,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def log_article_selected(
    *,
    title: str,
    url: str,
    reason: str,
    slot: str,
    category: str,
    source: str,
    result: ResultKind = "スキップ",
    published: str | None = None,
) -> bool:
    """Notion DB に1件追加。失敗時は警告ログのみで例外を投げない。"""
    creds = _get_credentials()
    if not creds:
        return False
    token, db_id = creds

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    date_str = published or now_iso

    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            "タイトル": {"title": [{"text": {"content": (title or "")[:200]}}]},
            "URL": {"url": url or None},
            "選定理由": {"rich_text": [{"text": {"content": (reason or "")[:2000]}}]},
            "スロット": {"select": {"name": slot or "unknown"}},
            "日時": {"date": {"start": date_str}},
            "処理結果": {"select": {"name": result}},
            "カテゴリ": {"select": {"name": (category or "その他")[:50]}},
            "ソース": {"rich_text": [{"text": {"content": (source or "")[:200]}}]},
        },
    }
    try:
        _notion_request("/pages", payload, token)
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        logger.warning("Notion ログ失敗 HTTP %d: %s", e.code, body)
    except Exception as e:
        logger.warning("Notion ログ失敗: %s", e)
    return False


def log_research_batch(
    articles: list[dict],
    *,
    slot: str,
    results: dict[str, ResultKind] | None = None,
) -> None:
    """curated_articles.json の1バッチ分をまとめて Notion に記録する。

    articles: curated_articles.json のエントリリスト（reason フィールド付き）
    results:  {url: ResultKind} の辞書。処理後に呼ぶと成功/失敗が記録できる。
    """
    creds = _get_credentials()
    if not creds:
        logger.debug("Notion 未設定のためログをスキップ")
        return
    results = results or {}
    ok_count = 0
    for art in articles:
        url = art.get("url", "")
        result: ResultKind = results.get(url, "スキップ")
        success = log_article_selected(
            title=art.get("title", ""),
            url=url,
            reason=art.get("reason", ""),
            slot=slot,
            category=art.get("category", ""),
            source=art.get("source", ""),
            result=result,
            published=art.get("published"),
        )
        if success:
            ok_count += 1
    logger.info("Notion ログ記録: %d / %d 件", ok_count, len(articles))
