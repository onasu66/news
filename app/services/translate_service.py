"""海外記事の日本語訳・言い換え（著作権配慮）"""
import re

from app.utils.openai_compat import create_with_retry

FOREIGN_SOURCES = {"Reuters", "AP News", "BBC News", "共同通信", "World News International", "Le Monde"}


def is_foreign_article(source: str, title: str, summary: str) -> bool:
    """海外ソースまたは英語コンテンツか"""
    if source in FOREIGN_SOURCES:
        return True
    text = f"{title} {summary}"
    if not text or len(text) < 5:
        return False
    ascii_count = sum(1 for c in text if ord(c) < 128)
    return ascii_count / len(text) > 0.5


def translate_and_rewrite(title: str, summary: str) -> tuple[str, str]:
    """海外記事を日本語に訳し、独自の表現で言い換える（著作権配慮）"""
    try:
        from openai import OpenAI
        from app.config import settings

        if not settings.OPENAI_API_KEY:
            return title, summary

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        model = settings.OPENAI_MODEL
        prompt = f"""以下の英語ニュースのタイトルと要約を、日本語に訳し、独自の表現で言い直してください。
元の文章をそのまま訳すのではなく、意味を保ちながら別の言い方で書き直してください（著作権配慮）。

■ タイトルは【】で囲んだインパクトのある短い語句から始めてください。
  例：【ついに】〇〇が〇〇に、【なぜ】〇〇は〇〇なのか、【衝撃】〇〇が判明、【速報】〇〇を発表
  ※【】の中は2〜5文字程度の短い語句。内容に合う自然なものにする。

【元タイトル】{title[:300]}

【元要約】
{summary[:800]}

以下の形式のみで返してください。
===タイトル===
（日本語のタイトルを1行で。【○○】から始める）
===要約===
（日本語の要約を2〜4文で）"""

        resp = create_with_retry(
            client,
            500,
            model=model,
            messages=[
                {"role": "system", "content": "ニュースを日本語で分かりやすく言い換えるアシスタント。元文をコピーせず独自表現で。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
        )
        raw = resp.choices[0].message.content or ""

        new_title = title
        new_summary = summary
        if "===タイトル===" in raw:
            parts = raw.split("===タイトル===", 1)
            if len(parts) > 1:
                rest = parts[1].split("===要約===", 1)
                new_title = rest[0].strip()[:200] or title
                if len(rest) > 1:
                    new_summary = rest[1].strip()[:500] or summary

        return new_title, new_summary
    except Exception:
        return title, summary


def translate_article_body(body: str, max_chars: int = 25000) -> str:
    """英語の記事本文を日本語に翻訳。APIキー未設定や失敗時は原文を返す"""
    if not body or len(body) < 50:
        return body
    try:
        from openai import OpenAI
        from app.config import settings

        if not settings.OPENAI_API_KEY:
            return body

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        model = settings.OPENAI_MODEL
        prompt = f"""以下の英語ニュース記事の本文を、日本語に翻訳してください。
・意味を保ちながら自然な日本語に。著作権に配慮し、独自の表現で言い換えてください。
・専門用語は必要に応じて補足説明を添える。
・翻訳後の本文のみ返し、余計な説明は不要。

【元の本文】
{body[:max_chars]}
"""
        resp = create_with_retry(
            client,
            8000,
            model=model,
            messages=[
                {"role": "system", "content": "ニュース記事を日本語に翻訳するアシスタント。自然な日本語で、余計な説明は出力しない。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        raw = resp.choices[0].message.content or ""
        if raw and len(raw.strip()) > 100:
            return raw.strip()
    except Exception:
        pass
    return body
