"""AI解説・5人格・秒速理解・投票を一括生成しキャッシュする"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app.services.ai_service import (
    explain_article_long_with_bubbles,
    get_persona_opinion,
    generate_quick_understand,
    generate_vote_question,
    PERSONAS,
)
from app.services.explanation_cache import get_cached, save_cache


def generate_all_explanations(article_id: str, title: str, content: str) -> dict:
    """ミドルマン解説＋5人格＋秒速理解＋投票を一括生成。キャッシュがあれば返却、なければ並列生成して保存"""
    cached = get_cached(article_id)
    if cached:
        return cached

    def do_inline():
        return explain_article_long_with_bubbles(title, content)

    def do_persona(i: int):
        return get_persona_opinion(title, content, i)

    def do_quick():
        return generate_quick_understand(title, content)

    def do_vote():
        return generate_vote_question(title, content)

    blocks = []
    personas = [""] * 5
    quick_understand = {}
    vote_data = {}

    with ThreadPoolExecutor(max_workers=8) as ex:
        fut_inline = ex.submit(do_inline)
        fut_personas = {ex.submit(do_persona, i): i for i in range(5)}
        fut_quick = ex.submit(do_quick)
        fut_vote = ex.submit(do_vote)

        blocks = fut_inline.result()
        for f in as_completed(fut_personas):
            idx = fut_personas[f]
            try:
                personas[idx] = f.result()
            except Exception:
                pass
        try:
            quick_understand = fut_quick.result()
        except Exception:
            pass
        try:
            vote_data = fut_vote.result()
        except Exception:
            pass

    result = {
        "blocks": blocks,
        "personas": personas,
        "quick_understand": quick_understand,
        "vote_data": vote_data,
    }
    save_cache(article_id, blocks, personas, quick_understand=quick_understand, vote_data=vote_data)
    return result
