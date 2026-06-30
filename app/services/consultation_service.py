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

# Xトレンドへの返信用: 何でも否定から入る癖がつかないよう、賛同もできる旨を明示する
_TREND_REPLY_EXTRA_RULE = (
    "\n- 内容に心から賛同できるなら、力強く肯定・称賛してよい。納得できない点があるときだけ、"
    "お前の哲学・価値観で率直に異論を述べよ。何でも否定から入る必要はない。"
)


def _generate_with_claude_cli(name: str, role: str, question: str, *, allow_agreement: bool = False) -> str:
    """Claude Code CLI（subprocess）でペルソナ回答を生成する。"""
    from app.services.claude_researcher import run_claude_text_gen, is_claude_available
    if not is_claude_available():
        return ""
    system_prompt = _PERSONA_SYSTEM_TEMPLATE.format(name=name, role=role)
    if allow_agreement:
        system_prompt += _TREND_REPLY_EXTRA_RULE
    prompt = f"{system_prompt}\n\n【相談】\n{question}"
    return run_claude_text_gen(prompt, timeout=60, usage_kind="persona")


def generate_consultation_answer(persona_id: int, question: str, *, allow_agreement: bool = False) -> str:
    """指定した偉人が相談に答えるテキストを生成する。
    allow_agreement=True のとき、何でも否定から入らず賛同もできることを明示する
    （Xトレンドへの返信など、何でも批判するキャラに見えてほしくない用途向け）。"""
    from app.services.ai_service import PERSONAS

    if persona_id < 0 or persona_id >= len(PERSONAS):
        raise ValueError(f"不正な persona_id: {persona_id}")

    p = PERSONAS[persona_id]

    # Claude CLI が使えれば優先（ローカル環境。Render では自動スキップ）
    from app.services.claude_researcher import is_claude_available
    if is_claude_available():
        try:
            result = _generate_with_claude_cli(p["name"], p["role"], question, allow_agreement=allow_agreement)
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
    if allow_agreement:
        system_prompt += _TREND_REPLY_EXTRA_RULE
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


def summarize_post_text(text: str, max_chars: int = 100) -> str:
    """X投稿本文をClaude CLIで要約する。失敗時は原文を切り詰めて返す。"""
    text = (text or "").strip()
    if not text:
        return ""
    from app.services.claude_researcher import run_claude_text_gen, is_claude_available
    if is_claude_available():
        prompt = (
            f"以下のX(Twitter)投稿を日本語で{max_chars}字程度に要約してください。\n"
            "説明・前置き・引用符は不要、要約文のみを出力してください。\n\n"
            f"【投稿】\n{text[:500]}"
        )
        try:
            out = run_claude_text_gen(prompt, timeout=60, usage_kind="x_trend_summary").strip().strip("「」\"'")
            if out:
                return out[: max_chars + 20]
        except Exception as e:
            logger.warning("投稿要約失敗、原文を使用: %s", e)
    return text[:max_chars]


def compress_to_140(text: str, limit: int = 140) -> str:
    """文章をClaude CLIで指定字数以内に圧縮する。失敗時は単純切り詰め。"""
    text = (text or "").strip()
    if not text or len(text) <= limit:
        return text
    from app.services.claude_researcher import run_claude_text_gen, is_claude_available
    if is_claude_available():
        prompt = (
            f"以下の文章を意味・トーンを保ったまま日本語で{limit}字以内に圧縮してください。\n"
            "説明・前置き・引用符は不要、圧縮後の文章のみを出力してください。\n\n"
            f"【文章】\n{text}"
        )
        try:
            out = run_claude_text_gen(prompt, timeout=60, usage_kind="x_compress_140").strip().strip("「」\"'")
            if out:
                return out[:limit]
        except Exception as e:
            logger.warning("140字圧縮失敗、単純切り詰めを使用: %s", e)
    return text[:limit]


def format_trend_post(persona_name: str, persona_emoji: str, comment: str, limit: int = 140) -> str:
    """誰のコメントか分かるヘッダーと知リポAIのサイトURLを付けてX投稿用に整形する。"""
    from app.config import settings

    site_url = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    hashtag = "#知リポAI"

    header = f"【{persona_emoji}{persona_name}の一言】\n「" if persona_name else "「"
    tail = "」\n\n" + hashtag + (f"\n{site_url}" if site_url else "")

    available = max(10, limit - len(header) - len(tail))
    comment = (comment or "").strip()
    if len(comment) <= available:
        body = comment
    else:
        chunk = comment[: available - 1]
        body = None
        for sep in ("。", "、", "！", "？", "」", "・", " "):
            i = chunk.rfind(sep)
            if i >= available // 2:
                body = chunk[: i + 1] if sep in ("。", "！", "？", "」") else chunk[:i]
                break
        if body is None:
            body = chunk
        body = body.rstrip("、・ ") + "…"
    return header + body + tail


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


def run_trend_comment_once() -> bool:
    """急上昇ポストへの偉人コメント自動生成（Notionドラフト）。現在は無効。"""
    logger.info("Xトレンドコメント生成は無効化されています（スキップ）")
    return False
