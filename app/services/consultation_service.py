"""偉人への相談 - AI回答生成 + X ハッシュタグ検索"""
import logging

logger = logging.getLogger(__name__)

_ANSWER_MAX_TOKENS = 600

_PERSONA_SYSTEM_TEMPLATE = (
    "あなたはAIではない。2026年に死から蘇り、現代人の悩みを受け取った本物の{name}だ。\n\n"
    "【人物設定（絶対に崩すな）】\n{role}\n\n"
    "【回答のルール】\n"
    "- 冒頭の一文で読者を引き込め。驚き・矛盾・逆説から入るのが効く。\n"
    "- お前自身の失敗・苦境・確信から語れ。理屈でなく体験で刺せ。\n"
    "- 相談者を慰めるな。お前の哲学・価値観で正面から答えよ。\n"
    "- 語り口は独白。丁寧語・同調表現禁止。\n"
    "- 日本語のみ。箇条書き禁止。前置き・署名不要。\n"
    "- 相談者が具体的に動けるよう、解決策や次の一手を必ず一つ提示せよ。\n"
    "- 150文字前後で完結させよ。"
)


def _generate_with_claude_cli(name: str, role: str, question: str) -> str:
    """Claude Code CLI（subprocess）でペルソナ回答を生成する。"""
    from app.services.claude_researcher import run_claude_text_gen, is_claude_available
    if not is_claude_available():
        return ""
    system_prompt = _PERSONA_SYSTEM_TEMPLATE.format(name=name, role=role)
    prompt = f"{system_prompt}\n\n【相談】\n{question}"
    return run_claude_text_gen(prompt, timeout=60, usage_kind="persona")


def generate_consultation_answer(persona_id: int, question: str) -> str:
    """指定した偉人が相談に答えるテキストを生成する。"""
    from app.services.ai_service import PERSONAS

    if persona_id < 0 or persona_id >= len(PERSONAS):
        raise ValueError(f"不正な persona_id: {persona_id}")

    p = PERSONAS[persona_id]

    # Claude CLI が使えれば優先（ローカル環境。Render では自動スキップ）
    from app.services.claude_researcher import is_claude_available
    if is_claude_available():
        try:
            result = _generate_with_claude_cli(p["name"], p["role"], question)
            if result:
                return result
        except Exception as e:
            logger.warning("Claude CLI ペルソナ生成失敗、フォールバック: %s", e)

    # OpenAI / Gemini フォールバック
    from app.utils.llm_client import get_chat_client, resolve_persona_model, persona_provider, is_ai_configured

    if not is_ai_configured(provider=persona_provider()):
        raise RuntimeError("AIキーが設定されていません")

    client = get_chat_client(provider=persona_provider())
    model = resolve_persona_model()
    system_prompt = _PERSONA_SYSTEM_TEMPLATE.format(name=p["name"], role=p["role"])
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"【相談】\n{question}"},
    ]

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.85,
            max_completion_tokens=_ANSWER_MAX_TOKENS,
            gemini_task="persona",
        )
    except TypeError:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.85,
            max_completion_tokens=_ANSWER_MAX_TOKENS,
        )

    return (resp.choices[0].message.content or "").strip()


def fetch_x_posts_by_tag(tag: str, limit: int = 20) -> list[dict]:
    """Nitter経由でハッシュタグの投稿を取得する。"""
    try:
        import httpx
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise RuntimeError(f"依存ライブラリが不足しています: {e}") from e

    NITTER_INSTANCES = [
        "https://nitter.privacyredirect.com",
        "https://nitter.catsarch.com",
        "https://nitter.tiekoetter.com",
    ]
    tag_clean = tag.lstrip("#").strip()
    results: list[dict] = []

    for base in NITTER_INSTANCES:
        try:
            url = f"{base}/search?q=%23{tag_clean}&f=tweets"
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                resp = client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; NewsSite/1.0)"})
                resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for tweet in soup.select(".timeline-item"):
                text_el = tweet.select_one(".tweet-content")
                user_el = tweet.select_one(".username")
                if not text_el:
                    continue
                text = text_el.get_text(" ", strip=True)
                user = user_el.get_text(strip=True) if user_el else "匿名"
                if len(text) > 10:
                    results.append({"user": user, "text": text})
                if len(results) >= limit:
                    break
            if results:
                break
        except Exception as e:
            logger.debug("Nitter %s 失敗: %s", base, e)
            continue

    return results
