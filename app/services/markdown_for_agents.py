"""Markdown for Agents — Accept: text/markdown コンテンツネゴシエーション"""
from __future__ import annotations

import json
import re
from typing import Any

import yaml
from fastapi import Request
from fastapi.responses import Response

CONTENT_SIGNAL = "ai-train=yes, search=yes, ai-input=yes"

_SKIP_PREFIXES = ("/static/", "/api/", "/admin")
_SKIP_EXACT = {
    "/robots.txt",
    "/sitemap.xml",
    "/sitemap-news.xml",
    "/indexnow-key.txt",
    "/llms.txt",
}


def accepts_markdown(request: Request) -> bool:
    """Accept ヘッダーで text/markdown が要求されているか"""
    accept = (request.headers.get("accept") or "").lower()
    if "text/markdown" not in accept:
        return False
    md_q = _mime_qvalue(accept, "text/markdown")
    html_q = _mime_qvalue(accept, "text/html")
    if md_q is None:
        return False
    if html_q is None:
        return True
    return md_q >= html_q


def _mime_qvalue(accept: str, mime: str) -> float | None:
    best: float | None = None
    for part in accept.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = [t.strip() for t in part.split(";")]
        part_mime = tokens[0].lower()
        q = 1.0
        for token in tokens[1:]:
            if token.startswith("q="):
                try:
                    q = float(token[2:])
                except ValueError:
                    q = 1.0
        if part_mime == mime or part_mime == "*/*":
            best = q if best is None else max(best, q)
    return best


def should_convert_path(path: str) -> bool:
    if path in _SKIP_EXACT:
        return False
    return not any(path.startswith(prefix) for prefix in _SKIP_PREFIXES)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 3)


def default_agent_frontmatter(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ai-train": "allow",
        "ai-search": "allow",
    }
    base.update(extra)
    return base


def _yaml_frontmatter(fields: dict[str, Any]) -> str:
    cleaned = {k: v for k, v in fields.items() if v is not None and str(v).strip() != ""}
    if not cleaned:
        return ""
    body = yaml.safe_dump(cleaned, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{body}\n---"


def assemble_markdown_document(
    body: str,
    *,
    frontmatter: dict[str, Any] | None = None,
    jsonld: list | dict | None = None,
) -> str:
    parts: list[str] = []
    fm = _yaml_frontmatter(frontmatter or default_agent_frontmatter())
    if fm:
        parts.append(fm)
    parts.append(body.strip())
    if jsonld:
        docs = jsonld if isinstance(jsonld, list) else [jsonld]
        lines: list[str] = []
        for doc in docs:
            if doc:
                lines.append(json.dumps(doc, ensure_ascii=False))
        if lines:
            parts.append("```json\n" + "\n".join(lines) + "\n```")
    return "\n\n".join(p for p in parts if p)


def build_markdown_response(
    body: str,
    *,
    frontmatter: dict[str, Any] | None = None,
    jsonld: list | dict | None = None,
    original_html: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    content = assemble_markdown_document(body, frontmatter=frontmatter, jsonld=jsonld)
    headers = {
        "Content-Type": "text/markdown; charset=utf-8",
        "Vary": "Accept",
        "Content-Signal": CONTENT_SIGNAL,
        "x-markdown-tokens": str(estimate_tokens(content)),
    }
    if original_html is not None:
        headers["x-original-tokens"] = str(estimate_tokens(original_html))
    if extra_headers:
        headers.update(extra_headers)
    return Response(content=content, headers=headers, media_type="text/markdown; charset=utf-8")


def _meta_content(soup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
    if tag and tag.get("content"):
        return str(tag["content"]).strip()
    return ""


def html_to_markdown(html: str) -> tuple[str, dict[str, Any], list[Any]]:
    """HTML ページを Markdown に変換（ミドルウェア用フォールバック）"""
    from bs4 import BeautifulSoup
    import html2text

    soup = BeautifulSoup(html, "html.parser")
    frontmatter = default_agent_frontmatter()
    title = _meta_content(soup, "og:title") or _meta_content(soup, "title")
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    desc = _meta_content(soup, "description") or _meta_content(soup, "og:description")
    image = _meta_content(soup, "og:image")
    canonical = soup.find("link", rel="canonical")
    if title:
        frontmatter["title"] = title
    if desc:
        frontmatter["description"] = desc
    if image:
        frontmatter["image"] = image
    if canonical and canonical.get("href"):
        frontmatter["url"] = str(canonical["href"]).strip()

    jsonld: list[Any] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            jsonld.append(json.loads(raw))
        except Exception:
            continue

    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup.find_all(["header", "footer", "nav"]):
        tag.decompose()

    main = soup.find("main") or soup.find(id="main-content") or soup.body
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = False
    converter.single_line_break = True
    md = converter.handle(str(main or "")).strip()
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md, frontmatter, jsonld


def blocks_to_markdown(blocks: list) -> str:
    """記事ブロックを Markdown 本文に変換"""
    safe = [b for b in (blocks or []) if isinstance(b, dict)]
    if not safe:
        return ""
    parts: list[str] = []
    nav_labels = {
        "facts": "ニュース",
        "background": "背景",
        "impact": "影響範囲",
        "prediction": "予測",
        "caution": "注意",
    }
    is_navigator = safe[0].get("type") == "navigator_section"
    if is_navigator:
        for block in safe:
            if block.get("type") != "navigator_section" or not block.get("section"):
                continue
            body = (block.get("content") or "").strip()
            if not body:
                continue
            section = block["section"]
            if section == "facts":
                for para in body.split("\n\n"):
                    para = para.strip()
                    if para:
                        parts.append(para)
            else:
                label = nav_labels.get(section, section)
                parts.append(f"### {label}\n\n{body}")
    else:
        for block in safe:
            if block.get("type") == "text":
                for para in (block.get("content") or "").strip().split("\n\n"):
                    para = para.strip()
                    if para:
                        parts.append(para)
            elif block.get("type") == "explain":
                content = (block.get("content") or "").strip()
                if content:
                    parts.append(f"### ミドルマン解説\n\n{content}")
    return "\n\n".join(parts)


def build_topic_markdown(
    *,
    item,
    article_url: str,
    meta_desc: str,
    og_image: str,
    blocks: list,
    quick_understand: dict | None,
    personas_data: list[str],
    display_personas: list[dict],
    page_jsonld: dict | None,
    related_articles: list | None = None,
    site_url: str,
) -> tuple[str, dict[str, Any], dict | None]:
    """トピック記事のネイティブ Markdown（HTML 変換より高精度）"""
    title = (getattr(item, "title", "") or "").strip()
    summary = (getattr(item, "summary", "") or "").strip()
    category = (getattr(item, "category", "") or "").strip()
    source = (getattr(item, "source", "") or "").strip()
    source_link = (getattr(item, "link", "") or "").strip()

    frontmatter = default_agent_frontmatter(
        title=title,
        description=meta_desc or summary[:160],
        image=og_image or None,
        url=article_url,
        category=category or None,
        source=source or None,
    )
    published = getattr(item, "published", None)
    if published is not None and hasattr(published, "isoformat"):
        frontmatter["datePublished"] = published.isoformat()

    lines = [f"# {title}", ""]
    if summary:
        lines.extend([summary, ""])
    if isinstance(quick_understand, dict):
        qu_parts: list[str] = []
        for key, label in (("what", "要点"), ("why", "なぜ"), ("how", "どう理解する")):
            val = quick_understand.get(key)
            text = val.strip() if isinstance(val, str) else ""
            if text:
                qu_parts.append(f"- **{label}**: {text}")
        if qu_parts:
            lines.extend(["## 1分で理解", ""] + qu_parts + [""])

    body_md = blocks_to_markdown(blocks)
    if body_md:
        lines.extend(["## 解説", "", body_md, ""])

    persona_lines: list[str] = []
    for persona, comment in zip(display_personas or [], personas_data or []):
        name = (persona.get("name") if isinstance(persona, dict) else "") or ""
        comment_text = (comment or "").strip()
        if name and comment_text:
            persona_lines.append(f"### {name}\n\n{comment_text}")
    if persona_lines:
        lines.extend(["## 偉人AIの視点", ""] + persona_lines + [""])

    meta_bits = []
    if category:
        meta_bits.append(f"カテゴリ: {category}")
    if source:
        meta_bits.append(f"出典: {source}")
    if source_link:
        meta_bits.append(f"原文: {source_link}")
    if meta_bits:
        lines.extend(["---", " | ".join(meta_bits), ""])

    related = related_articles or []
    if related:
        lines.extend(["## 関連記事", ""])
        for rel in related[:8]:
            rel_title = (getattr(rel, "title", "") or "").strip()
            rel_id = getattr(rel, "id", "")
            if rel_title and rel_id:
                lines.append(f"- [{rel_title}]({site_url.rstrip('/')}/topic/{rel_id})")
        lines.append("")

    return "\n".join(lines).strip(), frontmatter, page_jsonld


def build_list_markdown(
    *,
    title: str,
    description: str,
    page_url: str,
    site_url: str,
    items: list,
    page_jsonld: dict | None,
    list_heading: str = "記事一覧",
) -> tuple[str, dict[str, Any], dict | None]:
    """一覧ページ（トップ・ニュース）のネイティブ Markdown"""
    frontmatter = default_agent_frontmatter(
        title=title,
        description=description,
        url=page_url,
    )
    lines = [f"# {title}", "", description, "", f"## {list_heading}", ""]
    for item in items[:30]:
        item_title = (getattr(item, "title", "") or "").strip()
        item_id = getattr(item, "id", "")
        if not item_title or not item_id:
            continue
        summary = (getattr(item, "summary", "") or "").strip()
        link = f"{site_url.rstrip('/')}/topic/{item_id}"
        if summary:
            lines.append(f"- [{item_title}]({link}) — {summary[:120]}")
        else:
            lines.append(f"- [{item_title}]({link})")
    return "\n".join(lines).strip(), frontmatter, page_jsonld
