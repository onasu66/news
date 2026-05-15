"""記事化前後の素材・生成物が十分かどうかを判定する。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _settings_int(name: str, default: int) -> int:
    try:
        from app.config import settings

        return max(0, int(getattr(settings, name, default) or default))
    except Exception:
        return default


def build_source_text(title: str, summary: str, body: str | None) -> str:
    """AI に渡す前の素材テキスト（タイトル・要約・本文）。"""
    parts: list[str] = []
    t = (title or "").strip()
    s = (summary or "").strip()
    b = (body or "").strip()
    if t:
        parts.append(t)
    if s:
        parts.append(s)
    if b:
        parts.append(b)
    return "\n\n".join(parts)


def source_material_length(title: str, summary: str, body: str | None) -> int:
    return len(build_source_text(title, summary, body))


def is_source_material_sufficient(
    title: str,
    summary: str,
    body: str | None,
    *,
    is_paper: bool = False,
) -> bool:
    """
    記事化に進んでよい素材か。
    本文が取れていれば要約は短くても可。本文が無い場合は要約が十分長い必要がある。
    """
    t = (title or "").strip()
    s = (summary or "").strip()
    b = (body or "").strip()
    if not t:
        return False

    body_len = len(b)
    summary_len = len(s)
    total = source_material_length(title, summary, body)

    min_total = _settings_int("ARTICLE_MIN_SOURCE_CHARS", 900)
    min_summary = _settings_int(
        "ARTICLE_MIN_SUMMARY_CHARS_PAPER" if is_paper else "ARTICLE_MIN_SUMMARY_CHARS",
        400 if is_paper else 280,
    )
    min_body = _settings_int("ARTICLE_MIN_BODY_CHARS", 450)

    if body_len >= min_body:
        return total >= min(600, min_total) and summary_len >= 80

    if summary_len >= min_summary and total >= min_total:
        return True

    logger.info(
        "素材不足: total=%d summary=%d body=%d (need total>=%d summary>=%d or body>=%d)",
        total,
        summary_len,
        body_len,
        min_total,
        min_summary,
        min_body,
    )
    return False


def blocks_text_char_count(blocks: list[dict[str, Any]] | None) -> int:
    if not blocks:
        return 0
    total = 0
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") not in ("text", "explain"):
            continue
        total += len(str(b.get("content") or "").strip())
    return total


def is_generated_article_sufficient(blocks: list[dict[str, Any]] | None) -> bool:
    """ミドルマン記事（text+explain）がサイト掲載に足りる分量か。"""
    if not blocks:
        return False
    text_chars = 0
    explain_count = 0
    for b in blocks:
        if not isinstance(b, dict):
            continue
        typ = b.get("type")
        content = str(b.get("content") or "").strip()
        if not content or content.startswith("（"):
            continue
        if typ == "text":
            text_chars += len(content)
        elif typ == "explain":
            explain_count += 1

    min_text = _settings_int("ARTICLE_MIN_GENERATED_TEXT_CHARS", 1200)
    min_explains = _settings_int("ARTICLE_MIN_EXPLAIN_COUNT", 3)

    if text_chars < min_text:
        logger.info("生成記事が短すぎる: text=%d字 (最低 %d字)", text_chars, min_text)
        return False
    if explain_count < min_explains:
        logger.info("explain 不足: %d個 (最低 %d個)", explain_count, min_explains)
        return False
    return True


def is_navigator_sufficient(navigator_blocks: list[dict[str, Any]] | None) -> bool:
    """理解ナビゲーター5項目の合計が薄すぎないか。"""
    if not navigator_blocks:
        return False
    total = 0
    for b in navigator_blocks:
        if isinstance(b, dict) and b.get("content"):
            total += len(str(b["content"]).strip())
    min_nav = _settings_int("ARTICLE_MIN_NAVIGATOR_CHARS", 500)
    if total < min_nav:
        logger.info("ナビゲーター要約が短すぎる: %d字 (最低 %d字)", total, min_nav)
        return False
    return True
