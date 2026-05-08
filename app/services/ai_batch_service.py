"""AI解説・秒速理解・投票を一括生成しキャッシュする。
理解は1回（理解ナビゲーター）だけ行い、その結果を記事・秒速理解・投票に流用。
人格は「論理2＋エンタメ1」のランダム3人を選んでから、その3人分だけAPI呼び出し。
ミドルマン記事本文・ペルソナコメントは Claude CLI（サブスク使用量） → OpenAI の順で生成する。"""
import json
import logging
import random
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
from app.config import settings

from app.services.ai_service import (
    explain_article_as_navigator,
    expand_navigator_to_article,
    get_persona_opinion,
    generate_vote_question,
    generate_paper_knowledge_graph,
    generate_paper_quiz,
    generate_deep_insights,
    PERSONA_LOGIC_IDS,
    PERSONA_ENT_IDS,
)
from app.services.explanation_cache import get_cached, save_cache

_MIDDLEMAN_YAML = Path(__file__).resolve().parent.parent / "prompts" / "middleman.yaml"


def _load_middleman_prompt_config() -> dict:
    defaults = {
        "language": "日本語",
        "style": {
            "narration_tone": "友達に話す喋り言葉（です・ます調）",
            "allow_speculation_format": "推測は『〜とみられてます』などで控えめに",
            "forbidden_styles": ["新聞調", "体言止め", "堅すぎる書き言葉"],
        },
        "length": {
            "reading_time_minutes": 3,
            "article_chars_min": 1200,
            "article_chars_max": 2500,
        },
        "blocks": {
            "types": ["text", "explain"],
            "explain_min": 3,
            "explain_max": 6,
            "explain_sentence_range": "1〜3文",
        },
        "comment_focus": [
            "記事の事実を崩さず、背景や難語を噛み砕いて補足する",
            "難しいポイントの直後に explain を入れる",
            "過剰な煽りを避け、読者理解を優先する",
        ],
    }
    try:
        import yaml

        if not _MIDDLEMAN_YAML.exists():
            return defaults
        with open(_MIDDLEMAN_YAML, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        if not isinstance(loaded, dict):
            return defaults
        merged = dict(defaults)
        for k, v in loaded.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                d = dict(merged[k])
                d.update(v)
                merged[k] = d
            else:
                merged[k] = v
        return merged
    except Exception:
        return defaults


def _build_middleman_claude_prompt(title: str, content: str) -> str:
    cfg = _load_middleman_prompt_config()
    style = cfg.get("style", {}) if isinstance(cfg.get("style"), dict) else {}
    length = cfg.get("length", {}) if isinstance(cfg.get("length"), dict) else {}
    blocks = cfg.get("blocks", {}) if isinstance(cfg.get("blocks"), dict) else {}
    focus = cfg.get("comment_focus", [])
    if not isinstance(focus, list):
        focus = []
    focus_text = "\n".join(f"- {str(x)}" for x in focus if str(x).strip())

    from app.services.ai_service import MIDDLEMAN_ROLE

    return f"""{MIDDLEMAN_ROLE}

【タイトル】{title}
【内容】
{content[:8000]}

上記の内容を読んで、ミドルマンとして記事本文（text）と解説（explain）のブロックを書いてください。

出力ルール:
- JSON配列のみ出力（説明文不要）
- 形式: [{{"type": "text", "content": "..."}}, {{"type": "explain", "content": "..."}} ...]
- 言語: {cfg.get("language", "日本語")}
- 文体: {style.get("narration_tone", "友達に話す喋り言葉")}
- 推測表現: {style.get("allow_speculation_format", "控えめに示す")}
- explain（吹き出し解説）は{blocks.get("explain_min", 3)}〜{blocks.get("explain_max", 6)}個
- explain は{blocks.get("explain_sentence_range", "1〜3文")}で簡潔に
- 全体で約{length.get("reading_time_minutes", 3)}分で読める分量
- 本文目安: {length.get("article_chars_min", 1200)}〜{length.get("article_chars_max", 2500)}文字

補足方針:
{focus_text if focus_text else "- 記事理解を最優先し、難所を補足する"}"""


# ── Claude CLI を使ったミドルマン・ペルソナ生成 ──────────────────────────────

def _generate_blocks_via_claude(navigator_blocks: list, title: str) -> list[dict]:
    """Claude CLI でミドルマン記事本文（blocks 配列）を生成。失敗時は空リスト → OpenAI フォールバック。"""
    try:
        from app.services.claude_researcher import run_claude_text_gen, is_claude_available
        if not is_claude_available():
            return []
    except Exception:
        return []

    parts = [b["content"].strip() for b in (navigator_blocks or []) if isinstance(b, dict) and b.get("content")]
    content = "\n\n".join(parts)
    if not content.strip():
        return []
    prompt = _build_middleman_claude_prompt(title, content)

    raw = run_claude_text_gen(prompt, timeout=180, usage_kind="middleman_blocks")
    if not raw:
        return []
    try:
        if raw.startswith("```"):
            raw = "\n".join(ln for ln in raw.splitlines() if not ln.startswith("```")).strip()
        m = re.search(r'\[[\s\S]*\]', raw)
        if m:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                valid = [b for b in data if isinstance(b, dict) and b.get("type") in ("text", "explain") and b.get("content")]
                if valid:
                    logger.info("Claude ミドルマン生成成功: %d ブロック", len(valid))
                    return valid
    except Exception as e:
        logger.warning("Claude ミドルマン JSON パースエラー: %s / raw: %s", e, raw[:200])
    return []


def _generate_personas_via_claude(title: str, summary_text: str, display_persona_ids: list[int]) -> list[str]:
    """Claude CLI で3人の偉人コメントを一括生成。失敗時は空リスト → OpenAI フォールバック。"""
    try:
        from app.services.claude_researcher import run_claude_text_gen, is_claude_available
        if not is_claude_available():
            return []
    except Exception:
        return []

    from app.services.ai_service import PERSONAS, PERSONA_COMMENT_MAX_LEN

    selected = [PERSONAS[pid] for pid in display_persona_ids if 0 <= pid < len(PERSONAS)]
    if not selected:
        return []

    personas_desc = "\n".join(
        f"{i + 1}. {p['name']}（{p['emoji']}）\n   人物設定: {p['role']}"
        for i, p in enumerate(selected)
    )

    prompt = f"""以下の3人の歴史上の偉人が、それぞれの思想・価値観でニュース記事にコメントします。

【記事タイトル】
{title}

【記事内容】
{summary_text[:1500]}

【3人の設定】
{personas_desc}

【コメントルール】
- 各人物が{PERSONA_COMMENT_MAX_LEN}文字以内でコメントする
- 丁寧語不要。その人物の哲学・価値観で主観100%で語る
- ニュース内容の要約・説明から入らない（最初の一文から意見・感想・哲学を述べる）
- 句点「。」で終わる
- 3人それぞれが全く違う切り口で語る
- 他の人物と同じ表現・結論は使わない

JSON配列形式で出力してください:
[
  {{"name": "{selected[0]['name']}", "comment": "..."}},
  {{"name": "{selected[1]['name']}", "comment": "..."}},
  {{"name": "{selected[2]['name']}", "comment": "..."}}
]"""

    raw = run_claude_text_gen(prompt, timeout=120, usage_kind="persona_comments")
    if not raw:
        return []
    try:
        if raw.startswith("```"):
            raw = "\n".join(ln for ln in raw.splitlines() if not ln.startswith("```")).strip()
        m = re.search(r'\[[\s\S]*\]', raw)
        if m:
            data = json.loads(m.group(0))
            if isinstance(data, list) and len(data) >= len(selected):
                comments = [str(item.get("comment", ""))[:PERSONA_COMMENT_MAX_LEN] for item in data[:len(selected)]]
                if all(c.strip() for c in comments):
                    logger.info("Claude ペルソナ生成成功: %d 人", len(comments))
                    return comments
    except Exception as e:
        logger.warning("Claude ペルソナ JSON パースエラー: %s / raw: %s", e, raw[:200])
    return []


# ─────────────────────────────────────────────────────────────────────────────

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


def generate_all_explanations(article_id: str, title: str, content: str, category: str | None = None) -> dict:
    """
    理解を1回（理解ナビゲーター）だけ行い、その結果を記事・秒速理解・投票に流用。
    人格は先にランダムで3人（論理2＋エンタメ1）を選び、その3人分だけAPI呼び出し。
    API呼び出し: 1（ナビ）+ 1（記事展開）+ 3（人格）+ 1（投票）= 6回/記事。
    """
    cached = get_cached(article_id)
    if cached:
        return cached

    # 1) 理解は1回だけ：理解ナビゲーター（事実・背景・影響・予測・注意）
    is_paper = category == "研究・論文"
    navigator_blocks = explain_article_as_navigator(title, content, is_paper=is_paper)
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

    # 3) 記事展開・投票・深掘り（論文時は知識グラフ/クイズ）を並列生成
    #    ミドルマンとペルソナは Claude CLI（サブスク） → OpenAI フォールバック

    def do_blocks():
        provider = (getattr(settings, "MIDDLEMAN_PROVIDER", "claude_first") or "claude_first").strip().lower()
        # openai 指定時は Claude を使わず OpenAI へ直行
        if provider != "openai":
            claude_blocks = _generate_blocks_via_claude(navigator_blocks, title)
            if claude_blocks:
                return claude_blocks
        model = (getattr(settings, "MIDDLEMAN_OPENAI_MODEL", "") or "").strip() or "gpt-4o"
        logger.info("ミドルマン記事生成: provider=%s model=%s", provider, model)
        return expand_navigator_to_article(navigator_blocks, title, model=model)

    def do_vote():
        return generate_vote_question(title, summary_text)

    def do_deep():
        return generate_deep_insights(title, summary_text)

    def do_paper_graph():
        return generate_paper_knowledge_graph(title, summary_text)

    def do_paper_quiz():
        return generate_paper_quiz(title, summary_text)

    blocks = []
    personas_3 = [""] * 3
    vote_data = {}
    paper_graph = {}
    paper_quiz = {}
    deep_insights = {}

    with ThreadPoolExecutor(max_workers=16) as ex:
        fut_blocks = ex.submit(do_blocks)
        fut_vote = ex.submit(do_vote)
        fut_deep = ex.submit(do_deep)
        fut_paper_graph = ex.submit(do_paper_graph) if is_paper else None
        fut_paper_quiz = ex.submit(do_paper_quiz) if is_paper else None

        blocks = fut_blocks.result()

        # ペルソナ: Claude CLI で3人一括生成 → 失敗時は OpenAI で1人ずつ
        claude_comments = _generate_personas_via_claude(title, summary_text, display_persona_ids)
        if claude_comments and len(claude_comments) == 3:
            personas_3 = claude_comments
        else:
            # OpenAI フォールバック: 順に生成し先のコメントを渡して重複を避ける
            generated_comments: list[str] = []
            for slot_idx, pid in enumerate(display_persona_ids):
                try:
                    comment = (
                        get_persona_opinion(
                            title,
                            summary_text,
                            pid,
                            other_comments=generated_comments if generated_comments else None,
                        )
                        or ""
                    )
                except Exception:
                    comment = ""
                personas_3[slot_idx] = comment
                if comment and "取得失敗" not in comment:
                    generated_comments.append(comment)

        try:
            vote_data = fut_vote.result() or {}
        except Exception:
            pass
        try:
            deep_insights = fut_deep.result() or {}
        except Exception:
            pass
        if fut_paper_graph is not None:
            try:
                paper_graph = fut_paper_graph.result() or {}
            except Exception:
                pass
        if fut_paper_quiz is not None:
            try:
                paper_quiz = fut_paper_quiz.result() or {}
            except Exception:
                pass

    result = {
        "blocks": blocks,
        "personas": personas_3,
        "display_persona_ids": display_persona_ids,
        "quick_understand": quick_understand,
        "vote_data": vote_data,
        "paper_graph": paper_graph,
        "paper_quiz": paper_quiz,
        "deep_insights": deep_insights,
    }
    save_cache(
        article_id, blocks, personas_3,
        display_persona_ids=display_persona_ids,
        quick_understand=quick_understand,
        vote_data=vote_data,
        paper_graph=paper_graph,
        paper_quiz=paper_quiz,
        deep_insights=deep_insights,
    )
    return result
