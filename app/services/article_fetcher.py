"""記事URLから本文を取得（RSS要約を補強・全文取得する用）"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 取得タイムアウト（秒）
FETCH_TIMEOUT = 20
# 本文の最大文字数（記事を厚くするため多めに取得）
MAX_BODY_LEN = 50000

# 本文を含みそうなセレクター（readability が効かないときのフォールバック）
MAIN_SELECTORS = [
    "article",
    "[role='main']",
    "main",
    ".article-body",
    ".article-content",
    ".post-content",
    ".entry-content",
    ".content-body",
    ".news-body",
    ".post-body",
    ".article__body",
    ".newsContent",
    "#main",
    "#content",
    ".main-content",
    ".entry",
]


def _html_to_text(html: str) -> str:
    """HTMLをプレーンテキストに（タグ除去・空白正規化）"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


def fetch_article_body(url: str) -> Optional[str]:
    """
    記事URLを取得し、本文をできるだけ全文抽出して返す。
    まず readability-lxml で本文を抽出し、失敗時はセレクターで抽出。
    失敗時は None（RSS要約のみ使う）。
    """
    if not url or not url.startswith("http"):
        return None
    try:
        import httpx

        with httpx.Client(
            follow_redirects=True,
            timeout=FETCH_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ja,en;q=0.9",
            },
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            raw = resp.content
            text = resp.text
    except Exception as e:
        logger.info("記事取得スキップ %s: %s", url[:60], e)
        return None

    try:
        body_text = None

        # 1) readability-lxml で本文を抽出（多くのニュースサイトで全文取りに有利）
        try:
            from readability import Document
            doc = Document(raw)
            summary_html = doc.summary()
            if summary_html and len(summary_html) > 200:
                body_text = _html_to_text(summary_html)
        except Exception as e:
            logger.debug("readability スキップ %s: %s", url[:50], e)

        # 2) フォールバック: セレクターで抽出（readability が使えない or 短いとき）
        if not body_text or len(body_text) < 300:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "iframe"]):
                tag.decompose()
            fallback_text = None
            for sel in MAIN_SELECTORS:
                node = soup.select_one(sel)
                if node:
                    t = node.get_text(separator="\n", strip=True)
                    t = re.sub(r"\n{3,}", "\n\n", t)
                    if len(t) > 200:
                        fallback_text = t
                        break
            if not fallback_text:
                body = soup.find("body")
                if body:
                    fallback_text = body.get_text(separator="\n", strip=True)
                    fallback_text = re.sub(r"\n{3,}", "\n\n", fallback_text)
            if fallback_text and (not body_text or len(fallback_text) > len(body_text)):
                body_text = fallback_text

        if not body_text or len(body_text) < 100:
            return None
        return body_text[:MAX_BODY_LEN]
    except Exception as e:
        logger.warning("本文抽出エラー %s: %s", url[:50], e)
        return None
