"""AI解説・秒速理解を一括生成しキャッシュする。
理解は1回（理解ナビゲーター）だけ行い、その結果を記事・秒速理解に流用。
人格は全ペルソナからランダム3人を選び、その3人分だけ OpenAI で生成する。
ミドルマン記事本文は MIDDLEMAN_PROVIDER に従い Claude CLI → OpenAI の順で生成する。
（投票クイズ・論文ナレッジグラフ・論文クイズ・深掘り「AIに聞く」は生成しない。）"""
import json
import logging
import random
import re
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
from app.config import settings

# 同一 article_id への同時アクセスでフル生成が二重に走らないようにする per-article ロック。
# キャッシュ済みなら誰もロックに触れないため、通常運用でのオーバーヘッドはほぼゼロ。
_article_gen_locks: dict[str, threading.Lock] = {}
_article_gen_locks_guard = threading.Lock()


def _acquire_article_gen_lock(article_id: str) -> threading.Lock:
    with _article_gen_locks_guard:
        lock = _article_gen_locks.setdefault(article_id, threading.Lock())
    lock.acquire()
    return lock


def _release_article_gen_lock(article_id: str, lock: threading.Lock) -> None:
    lock.release()
    with _article_gen_locks_guard:
        # 待機者がいなければレジストリから外し、辞書の肥大化を防ぐ
        if _article_gen_locks.get(article_id) is lock and not lock.locked():
            _article_gen_locks.pop(article_id, None)


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
    get_all_persona_opinions_batch,
    generate_editorial_take_fallback,
    build_persona_batch_prompt,
    parse_persona_batch_payload,
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
            "article_chars_min": 900,
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
- 本文目安: {length.get("article_chars_min", 900)}〜{length.get("article_chars_max", 2500)}文字

補足方針:
{focus_text if focus_text else "- 記事理解を最優先し、難所を補足する"}"""


# ── Claude CLI を使ったミドルマン生成 ─────────────────────────────────────────


def _generate_blocks_via_claude(navigator_blocks: list, title: str) -> list[dict]:
    """Claude CLI でミドルマン記事本文（blocks 配列）を生成。失敗時は空リスト → OpenAI フォールバック。"""
    try:
        from app.services.claude_researcher import run_claude_text_gen, is_claude_available
        if not is_claude_available():
            return {"personas": [], "editorial_take": ""}
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


# ── Claude CLI を使ったペルソナ3人分バッチ生成 ─────────────────────────────────

def _generate_personas_via_claude(
    title: str,
    content: str,
    persona_ids: list[int],
    category: str | None = None,
) -> dict:
    """Claude CLI で3人分のペルソナコメントを1回の呼び出しでまとめて生成する。
    失敗・CLI未導入時は空リスト → 呼び出し元が Gemini/OpenAI バッチにフォールバックする。"""
    try:
        from app.services.claude_researcher import run_claude_text_gen, is_claude_available
        if not is_claude_available():
            return []
    except Exception:
        return {"personas": [], "editorial_take": ""}

    built = build_persona_batch_prompt(title, content, persona_ids, category=category)
    if not built:
        return {"personas": [], "editorial_take": ""}
    system_prompt, user_prompt, personas_data = built
    prompt = f"{system_prompt}\n\n{user_prompt}"

    raw = run_claude_text_gen(prompt, timeout=180, usage_kind="persona_batch")
    if not raw:
        return {"personas": [], "editorial_take": ""}

    payload = parse_persona_batch_payload(raw, personas_data)
    results = payload.get("personas") if isinstance(payload.get("personas"), list) else []
    if any(results):
        logger.info("Claude ペルソナ生成成功: %d/%d 件", sum(1 for r in results if r), len(results))
    return {"personas": results, "editorial_take": str(payload.get("editorial_take") or "")}


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

    # 同一記事への同時リクエストでフル生成が二重に走らないようロック。
    # 待機中に他スレッドが生成・保存を終えている場合があるため、取得後にキャッシュを再チェックする。
    lock = _acquire_article_gen_lock(article_id)
    try:
        cached = get_cached(article_id)
        if cached:
            return cached
        return _generate_all_explanations_locked(
            article_id, title, content, category, persist_cache=persist_cache
        )
    finally:
        _release_article_gen_lock(article_id, lock)


def _generate_all_explanations_locked(
    article_id: str,
    title: str,
    content: str,
    category: str | None,
    *,
    persist_cache: bool,
) -> dict:
    """generate_all_explanations の本体。呼び出し元で per-article ロックを保持した状態で呼ぶこと。"""
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
            "editorial_take": "",
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
            if claude_blocks and is_generated_article_sufficient(claude_blocks):
                return claude_blocks
            if claude_blocks:
                logger.info("Claude ミドルマンが品質基準未達のため OpenAI へフォールバック")
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
    editorial_take = ""

    with ThreadPoolExecutor(max_workers=4) as ex:
        fut_blocks = ex.submit(do_blocks)
        blocks = fut_blocks.result()

        # ペルソナ: まず3人まとめてバッチ生成（呼び出し回数を最小化するため Claude CLI → Gemini/OpenAI の順）
        # 全滅 or 特定人物が短すぎた場合のみ個別呼び出しにフォールバック
        batch_payload = _generate_personas_via_claude(title, persona_source, display_persona_ids, category=category)
        batch_results = batch_payload.get("personas") if isinstance(batch_payload, dict) else []
        editorial_take = str(batch_payload.get("editorial_take") or "") if isinstance(batch_payload, dict) else ""
        if not any(batch_results):
            batch_results = get_all_persona_opinions_batch(
                title, persona_source, display_persona_ids, category=category
            )
        if not editorial_take:
            editorial_take = generate_editorial_take_fallback(title, persona_source, category=category)
        generated_comments: list[str] = []
        for slot_idx, pid in enumerate(display_persona_ids):
            batch_comment = batch_results[slot_idx] if slot_idx < len(batch_results) else ""
            if batch_comment:
                personas_3[slot_idx] = batch_comment
                generated_comments.append(batch_comment)
                continue
            # フォールバック: 個別呼び出し
            logger.info("ペルソナ%d個別フォールバック (pid=%d)", slot_idx, pid)
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
        "editorial_take": editorial_take,
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
            editorial_take=editorial_take,
        )
    return result
