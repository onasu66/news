"""Notion 相談データベース連携"""
import logging
import os
import random
from datetime import date

import httpx

logger = logging.getLogger(__name__)

NOTION_DB_ID = "96659908719b43a1a6cb499ff1bddaa5"

_PERSONA_MAP = {
    "🪷 ブッダ":         (0,  "ブッダ",         "🪷"),
    "🔥 織田信長":       (1,  "織田信長",       "🔥"),
    "📖 吉田松陰":       (2,  "吉田松陰",       "📖"),
    "⚓ 坂本龍馬":       (3,  "坂本龍馬",       "⚓"),
    "🥀 太宰治":         (4,  "太宰治",         "🥀"),
    "🌊 葛飾北斎":       (5,  "葛飾北斎",       "🌊"),
    "🏛️ ソクラテス":    (6,  "ソクラテス",     "🏛️"),
    "🔬 野口英世":       (7,  "野口英世",       "🔬"),
    "🖌️ ダヴィンチ":    (8,  "ダヴィンチ",     "🖌️"),
    "💡 エジソン":       (9,  "エジソン",       "💡"),
    "⚛️ アインシュタイン": (10, "アインシュタイン", "⚛️"),
    "🕯️ ナイチンゲール": (11, "ナイチンゲール",  "🕯️"),
    "🔭 ガリレオ":       (12, "ガリレオ",       "🔭"),
    "⚡ ニーチェ":       (13, "ニーチェ",       "⚡"),
}

_SOURCE_MAP = {
    "LINE":         "line",
    "X（旧Twitter）": "x",
    "その他":        "other",
}


def _is_configured() -> bool:
    return bool(os.environ.get("NOTION_API_KEY", "").strip())


def _headers() -> dict:
    token = os.environ.get("NOTION_API_KEY", "").strip()
    if not token:
        raise RuntimeError("NOTION_API_KEY が設定されていません")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }


def fetch_pending_consultations() -> list[dict]:
    """未掲載（掲載済み=OFF）の相談一覧を Notion から取得する。"""
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    payload = {
        "filter": {
            "property": "掲載済み",
            "checkbox": {"equals": False},
        }
    }
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, headers=_headers(), json=payload)
        resp.raise_for_status()

    results = []
    for page in resp.json().get("results", []):
        props = page["properties"]
        title_parts = props.get("相談文", {}).get("title", [])
        question = "".join(t.get("plain_text", "") for t in title_parts).strip()
        if not question:
            continue
        persona_label = (props.get("偉人", {}).get("select") or {}).get("name", "")
        source_label  = (props.get("出典", {}).get("select") or {}).get("name", "")
        source_user   = "".join(
            t.get("plain_text", "") for t in props.get("投稿者", {}).get("rich_text", [])
        ).strip() or None
        results.append({
            "page_id":      page["id"],
            "question":     question,
            "persona_label": persona_label,
            "source_label": source_label,
            "source_user":  source_user,
        })
    return results


def _mark_as_published(page_id: str, answer: str) -> None:
    """Notion ページの「掲載済み」をチェックし、掲載日と生成回答を記録する。"""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {
        "properties": {
            "掲載済み": {"checkbox": True},
            "掲載日":   {"date": {"start": date.today().isoformat()}},
            "生成回答": {"rich_text": [{"text": {"content": answer[:2000]}}]},
        }
    }
    with httpx.Client(timeout=15.0) as client:
        resp = client.patch(url, headers=_headers(), json=payload)
        resp.raise_for_status()


def process_notion_consultation() -> bool:
    """
    Notion から未掲載の相談をランダムに1件選び、AI回答を生成・公開する。
    NOTION_TOKEN 未設定時はサイレントスキップ。成功時 True。
    """
    if not _is_configured():
        return False
    try:
        pending = fetch_pending_consultations()
        if not pending:
            logger.info("Notion 相談: 未掲載の相談なし、スキップ")
            return False

        entry = random.choice(pending)
        persona_info = _PERSONA_MAP.get(entry["persona_label"])
        if not persona_info:
            logger.warning("Notion 相談: 未知の偉人名 '%s'、スキップ", entry["persona_label"])
            return False

        pid, pname, pemoji = persona_info
        source = _SOURCE_MAP.get(entry["source_label"], "other")

        from app.services.consultation_service import generate_consultation_answer
        answer = generate_consultation_answer(pid, entry["question"])

        from app.services.consultation_store import save_consultation
        save_consultation(
            question=entry["question"],
            persona_id=pid,
            persona_name=pname,
            persona_emoji=pemoji,
            answer=answer,
            source=source,
            source_user=entry.get("source_user"),
        )

        _mark_as_published(entry["page_id"], answer)
        logger.info("Notion 相談: 掲載完了 [%s → %s%s]", entry["question"][:30], pemoji, pname)
        try:
            from app.routers.consultation import invalidate_consultation_cache
            invalidate_consultation_cache()
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning("Notion 相談処理でエラー: %s", e)
        return False
