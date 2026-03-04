"""AI解説・秒速理解・投票を一括生成しキャッシュする。
理解は1回（理解ナビゲーター）だけ行い、その結果を記事・秒速理解・投票に流用。
人格は「論理2＋エンタメ1」のランダム3人を選んでから、その3人分だけAPI呼び出し。"""
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.services.ai_service import (
    explain_article_as_navigator,
    expand_navigator_to_article,
    get_persona_opinion,
    generate_vote_question,
    PERSONA_LOGIC_IDS,
    PERSONA_ENT_IDS,
)
from app.services.explanation_cache import get_cached, save_cache


def _navigator_summary(navigator_blocks: list) -> str:
    """理解ナビゲーターのブロックを1本の要約テキストに結合"""
    parts = []
    for b in navigator_blocks or []:
        if isinstance(b, dict) and b.get("content"):
            parts.append(b["content"].strip())
    return "\n\n".join(parts) if parts else ""


def _quick_understand_from_navigator(navigator_blocks: list) -> dict:
    """理解ナビゲーターの5項目から秒速理解（what/why/how）を組み立て。API呼び出しなし。"""
    def section(key: str) -> str:
        for b in navigator_blocks or []:
            if isinstance(b, dict) and b.get("section") == key and b.get("content"):
                return (b["content"] or "").strip()
        return ""
    return {
        "what": section("facts"),
        "why": section("background"),
        "how": section("prediction"),
    }


def generate_all_explanations(article_id: str, title: str, content: str) -> dict:
    """
    理解を1回（理解ナビゲーター）だけ行い、その結果を記事・秒速理解・投票に流用。
    人格は先にランダムで3人（論理2＋エンタメ1）を選び、その3人分だけAPI呼び出し。
    API呼び出し: 1（ナビ）+ 1（記事展開）+ 3（人格）+ 1（投票）= 6回/記事。
    """
    cached = get_cached(article_id)
    if cached:
        return cached

    # 1) 理解は1回だけ：理解ナビゲーター（事実・背景・影響・予測・注意）
    navigator_blocks = explain_article_as_navigator(title, content)
    summary_text = _navigator_summary(navigator_blocks)
    quick_understand = _quick_understand_from_navigator(navigator_blocks)

    # 2) 先に表示する3人を選ぶ（論理2 + エンタメ1）
    logic_ids = list(PERSONA_LOGIC_IDS)
    ent_ids = list(PERSONA_ENT_IDS)
    if len(logic_ids) >= 2 and len(ent_ids) >= 1:
        display_persona_ids = random.sample(logic_ids, 2) + random.sample(ent_ids, 1)
        random.shuffle(display_persona_ids)
    else:
        from app.services.ai_service import PERSONAS
        display_persona_ids = list(range(min(3, len(PERSONAS))))

    # 3) 記事展開・3人格・投票を並列で生成
    def do_blocks():
        return expand_navigator_to_article(navigator_blocks, title)

    def do_persona(pid: int):
        return get_persona_opinion(title, summary_text, pid)

    def do_vote():
        return generate_vote_question(title, summary_text)

    blocks = []
    personas_3 = [""] * 3
    vote_data = {}

    with ThreadPoolExecutor(max_workers=16) as ex:
        fut_blocks = ex.submit(do_blocks)
        fut_personas = {ex.submit(do_persona, pid): i for i, pid in enumerate(display_persona_ids)}
        fut_vote = ex.submit(do_vote)

        blocks = fut_blocks.result()
        for f in as_completed(fut_personas):
            idx = fut_personas[f]
            try:
                personas_3[idx] = f.result() or ""
            except Exception:
                pass
        try:
            vote_data = fut_vote.result() or {}
        except Exception:
            pass

    result = {
        "blocks": blocks,
        "personas": personas_3,
        "display_persona_ids": display_persona_ids,
        "quick_understand": quick_understand,
        "vote_data": vote_data,
    }
    save_cache(
        article_id, blocks, personas_3,
        display_persona_ids=display_persona_ids,
        quick_understand=quick_understand,
        vote_data=vote_data,
    )
    return result
