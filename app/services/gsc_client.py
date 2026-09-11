"""Google Search Console (Search Analytics API) クライアント。

サービスアカウント（読み取り専用）で検索パフォーマンスを取得する。
認証情報が未設定でも import・呼び出しは壊れず、status で理由を返す。

認証情報の置き場所（優先順）:
  1) GSC_SERVICE_ACCOUNT_JSON  … JSON 文字列そのもの（Render等の env 用）
  2) GSC_SERVICE_ACCOUNT_FILE  … JSON ファイルパス
  3) ~/.config/seo-rank-watch/service-account.json

対象プロパティ:
  GSC_SITE_URL（例 "sc-domain:example.com" / "https://example.com/"）
  未設定なら SITE_URL から "sc-domain:<host>" を組み立てる。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
_API = "https://searchconsole.googleapis.com/webmasters/v3/sites"

# GSC のデータ確定は2〜3日遅れる。未確定分は取り込まない。
DATA_LAG_DAYS = 3

_DEFAULT_KEY_PATH = Path.home() / ".config" / "seo-rank-watch" / "service-account.json"


class GscNotConfigured(RuntimeError):
    """認証情報またはプロパティが未設定。"""


def _site_url() -> str:
    explicit = os.getenv("GSC_SITE_URL", "").strip()
    if explicit:
        return explicit
    from app.config import settings

    base = (getattr(settings, "SITE_URL", "") or "").strip()
    if not base:
        return ""
    host = urlparse(base if "://" in base else f"https://{base}").netloc
    return f"sc-domain:{host}" if host else ""


def _load_key() -> dict[str, Any]:
    raw = os.getenv("GSC_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            raise GscNotConfigured(f"GSC_SERVICE_ACCOUNT_JSON が不正な JSON です: {e}") from e

    path_str = os.getenv("GSC_SERVICE_ACCOUNT_FILE", "").strip()
    path = Path(path_str).expanduser() if path_str else _DEFAULT_KEY_PATH
    if not path.exists():
        raise GscNotConfigured(
            f"サービスアカウント鍵が見つかりません: {path}"
            "（GSC_SERVICE_ACCOUNT_JSON か GSC_SERVICE_ACCOUNT_FILE を設定してください）"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise GscNotConfigured(f"サービスアカウント鍵の読み込みに失敗: {e}") from e


def _access_token() -> str:
    """サービスアカウント鍵からアクセストークンを取得。"""
    try:
        from google.oauth2 import service_account  # type: ignore
        from google.auth.transport.requests import Request as GoogleRequest  # type: ignore
    except ImportError as e:
        raise GscNotConfigured(
            "google-auth が未インストールです（pip install google-auth requests）"
        ) from e

    info = _load_key()
    creds = service_account.Credentials.from_service_account_info(info, scopes=[_SCOPE])
    creds.refresh(GoogleRequest())
    return creds.token


def is_configured() -> bool:
    """鍵とプロパティが揃っているか（通信はしない）。"""
    if not _site_url():
        return False
    try:
        _load_key()
        return True
    except GscNotConfigured:
        return False


def status() -> dict[str, Any]:
    """ダッシュボード表示用の設定状況。例外を投げない。"""
    site = _site_url()
    info: dict[str, Any] = {"site_url": site, "configured": False, "error": None}
    if not site:
        info["error"] = "GSC_SITE_URL（または SITE_URL）が未設定です"
        return info
    try:
        key = _load_key()
        info["service_account_email"] = key.get("client_email") or ""
        info["configured"] = True
    except GscNotConfigured as e:
        info["error"] = str(e)
    return info


def check_access() -> dict[str, Any]:
    """疎通確認。プロパティに実際にアクセスできるかを見る。"""
    site = _site_url()
    if not site:
        return {"ok": False, "error": "GSC_SITE_URL（または SITE_URL）が未設定です"}
    try:
        rows = fetch_query_stats(days=7, row_limit=1)
    except GscNotConfigured as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "site_url": site, "sample_rows": len(rows)}


def date_range(days: int) -> tuple[str, str]:
    """確定済みデータのみを含む [start, end]（ISO 日付）。"""
    end = date.today() - timedelta(days=DATA_LAG_DAYS)
    start = end - timedelta(days=max(1, days) - 1)
    return start.isoformat(), end.isoformat()


def _search_analytics(body: dict[str, Any]) -> list[dict[str, Any]]:
    import httpx

    site = _site_url()
    if not site:
        raise GscNotConfigured("GSC_SITE_URL（または SITE_URL）が未設定です")

    token = _access_token()
    from urllib.parse import quote

    url = f"{_API}/{quote(site, safe='')}/searchAnalytics/query"
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 403:
        raise PermissionError(
            f"プロパティ {site} にアクセスできません。"
            "Search Console でサービスアカウントのメールアドレスを"
            "「フルユーザー」または「制限付きユーザー」として追加してください。"
        )
    resp.raise_for_status()
    return resp.json().get("rows") or []


def fetch_query_stats(
    *,
    days: int = 28,
    row_limit: int = 500,
    with_page: bool = False,
) -> list[dict[str, Any]]:
    """クエリ別の clicks / impressions / ctr / position。

    with_page=True なら (query, page) の組で返す。
    """
    start, end = date_range(days)
    dimensions = ["query", "page"] if with_page else ["query"]
    rows = _search_analytics(
        {
            "startDate": start,
            "endDate": end,
            "dimensions": dimensions,
            "rowLimit": min(int(row_limit), 25000),
            "dataState": "final",
        }
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        keys = r.get("keys") or []
        if not keys:
            continue
        item: dict[str, Any] = {
            "query": keys[0],
            "clicks": int(r.get("clicks") or 0),
            "impressions": int(r.get("impressions") or 0),
            # GSC の ctr は 0..1。既存の貼り付け由来データに合わせて % に揃える。
            "ctr": round(float(r.get("ctr") or 0.0) * 100, 2),
            "position": round(float(r.get("position") or 0.0), 1),
        }
        if with_page and len(keys) > 1:
            item["page"] = keys[1]
        out.append(item)
    return out


def fetch_page_stats(*, days: int = 28, row_limit: int = 200) -> list[dict[str, Any]]:
    """ページ別の実績。どの記事が効いているかを見る用。"""
    start, end = date_range(days)
    rows = _search_analytics(
        {
            "startDate": start,
            "endDate": end,
            "dimensions": ["page"],
            "rowLimit": min(int(row_limit), 25000),
            "dataState": "final",
        }
    )
    return [
        {
            "page": (r.get("keys") or [""])[0],
            "clicks": int(r.get("clicks") or 0),
            "impressions": int(r.get("impressions") or 0),
            "ctr": round(float(r.get("ctr") or 0.0) * 100, 2),
            "position": round(float(r.get("position") or 0.0), 1),
        }
        for r in rows
        if r.get("keys")
    ]


def fetch_totals(*, days: int = 28) -> dict[str, Any]:
    """サイト全体の合計。サマリタイル用。"""
    start, end = date_range(days)
    rows = _search_analytics(
        {"startDate": start, "endDate": end, "dimensions": [], "dataState": "final"}
    )
    if not rows:
        return {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0,
                "start": start, "end": end}
    r = rows[0]
    return {
        "clicks": int(r.get("clicks") or 0),
        "impressions": int(r.get("impressions") or 0),
        "ctr": round(float(r.get("ctr") or 0.0) * 100, 2),
        "position": round(float(r.get("position") or 0.0), 1),
        "start": start,
        "end": end,
    }
