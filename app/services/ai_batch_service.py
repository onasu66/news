"""AI解説・5人格を一括生成しキャッシュする"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app.services.ai_service import (
    explain_article_long_with_bubbles,
    get_persona_opinion,
    PERSONAS,
)
from app.services.explanation_cache import get_cached, save_cache


def generate_all_explanations(article_id: str, title: str, content: str) -> dict:
    """ミドルマン解説＋5人格の意見を一括生成。キャッシュがあれば返却、なければ並列生成して保存"""
    cached = get_cached(article_id)
    if cached:
        return cached

    def do_inline():
        return explain_article_long_with_bubbles(title, content)

    def do_persona(i: int):
        return get_persona_opinion(title, content, i)

    blocks = []
    personas = [""] * 5

    with ThreadPoolExecutor(max_workers=6) as ex:
        fut_inline = ex.submit(do_inline)
        fut_personas = {ex.submit(do_persona, i): i for i in range(5)}

        blocks = fut_inline.result()
        for f in as_completed(fut_personas):
            idx = fut_personas[f]
            try:
                personas[idx] = f.result()
            except Exception:
                pass

    save_cache(article_id, blocks, personas)
    return {"blocks": blocks, "personas": personas}
