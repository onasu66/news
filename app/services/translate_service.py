"""海外記事の日本語訳・言い換え（著作権配慮）"""
import re

from app.utils.openai_compat import create_with_retry

FOREIGN_SOURCES = {"Reuters", "AP News", "BBC News", "共同通信"}


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

【元タイトル】{title[:300]}

【元要約】
{summary[:800]}

以下の形式のみで返してください。
===タイトル===
（日本語のタイトルを1行で）
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
