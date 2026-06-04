"""AI解説・秒速理解を一括生成しキャッシュする。
理解は1回（理解ナビゲーター）だけ行い、その結果を記事・秒速理解に流用。
人格は全ペルソナからランダム3人を選び、その3人分だけ OpenAI で生成する。
ミドルマン記事本文は MIDDLEMAN_PROVIDER に従い Claude CLI → OpenAI の順で生成する。
（投票クイズ・論文ナレッジグラフ・論文クイズ・深掘り「AIに聞く」は生成しない。）"""
import json
import logging
import random
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
from app.config import settings


def _wait_between_gemini_personas(slot_idx: int) -> None:
    """Gemini ペルソナ連続呼び出しの RPM 対策（2人目以降で待機）。"""
    if slot_idx <= 0:
        return
    try:
        from app.utils.llm_client import persona_provider, use_gemini

        if not use_gemini(persona_provider()):
            return
        sec = max(0, int(getattr(settings, "GEMINI_PERSONA_INTERVAL_SEC", 8) or 0))
        if sec > 0:
            import time

            time.sleep(sec)
    except Exception:
        pass


def upgrade_personas_with_claude_if_configured(
    title: str,
    navigator_summary: str,
    display_persona_ids: list[int],
    current_personas: list[str],
) -> list[str]:
    """ペルソナの Claude 上書きは行わない。常に current_personas を返す。"""
    _ = (title, navigator_summary, display_persona_ids)
    return current_personas

from app.services.ai_service import (
    PERSONAS,
    explain_article_as_navigator,
    explain_article_long_with_bubbles,
    expand_navigator_to_article,
    get_persona_opinion,
)
from app.services.explanation_cache import get_cached, save_cache
from app.services.article_content_quality import is_generated_article_sufficient, is_navigator_sufficient

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


# ── Claude CLI を使ったミドルマン生成 ─────────────────────────────────────────


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


def generate_all_explanations(
    article_id: str,
    title: str,
    content: str,
    category: str | None = None,
    *,
    persist_cache: bool = True,
) -> dict:
    """
    理解を1回（理解ナビゲーター）だけ行い、その結果を記事・秒速理解に流用。
    人格は全員からランダム3人を選び、その3人分だけ API 呼び出し。

    persist_cache=False（RSS/手動記事パイプライン向け）:
      解説をDBに書かず返すのみ。
    """
    cached = get_cached(article_id)
    if cached:
        return cached

    # 1) 理解は1回だけ：理解ナビゲーター（事実・背景・影響・予測・注意）
    is_paper = category == "研究・論文"
    navigator_blocks = explain_article_as_navigator(title, content, is_paper=is_paper)
    if not is_navigator_sufficient(navigator_blocks):
        logger.warning("理解ナビゲーターが薄すぎるため記事化中止: %s", (title or "")[:60])
        return {
            "blocks": [],
            "personas": ["", "", ""],
            "display_persona_ids": [],
            "quick_understand": {},
            "vote_data": {},
            "paper_graph": {},
            "paper_quiz": {},
            "deep_insights": {},
        }
    summary_text = _navigator_summary(navigator_blocks)
    quick_understand = _quick_understand_from_navigator(navigator_blocks)
    persona_source = (content or summary_text or "")[:3500]

    # 2) 先に表示する3人を選ぶ（全ペルソナからランダム）
    n_personas = len(PERSONAS)
    display_persona_ids = random.sample(range(n_personas), min(3, n_personas)) if n_personas else []

    # 3) 記事展開（投票・論文グラフ/クイズ・深掘り「AIに聞く」は生成しない）
    #    ミドルマンは MIDDLEMAN_PROVIDER に従い Claude → OpenAI。ペルソナは OpenAI のみ。

    def do_blocks():
        provider = (getattr(settings, "MIDDLEMAN_PROVIDER", "claude_first") or "claude_first").strip().lower()
        # openai 指定時は Claude を使わず OpenAI へ直行
        if provider != "openai":
            claude_blocks = _generate_blocks_via_claude(navigator_blocks, title)
            if claude_blocks:
                return claude_blocks
        model = (getattr(settings, "MIDDLEMAN_OPENAI_MODEL", "") or "").strip() or settings.OPENAI_MODEL
        logger.info("ミドルマン記事生成: provider=%s model=%s", provider, model)
        blocks = expand_navigator_to_article(navigator_blocks, title, model=model, source_content=content)
        if blocks and is_generated_article_sufficient(blocks):
            return blocks
        logger.info("expand_navigator 不十分のため explain_article_long_with_bubbles へフォールバック")
        return explain_article_long_with_bubbles(title, content, model=model)

    blocks = []
    personas_3 = [""] * 3
    vote_data: dict = {}
    paper_graph: dict = {}
    paper_quiz: dict = {}
    deep_insights: dict = {}

    with ThreadPoolExecutor(max_workers=4) as ex:
        fut_blocks = ex.submit(do_blocks)
        blocks = fut_blocks.result()

        # ペルソナ: PERSONA_PROVIDER に従う（順に生成し先のコメントを渡して重複を避ける）
        generated_comments: list[str] = []
        for slot_idx, pid in enumerate(display_persona_ids):
            _wait_between_gemini_personas(slot_idx)
            try:
                comment = (
                    get_persona_opinion(
                        title,
                        persona_source,
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
    if not persist_cache:
        result["navigator_summary"] = summary_text
    if persist_cache:
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
