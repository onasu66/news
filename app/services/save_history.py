"""記事保存の成功・失敗履歴（起動中のメモリ保持・ブラウザで確認用）"""
from datetime import datetime
from typing import Optional

_MAX_ENTRIES = 200
_entries: list[dict] = []


def add_entry(
    article_id: str,
    title: str,
    success: bool,
    *,
    error: Optional[str] = None,
    source: str = "rss_seed",
) -> None:
    """1件の保存試行を記録する"""
    global _entries
    entry = {
        "article_id": article_id,
        "title": (title or "")[:200],
        "success": success,
        "error": (error or "")[:500] if error else None,
        "source": source,
        "at": datetime.now().isoformat(),
    }
    _entries.append(entry)
    if len(_entries) > _MAX_ENTRIES:
        _entries.pop(0)


def get_entries() -> list[dict]:
    """記録済みの履歴を新しい順で返す"""
    return list(reversed(_entries))
