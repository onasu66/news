"""Claude 記事選定ログを Notion データベースに記録する。

DB 構造（必須）:
  タイトル (title) / URL (url) / 選定理由 (rich_text)
  スロット (select) / 日時 (date) / 処理結果 (select) / カテゴリ (select) / ソース (rich_text)

DB 構造（任意・列を追加すると書き込まれる）:
  Xポスト (rich_text) / 記事リンク (url)
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


_OPTIONAL_PROPS = ("Xポスト", "記事リンク")


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
    x_post: str | None = None,
    site_article_url: str | None = None,
) -> bool:
    """Notion DB に1件追加。失敗時は警告ログのみで例外を投げない。"""
    creds = _get_credentials()
    if not creds:
        return False
    token, db_id = creds

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    date_str = published or now_iso

    properties: dict = {
        "タイトル": {"title": [{"text": {"content": (title or "")[:200]}}]},
        "URL": {"url": url or None},
        "選定理由": {"rich_text": [{"text": {"content": (reason or "")[:2000]}}]},
        "スロット": {"select": {"name": slot or "unknown"}},
        "日時": {"date": {"start": date_str}},
        "処理結果": {"select": {"name": result}},
        "カテゴリ": {"select": {"name": (category or "その他")[:50]}},
        "ソース": {"rich_text": [{"text": {"content": (source or "")[:200]}}]},
    }
    if x_post:
        properties["Xポスト"] = {"rich_text": [{"text": {"content": x_post[:2000]}}]}
    if site_article_url:
        properties["記事リンク"] = {"url": site_article_url}

    def _try_request(props: dict) -> bool:
        _notion_request("/pages", {"parent": {"database_id": db_id}, "properties": props}, token)
        return True

    try:
        return _try_request(properties)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        # Xポスト / 記事リンク 列が DB に未追加の場合、任意列を除いて再試行
        if e.code == 400 and any(k in body for k in _OPTIONAL_PROPS):
            fallback = {k: v for k, v in properties.items() if k not in _OPTIONAL_PROPS}
            try:
                result_ok = _try_request(fallback)
                logger.info("Notion ログ: 任意列（Xポスト/記事リンク）なしで保存 — DB に列を追加してください")
                return result_ok
            except Exception as e2:
                logger.warning("Notion ログ再試行失敗: %s", e2)
        else:
            logger.warning("Notion ログ失敗 HTTP %d: %s", e.code, body[:300])
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
            x_post=art.get("x_post") or None,
            site_article_url=art.get("site_article_url") or None,
        )
        if success:
            ok_count += 1
    logger.info("Notion ログ記録: %d / %d 件", ok_count, len(articles))
