"""AI日次コンテンツ生成 - 朝9時に1日1回更新。昨日のメモ・人格コメント"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_daily_cache: Optional[dict] = None
_daily_date: Optional[str] = None

_LOCAL_STATE = Path(__file__).resolve().parent.parent.parent / "data" / "ai_daily.json"


def _use_neon():
    try:
        from .neon_store import use_neon
        return use_neon()
    except Exception:
        return False


def _load_from_store() -> Optional[dict]:
    if _use_neon():
        try:
            from .neon_store import neon_ai_daily_get

            return neon_ai_daily_get()
        except Exception as e:
            logger.warning("ai_daily: Neon からの読み込みに失敗: %s", e)
            return None
    try:
        if _LOCAL_STATE.exists():
            return json.loads(_LOCAL_STATE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("ai_daily: ローカルファイル読み込み失敗: %s", e)
    return None


def _save_to_store(data: dict) -> None:
    if _use_neon():
        try:
            from .neon_store import neon_ai_daily_save

            neon_ai_daily_save(data)
        except Exception as e:
            logger.warning("ai_daily: Neon への保存に失敗: %s", e)
        return
    try:
        _LOCAL_STATE.parent.mkdir(parents=True, exist_ok=True)
        _LOCAL_STATE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("ai_daily: ローカルファイル保存失敗: %s", e)


def get_daily_ai_content() -> Optional[dict]:
    """キャッシュ済みの日次AIコンテンツを返す"""
    global _daily_cache, _daily_date
    today = datetime.now().strftime("%Y-%m-%d")

    if _daily_cache and _daily_date == today:
        return _daily_cache

    stored = _load_from_store()
    if stored and stored.get("date") == today:
        _daily_cache = stored
        _daily_date = today
        return stored

    return _daily_cache


def generate_daily_ai_content():
    """日次AIコンテンツを生成（スケジューラから呼ぶ）"""
    global _daily_cache, _daily_date
    from app.config import settings

    if not settings.OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set, skipping daily AI content generation")
        return

    from app.services.news_aggregator import NewsAggregator
    from app.services.ai_service import PERSONAS
    from app.utils.openai_compat import create_with_retry
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    model = settings.OPENAI_MODEL

    all_news = NewsAggregator.get_news()
    yesterday = datetime.now() - timedelta(days=1)
    recent = [a for a in all_news if a.published and a.published > yesterday]
    if not recent:
        recent = all_news[:10]

    titles = "\n".join([f"- {a.title}" for a in recent[:15]])
    today = datetime.now().strftime("%Y-%m-%d")

    memo = ""
    try:
        resp = create_with_retry(
            client, 300, model=model,
            messages=[
                {"role": "system", "content": "あなたはニュースアナリストです。昨日のニュースを踏まえ、今日どうなるかを100字以内の一言メモにしてください。日本語で。"},
                {"role": "user", "content": f"昨日の主要ニュース:\n{titles}"},
            ],
            temperature=0.5,
        )
        memo = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("Failed to generate daily memo: %s", e)

    import random
    selected_personas = random.sample(PERSONAS, min(2, len(PERSONAS)))
    persona_comments = []
    for p in selected_personas:
        try:
            resp = create_with_retry(
                client, 300, model=model,
                messages=[
                    {"role": "system", "content": f"あなたは「{p['name']}」です。{p['role']}\n\n今日のニューストレンドを見て、80字以内で一言コメントしてください。"},
                    {"role": "user", "content": f"最近のニュース:\n{titles}"},
                ],
                temperature=0.6,
            )
            comment = (resp.choices[0].message.content or "").strip()
            persona_comments.append({"name": p["name"], "emoji": p["emoji"], "comment": comment})
        except Exception as e:
            logger.warning("Failed to generate persona comment for %s: %s", p["name"], e)

    data = {
        "date": today,
        "memo": memo,
        "persona_comments": persona_comments,
    }

    _daily_cache = data
    _daily_date = today

    _save_to_store(data)

    logger.info("Generated daily AI content for %s", today)
    return data
