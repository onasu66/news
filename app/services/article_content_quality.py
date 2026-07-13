"""記事化前後の素材・生成物が十分かどうかを判定する。"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ペイウォール・続き読み等（本文が途中までしか取れていない疑い）
_INCOMPLETE_BODY_MARKERS: tuple[str, ...] = (
    "続きを読む",
    "記事の続きは",
    "全文を読む",
    "全文表示",
    "会員限定",
    "有料会員",
    "有料記事",
    "この記事は有料",
    "ログインが必要",
    "ログインして",
    "無料会員登録",
    "プレミアム会員",
    "subscribe to continue",
    "subscribe to read",
    "read more",
    "sign in to read",
    "この先は有料",
    "有料エリア",
)

_INCOMPLETE_BODY_TAIL = re.compile(r"(?:…|\.\.\.|··)\s*$")


def _settings_int(name: str, default: int) -> int:
    try:
        from app.config import settings

        return max(0, int(getattr(settings, name, default) or default))
    except Exception:
        return default


def min_generated_text_chars() -> int:
    return _settings_int("ARTICLE_MIN_GENERATED_TEXT_CHARS", 600)


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


def _body_looks_like_teaser_only(body: str, summary: str) -> bool:
    """取得本文が要約の冒頭リードだけで、記事本体が欠けている疑い。"""
    b = (body or "").strip()
    s = (summary or "").strip()
    if not b or len(b) > 700:
        return False
    if len(b) >= len(s) * 0.9:
        return False
    prefix = b[: min(len(b), max(80, len(b) - 20))]
    if prefix and s.startswith(prefix):
        return True
    if len(b) >= 120 and b in s:
        return True
    return False


def is_incomplete_source_body(body: str | None, summary: str = "") -> bool:
    """
    本文がペイウォール・途中切断・リードのみの疑いがあるか。
    True のとき本文は素材量計算から除外し、要約の十分さで判定する。
    """
    b = (body or "").strip()
    if not b:
        return False

    lower = b.lower()
    for marker in _INCOMPLETE_BODY_MARKERS:
        if marker in b or marker in lower:
            return True

    tail = b[-100:]
    if _INCOMPLETE_BODY_TAIL.search(tail):
        return True

    if len(b) < 500 and re.search(r"続き(?:を)?(?:読|見)", b):
        return True

    if _body_looks_like_teaser_only(b, summary):
        return True

    return False


def is_source_material_sufficient(
    title: str,
    summary: str,
    body: str | None,
    *,
    is_paper: bool = False,
) -> bool:
    """
    記事化に進んでよい素材か。
    合計400字程度から可。ただし途中までしか取れていない本文は使わない。
    """
    t = (title or "").strip()
    s = (summary or "").strip()
    b = (body or "").strip()
    if not t:
        return False

    raw_body_len = len(b)
    if b and is_incomplete_source_body(b, s):
        logger.info(
            "素材途中切れの疑い（本文を除外）: body=%d",
            raw_body_len,
        )
        b = ""

    body_len = len(b)
    summary_len = len(s)
    total = source_material_length(title, summary, b or None)

    min_total = _settings_int("ARTICLE_MIN_SOURCE_CHARS", 400)
    min_summary = _settings_int(
        "ARTICLE_MIN_SUMMARY_CHARS_PAPER" if is_paper else "ARTICLE_MIN_SUMMARY_CHARS",
        400 if is_paper else 280,
    )
    min_body = _settings_int("ARTICLE_MIN_BODY_CHARS", 200)

    if body_len >= min_body:
        # 本文が十分ある場合: 500字以上あれば summary なしでも通す（Claude reason方式対応）
        if body_len >= 500:
            return total >= min_total
        return total >= min_total and summary_len >= 80

    if summary_len >= min_summary and total >= min_total:
        return True

    logger.info(
        "素材不足: total=%d summary=%d body=%d raw_body=%d "
        "(need total>=%d summary>=%d or complete body>=%d)",
        total,
        summary_len,
        body_len,
        raw_body_len,
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


def _blocks_readable_content(blocks: list[dict[str, Any]] | None) -> str:
    """text/explain の本文を結合（プレースホルダー除外）。"""
    parts: list[str] = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") not in ("text", "explain"):
            continue
        content = str(b.get("content") or "").strip()
        if content and not content.startswith("（"):
            parts.append(content)
    return "\n".join(parts)


def min_generated_ja_ratio() -> float:
    try:
        from app.config import settings

        return max(0.1, min(0.9, float(getattr(settings, "ARTICLE_MIN_JA_RATIO", 0.35) or 0.35)))
    except Exception:
        return 0.35


def blocks_mainly_japanese(blocks: list[dict[str, Any]] | None) -> bool:
    """生成 blocks の text+explain が主に日本語か。"""
    combined = _blocks_readable_content(blocks)
    if len(combined) < 20:
        return True
    from app.services.translate_service import text_mainly_japanese

    return text_mainly_japanese(combined, min_ratio=min_generated_ja_ratio())


def is_generated_blocks_quantity_sufficient(blocks: list[dict[str, Any]] | None) -> bool:
    """ミドルマン記事（text+explain）が字数・explain 数の基準を満たすか。"""
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

    min_text = min_generated_text_chars()
    min_explains = _settings_int("ARTICLE_MIN_EXPLAIN_COUNT", 3)

    if text_chars < min_text:
        logger.info("生成記事が短すぎる: text=%d字 (最低 %d字)", text_chars, min_text)
        return False
    if explain_count < min_explains:
        logger.info("explain 不足: %d個 (最低 %d個)", explain_count, min_explains)
        return False
    return True


def is_generated_article_sufficient(blocks: list[dict[str, Any]] | None) -> bool:
    """ミドルマン記事（text+explain）がサイト掲載に足りる分量か。"""
    if not is_generated_blocks_quantity_sufficient(blocks):
        return False
    if not blocks_mainly_japanese(blocks):
        logger.info("生成記事が日本語比率不足 (最低 %.0f%%)", min_generated_ja_ratio() * 100)
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
