"""海外記事の日本語訳・言い換え（著作権配慮）"""
import re

from app.utils.openai_compat import create_with_retry

FOREIGN_SOURCES = {"Reuters", "AP News", "BBC News", "共同通信", "World News International", "Le Monde"}


def title_looks_english(title: str) -> bool:
    """タイトルが英語主体か（日本語化の必要判定用）"""
    if not title or len(title) < 3:
        return False
    ascii_count = sum(1 for c in title if ord(c) < 128 and c.isalpha())
    letter_count = sum(1 for c in title if c.isalpha())
    if letter_count < 3:
        return False
    return ascii_count / letter_count > 0.5


def summary_looks_english(summary: str) -> bool:
    """要約が英語主体か（日本語化の必要判定用）"""
    if not summary or len(summary) < 10:
        return False
    sample = summary[:600]
    ascii_letters = sum(1 for c in sample if ord(c) < 128 and c.isalpha())
    letters = sum(1 for c in sample if c.isalpha())
    if letters < 5:
        return False
    return ascii_letters / letters > 0.4


def text_mainly_japanese(text: str) -> bool:
    """テキストが主に日本語か（ひらがな・カタカナ・漢字の割合）"""
    if not text or len(text) < 5:
        return True
    sample = text[:500]
    ja_count = sum(1 for c in sample if '\u3040' <= c <= '\u309f' or '\u30a0' <= c <= '\u30ff' or '\u4e00' <= c <= '\u9fff')
    return ja_count / len(sample) > 0.3


def is_foreign_article(source: str, title: str, summary: str) -> bool:
    """海外ソースまたは英語コンテンツか（日本語訳が必要なら True）"""
    if source in FOREIGN_SOURCES:
        return True
    if title_looks_english(title):
        return True
    if summary and summary_looks_english(summary):
        return True
    # タイトル・要約のいずれかが主に日本語でなければ翻訳対象
    if title and not text_mainly_japanese(title):
        return True
    if summary and not text_mainly_japanese(summary):
        return True
    text = f"{title} {summary}"
    if not text or len(text) < 5:
        return False
    ascii_count = sum(1 for c in text if ord(c) < 128)
    return ascii_count / len(text) > 0.5


def translate_title_to_japanese(english_title: str) -> str:
    """タイトルだけを日本語に翻訳。必ず日本語のみで返す。"""
    if not english_title or not english_title.strip():
        return english_title
    if text_mainly_japanese(english_title):
        return english_title
    try:
        from openai import OpenAI
        from app.config import settings
        if not settings.OPENAI_API_KEY:
            return english_title
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        model = settings.OPENAI_MODEL
        prompt = f"""次のニュースのタイトルを日本語に翻訳してください。
ルール：出力は日本語のタイトルだけを1行で返す。英語は1文字も含めない。カタカナ・漢字・ひらがなで書く。

【元タイトル】
{english_title[:400]}"""
        resp = create_with_retry(
            client,
            200,
            model=model,
            messages=[
                {"role": "system", "content": "あなたはニュースの見出しを日本語に翻訳するアシスタントです。出力は必ず日本語のみ。英語は使わない。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw and text_mainly_japanese(raw):
            return raw[:200]
    except Exception:
        pass
    return english_title


def translate_and_rewrite(title: str, summary: str) -> tuple[str, str]:
    """海外記事を日本語に訳し、独自の表現で言い換える（著作権配慮）"""
    try:
        from openai import OpenAI
        from app.config import settings

        if not settings.OPENAI_API_KEY:
            return title, summary

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        model = settings.OPENAI_MODEL
        prompt = f"""以下の英語ニュースのタイトルと要約を、必ず日本語だけに訳し、独自の表現で言い直してください。
重要：タイトルも要約も、日本語以外（英語など）は1文字も含めないこと。全てカタカナ・漢字・ひらがなで書く。

■ タイトルは【】で囲んだ短い語句から始める（【】の中も日本語。例：【衝撃】【速報】【なぜ】）。
■ 【】の後の見出しも必ず日本語で。

【元タイトル】{title[:300]}

【元要約】
{summary[:800]}

以下の形式のみで返す。
===タイトル===
（日本語のタイトルを1行だけ。【○○】から始め、全て日本語）
===要約===
（日本語の要約を2〜4文で）"""

        resp = create_with_retry(
            client,
            500,
            model=model,
            messages=[
                {"role": "system", "content": "ニュースを日本語で分かりやすく言い換えるアシスタント。出力は必ず日本語のみ。英語は使わない。"},
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
                new_title = rest[0].strip().strip('"').strip()[:200] or title
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
