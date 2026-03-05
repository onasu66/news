"""ニュース関連ルート"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, Form, Header
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import settings, is_rss_and_ai_disabled
from app.services.news_aggregator import NewsAggregator
from app.services.rss_service import NewsItem, sanitize_display_text
from app.services.article_cache import save_article
from app.services.ai_batch_service import generate_all_explanations
from app.services.ai_service import (
    explain_article_with_ai,
    get_image_url,
    PERSONAS,
    PERSONA_LOGIC_IDS,
    PERSONA_ENT_IDS,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/robots.txt")
async def robots_txt(request: Request):
    """検索エンジン向け robots.txt"""
    site_url = _get_site_url(request)
    body = f"User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /confirm\n\nSitemap: {site_url}/sitemap.xml\n"
    return Response(content=body, media_type="text/plain; charset=utf-8")


@router.get("/sitemap.xml")
async def sitemap_xml(request: Request):
    """SEO用 sitemap.xml（一覧は NewsAggregator キャッシュ利用で Firestore 読取を抑える）"""
    site_url = _get_site_url(request)
    articles = NewsAggregator.get_news()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f"  <url><loc>{site_url}/</loc><changefreq>hourly</changefreq><priority>1.0</priority></url>",
    ]
    for a in articles[:5000]:
        lines.append(f"  <url><loc>{site_url}/topic/{a.id}</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>")
    lines.append("</urlset>")
    return Response(content="\n".join(lines), media_type="application/xml; charset=utf-8")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, page: int = 1, keyword: str = ""):
    """トップページ（ジャンル別表示・ページネーション対応）。keyword 指定時は関連記事のみ表示"""
    from app.services.news_aggregator import CATEGORY_ORDER, ITEMS_PER_PAGE
    keyword = (keyword or "").strip()
    if keyword:
        # キーワードでフィルタ：全記事からタイトル・要約に含まれるものだけ残し、ページネーション＋ジャンル再集計
        all_news = NewsAggregator.get_news()
        kw_lower = keyword.lower()
        filtered = [a for a in all_news if kw_lower in (a.title or "").lower() or kw_lower in (a.summary or "").lower()]
        per_page = ITEMS_PER_PAGE
        total = len(filtered)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        page_items = filtered[start : start + per_page]
        by_cat: dict[str, list] = {}
        for a in page_items:
            by_cat.setdefault(a.category, []).append(a)
        news_by_category = [(c, by_cat.get(c, [])) for c in CATEGORY_ORDER]
        pagination = {"page": page, "per_page": per_page, "total": total, "total_pages": total_pages, "has_prev": page > 1, "has_next": page < total_pages}
    else:
        news_by_category, pagination = NewsAggregator.get_news_by_category(page=page)
    trends = NewsAggregator.get_trends()
    added_one = None
    for _, items in news_by_category:
        for item in items:
            _ensure_japanese(item)
            if not item.image_url:
                item.image_url = get_image_url(item.id, 400, 225)
            elif item.image_url and not item.image_url.startswith("http"):
                item.image_url = get_image_url(item.image_url, 400, 225)
    site_url = _get_site_url(request)
    og_image = "https://picsum.photos/1200/630"
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "news_by_category": news_by_category,
            "trends": trends,
            "pagination": pagination,
            "added_one": added_one,
            "site_url": site_url,
            "og_image": og_image,
            "search_keyword": keyword,
        }
    )


@router.get("/api/news/page")
async def api_news_page(page: int = 1, keyword: str = ""):
    """無限スクロール用：ページの記事カードHTMLを返す"""
    from app.services.news_aggregator import CATEGORY_ORDER, ITEMS_PER_PAGE
    keyword = (keyword or "").strip()
    if keyword:
        all_news = NewsAggregator.get_news()
        kw_lower = keyword.lower()
        news = [a for a in all_news if kw_lower in (a.title or "").lower() or kw_lower in (a.summary or "").lower()]
    else:
        news = NewsAggregator.get_news()
    total = len(news)
    per_page = ITEMS_PER_PAGE
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    items = news[start : start + per_page]
    for item in items:
        _ensure_japanese(item)
        if not item.image_url:
            item.image_url = get_image_url(item.id, 400, 225)
        elif not item.image_url.startswith("http"):
            item.image_url = get_image_url(item.image_url, 400, 225)
    import html as html_mod
    cards_html = ""
    for item in items:
        pub = item.published.strftime('%m/%d %H:%M') if item.published else ''
        title_safe = html_mod.escape(item.title or "")
        summary_safe = html_mod.escape((item.summary or "")[:80])
        source_safe = html_mod.escape(item.source or "")
        cat_safe = html_mod.escape(item.category or "")
        cards_html += f'''<article class="news-card animate-fade-in" data-category="{cat_safe}">
<a href="/topic/{item.id}" class="news-card-link">
<div class="news-card-image"><img src="{item.image_url or 'https://picsum.photos/400/225'}" alt="{title_safe}" loading="lazy"><span class="news-card-category">{cat_safe}</span></div>
<div class="news-card-body">
<div class="news-card-meta"><span class="news-card-time">🕒 {pub}</span><span class="news-card-source">{source_safe}</span></div>
<h3 class="news-title">{title_safe}</h3>
<p class="news-summary-line">👀 {summary_safe}...</p>
<div class="news-card-footer"><span class="news-card-ai">✍ AIが解説</span><span class="news-badge">AI解説</span></div>
</div></a></article>'''
    return {"html": cards_html, "page": page, "total_pages": total_pages}


@router.get("/trend", response_class=HTMLResponse)
async def trend_page(request: Request):
    """トレンドページ：スコアが高い記事"""
    all_news = NewsAggregator.get_news()
    trends = NewsAggregator.get_trends()
    trend_keywords = [t.keyword for t in trends]
    scored = []
    for item in all_news:
        text = f"{item.title} {item.summary}"
        score = sum(1 for kw in trend_keywords if kw.lower() in text.lower())
        scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    top_articles = [item for _, item in scored[:30]]
    for item in top_articles:
        _ensure_japanese(item)
        if not item.image_url:
            item.image_url = get_image_url(item.id, 400, 225)
        elif not item.image_url.startswith("http"):
            item.image_url = get_image_url(item.image_url, 400, 225)
    return templates.TemplateResponse("trend.html", {"request": request, "articles": top_articles, "trends": trends})


@router.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request):
    """AIページ：おすすめ・昨日のメモ・人格コメント"""
    all_news = NewsAggregator.get_news()
    recommended = all_news[:6]
    for item in recommended:
        _ensure_japanese(item)
        if not item.image_url:
            item.image_url = get_image_url(item.id, 400, 225)
        elif not item.image_url.startswith("http"):
            item.image_url = get_image_url(item.image_url, 400, 225)
    ai_memo = None
    ai_personas = []
    try:
        from app.services.ai_daily import get_daily_ai_content
        daily = get_daily_ai_content()
        if daily:
            ai_memo = daily.get("memo", "")
            ai_personas = daily.get("persona_comments", [])
    except Exception:
        pass
    return templates.TemplateResponse("ai.html", {"request": request, "recommended": recommended, "ai_memo": ai_memo, "ai_personas": ai_personas})


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = ""):
    """探すページ"""
    q = (q or "").strip()
    results = []
    if q:
        all_news = NewsAggregator.get_news()
        ql = q.lower()
        results = [a for a in all_news if ql in (a.title or "").lower() or ql in (a.summary or "").lower()][:50]
        for item in results:
            _ensure_japanese(item)
            if not item.image_url:
                item.image_url = get_image_url(item.id, 400, 225)
            elif not item.image_url.startswith("http"):
                item.image_url = get_image_url(item.image_url, 400, 225)
    return templates.TemplateResponse("search.html", {"request": request, "query": q, "results": results})


@router.get("/saved", response_class=HTMLResponse)
async def saved_page(request: Request):
    """保存済み記事ページ（ローカルストレージベース）"""
    return templates.TemplateResponse("saved.html", {"request": request})


# 確認用ページで表示する直近の記事数
CONFIRM_PAGE_ARTICLE_LIMIT = 20


@router.get("/confirm", response_class=HTMLResponse)
async def confirm_page(request: Request):
    """確認用ページ：最新記事の1件取り込みと、記事の削除"""
    news = NewsAggregator.get_news()[:CONFIRM_PAGE_ARTICLE_LIMIT]
    for item in news:
        if not item.image_url:
            item.image_url = get_image_url(item.id, 400, 225)
        elif item.image_url and not item.image_url.startswith("http"):
            item.image_url = get_image_url(item.image_url, 400, 225)
    return templates.TemplateResponse(
        "confirm.html",
        {"request": request, "recent_articles": news}
    )


def _ensure_japanese(item):
    """保存時に日本語化済みのため、表示側では何もしない"""
    pass


def _get_site_url(request: Request) -> str:
    """サイトの絶対URL（末尾スラッシュなし）"""
    base = getattr(settings, "SITE_URL", "").strip().rstrip("/")
    if base:
        return base
    return str(request.base_url).rstrip("/")


def _meta_description_qa(title: str, summary: str | None, max_len: int = 160) -> str:
    """質問＋解答型のmeta description（SEO向け・「なぜ」「理由」「何」を入れる）"""
    t = (title or "").strip()
    s = (summary or "").replace("\n", " ").strip()[:200]
    if not t:
        return (s[: max_len - 3] + "...") if len(s) > max_len else s
    # SEO用に「なぜ」「理由」「何」を含む疑問形にする
    if "なぜ" in t or "理由" in t:
        question = f"{t}の理由とは？"
    elif "何" in t or "とは" in t:
        question = f"{t}を解説"
    else:
        question = f"{t}とは何？なぜ起きた？"
    if not s:
        return question[:max_len]
    answer = s[: max_len - len(question) - 4] + "..." if len(s) > max_len - len(question) - 2 else s
    return f"{question} {answer}"[:max_len]


def _build_short_summary(blocks: list, fallback_summary: str | None) -> str:
    """ブロックから「1分で理解」用の要点まとめHTMLを生成"""
    import html as _html
    if not blocks:
        s = (fallback_summary or "").strip()
        return f'<p class="article-text">{_html.escape(s)}</p>' if s else ""

    points = []
    is_nav = blocks and blocks[0].get("type") == "navigator_section"
    if is_nav:
        nav_labels = {"facts": "📌 事実", "background": "📖 背景", "impact": "🎯 影響", "prediction": "🔮 予測", "caution": "⚠ 注意"}
        for b in blocks:
            sec = b.get("section", "")
            content = (b.get("content") or "").strip()
            if sec in nav_labels and content:
                label = nav_labels[sec]
                snippet = content[:50]
                safe = _html.escape(snippet).replace("\n", "<br>")
                # 「…」での省略表示はせず、端的な一言として50文字以内で切る
                points.append(f'<div class="short-point"><span class="short-point-label">{label}</span><p>{safe}</p></div>')
    else:
        text_parts = []
        for b in blocks:
            if b.get("type") == "text":
                text_parts.append((b.get("content") or "").strip())
        combined = "\n\n".join(text_parts)
        paras = [p.strip() for p in combined.split("\n\n") if p.strip()]
        for p in paras[:3]:
            snippet = p[:50]
            safe = _html.escape(snippet).replace("\n", "<br>")
            points.append(f'<div class="short-point"><p>{safe}</p></div>')
    return "\n".join(points) if points else f'<p class="article-text">{_html.escape((fallback_summary or "")[:500])}</p>'


def _blocks_to_html(blocks: list) -> str:
    """ブロックをHTMLに変換。本文のみ表示。ミドルマン解説はフローティング吹き出し用の JSON データとして埋め込む"""
    if not blocks:
        return ""
    import html as _h
    import json as _json
    text_parts: list[str] = []
    float_items: list[dict] = []
    is_navigator = blocks and blocks[0].get("type") == "navigator_section"
    nav_labels = {"facts": "ニュース", "background": "背景", "impact": "影響範囲", "prediction": "予測", "caution": "注意"}
    if is_navigator:
        for b in blocks:
            if b.get("type") != "navigator_section" or not b.get("section"):
                continue
            body = (b.get("content") or "").strip()
            if not body:
                continue
            if b.get("section") == "facts":
                for p in body.split("\n\n"):
                    p = p.strip()
                    if p:
                        text_parts.append(_h.escape(p).replace("\n", "<br>"))
            else:
                label = nav_labels.get(b["section"], b["section"])
                float_items.append({"label": label, "body": _h.escape(body).replace("\n", "<br>")})
    else:
        for b in blocks:
            if b.get("type") == "text":
                for p in (b.get("content") or "").strip().split("\n\n"):
                    p = p.strip()
                    if p:
                        text_parts.append(_h.escape(p).replace("\n", "<br>"))
            elif b.get("type") == "explain":
                c = (b.get("content") or "").strip()
                if c:
                    float_items.append({"label": "ミドルマン", "body": _h.escape(c).replace("\n", "<br>")})
    out = ['<div class="article-readflow">']
    for i, p in enumerate(text_parts):
        out.append(f'<p class="article-text" data-para="{i}">{p}</p>')
    if float_items:
        out.append(f'<script type="application/json" class="midorman-float-data">{_json.dumps(float_items, ensure_ascii=False)}</script>')
    out.append("</div>")
    return "".join(out)


@router.get("/topic/{topic_id}", response_class=HTMLResponse)
async def topic_detail(request: Request, topic_id: str):
    """トピック詳細（URL: /topic/○○）・AI解説・SEO向け本文"""
    from app.services.explanation_cache import get_cached

    item = NewsAggregator.get_article(topic_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    _ensure_japanese(item)
    image_url = item.image_url or get_image_url(item.id, 800, 450)
    if image_url and not image_url.startswith("http"):
        image_url = get_image_url(image_url, 800, 450)
    site_url = _get_site_url(request)
    article_url = f"{site_url}/topic/{topic_id}"
    og_image = image_url if (image_url or "").startswith("http") else f"{site_url}{image_url}" if image_url else ""
    if not og_image:
        og_image = get_image_url(item.id, 1200, 630)
    cached = get_cached(topic_id)
    blocks = _sanitize_blocks(cached["blocks"]) if cached and cached.get("blocks") else []
    # 新形式: キャッシュに表示用3人分だけ保存されている場合はそのまま使用（API節約）
    if cached and cached.get("display_persona_ids") is not None and len(cached.get("personas", [])) == 3:
        display_persona_ids = cached["display_persona_ids"]
        display_personas = [PERSONAS[i] for i in display_persona_ids]
        personas_data = cached["personas"]
    else:
        all_personas_data = cached.get("personas", []) if cached else []
        import random as _rnd
        logic_ids = list(PERSONA_LOGIC_IDS)
        ent_ids = list(PERSONA_ENT_IDS)
        if len(logic_ids) >= 2 and len(ent_ids) >= 1:
            pick_logic = _rnd.sample(logic_ids, 2)
            pick_ent = _rnd.sample(ent_ids, 1)
            display_indices = pick_logic + pick_ent
            _rnd.shuffle(display_indices)
        else:
            display_indices = list(range(min(3, len(PERSONAS))))
        display_personas = [PERSONAS[i] for i in display_indices]
        personas_data = [all_personas_data[i] if i < len(all_personas_data) else "" for i in display_indices]
        display_persona_ids = display_indices
    quick_understand = cached.get("quick_understand") if cached else None
    vote_data = cached.get("vote_data") if cached else None
    body_html = _blocks_to_html(blocks) if blocks else ""
    short_summary = _build_short_summary(blocks, item.summary)
    meta_desc = _meta_description_qa(item.title, item.summary)

    all_news = NewsAggregator.get_news()
    next_article = prev_article = None
    for i, a in enumerate(all_news):
        if a.id == topic_id:
            if i + 1 < len(all_news):
                next_article = all_news[i + 1]
            if i > 0:
                prev_article = all_news[i - 1]
            break

    related = [a for a in all_news if a.category == item.category and a.id != topic_id][:4]
    import random as _rnd
    other_cat = [a for a in all_news if a.category != item.category and a.id != topic_id]
    ai_recommended = _rnd.sample(other_cat, min(3, len(other_cat))) if other_cat else []

    return templates.TemplateResponse(
        "article.html",
        {
            "request": request,
            "article": item,
            "image_url": image_url,
            "personas": display_personas,
            "display_persona_ids": display_persona_ids,
            "all_personas": PERSONAS,
            "site_url": site_url,
            "article_url": article_url,
            "og_image": og_image,
            "blocks": blocks,
            "personas_data": personas_data,
            "body_html": body_html,
            "short_summary": short_summary,
            "meta_description": meta_desc,
            "next_article": next_article,
            "prev_article": prev_article,
            "related_articles": related,
            "same_category_articles": related,
            "ai_recommended": ai_recommended,
            "quick_understand": quick_understand,
            "vote_data": vote_data,
        }
    )


@router.get("/article/{article_id}", response_class=HTMLResponse)
async def article_detail(request: Request, article_id: str):
    """旧URL: /topic/ へリダイレクト"""
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/topic/{article_id}", status_code=301)


def _sanitize_blocks(blocks: list) -> list:
    """ブロックのcontentからHTML断片を除去（キャッシュ済み悪データ対策）"""
    out = []
    for b in (blocks or []):
        if isinstance(b, dict) and "content" in b and b.get("content"):
            b = {**b, "content": sanitize_display_text(str(b["content"]))}
        out.append(b)
    return out


@router.get("/api/article/{article_id}/explain")
async def api_explain_article(article_id: str):
    """記事のAI解説を取得（従来形式・サイドパネル用）"""
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    content = f"{item.title}\n\n{item.summary}"
    explanation = explain_article_with_ai(item.title, content)
    return {"explanation": explanation}


@router.get("/api/article/{article_id}/explain-inline")
async def api_explain_inline(article_id: str):
    """記事本文と解説が交互に入った構造で取得（従来API・互換用）"""
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    data = generate_all_explanations(article_id, item.title, f"{item.title}\n\n{item.summary}")
    return {"blocks": _sanitize_blocks(data["blocks"])}


@router.get("/api/article/{article_id}/explanations")
async def api_all_explanations(article_id: str):
    """ミドルマン解説＋人格の意見を一括取得（キャッシュ優先・表示用3人分のみ生成）"""
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    content = f"{item.title}\n\n{item.summary}"
    data = generate_all_explanations(article_id, item.title, content)
    # 新形式は3人分のみ→フロント互換のため14スロットで返す（該当3件のみ埋める）
    if data.get("display_persona_ids") is not None and len(data.get("personas", [])) == 3:
        full_personas = [""] * len(PERSONAS)
        for i, pid in enumerate(data["display_persona_ids"]):
            if 0 <= pid < len(full_personas):
                full_personas[pid] = data["personas"][i]
        return {"blocks": _sanitize_blocks(data["blocks"]), "personas": full_personas}
    return {"blocks": _sanitize_blocks(data["blocks"]), "personas": data["personas"]}


@router.get("/api/article/{article_id}/opinion/{persona_id}")
async def api_persona_opinion(article_id: str, persona_id: int):
    """表示用3人のうち1人の意見を取得（キャッシュ経由。当該記事で選ばれていない人格は空）"""
    if persona_id < 0 or persona_id >= len(PERSONAS):
        raise HTTPException(status_code=404, detail="人格が見つかりません")
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    data = generate_all_explanations(article_id, item.title, f"{item.title}\n\n{item.summary}")
    if data.get("display_persona_ids") is not None and persona_id in data["display_persona_ids"]:
        idx = data["display_persona_ids"].index(persona_id)
        opinion = data["personas"][idx] if idx < len(data["personas"]) else ""
    else:
        opinion = data["personas"][persona_id] if persona_id < len(data["personas"]) else ""
    return {"persona": PERSONAS[persona_id], "opinion": opinion}


@router.get("/api/status")
async def api_status():
    """状態確認（記事数・DBパス等）。一覧は NewsAggregator キャッシュ利用で Firestore 読取を抑える"""
    from app.services.explanation_cache import get_cached_article_ids

    displayable = NewsAggregator.get_news()
    processed = get_cached_article_ids()
    try:
        from app.config import settings
        has_key = bool(getattr(settings, "OPENAI_API_KEY", ""))
    except Exception:
        has_key = False

    return {
        "articles_in_db": len(displayable),
        "ai_processed": len(processed),
        "displayable": len(displayable),
        "openai_key_set": has_key,
    }


@router.get("/api/news/refresh")
async def api_refresh_news():
    """ニュース・トレンドを手動更新（バックグラウンドで実行、即応答）"""
    import threading

    def _refresh():
        NewsAggregator.get_trends(force_refresh=True)  # トレンドは速い
        if not is_rss_and_ai_disabled():
            NewsAggregator.get_news(force_refresh=True)  # 記事はAI処理で遅い（無効時はスキップ）

    threading.Thread(target=_refresh, daemon=True).start()
    msg = "更新を開始しました" if not is_rss_and_ai_disabled() else "トレンドのみ更新しました（RSS・AIはこの環境では無効です）"
    return {"status": "ok", "message": msg}


def _is_admin(request: Request, x_admin_secret: str | None = Header(None, alias="X-Admin-Secret")) -> bool:
    """セッションまたは X-Admin-Secret ヘッダで管理者か判定"""
    if not getattr(settings, "ADMIN_SECRET", ""):
        return False
    if x_admin_secret and x_admin_secret.strip() == settings.ADMIN_SECRET:
        return True
    return request.session.get("admin") is True


@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    """管理者ログインフォーム表示"""
    if not getattr(settings, "ADMIN_SECRET", ""):
        return templates.TemplateResponse("admin_login.html", {"request": request, "error": "管理機能は無効です（ADMIN_SECRET 未設定）"})
    if request.session.get("admin"):
        return RedirectResponse(url="/admin", status_code=302)
    err = "シークレットが正しくありません" if request.query_params.get("error") == "invalid" else None
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": err})


@router.post("/admin/login", response_class=RedirectResponse)
async def admin_login_submit(request: Request, secret: str = Form(...)):
    """管理者ログイン処理"""
    if not getattr(settings, "ADMIN_SECRET", ""):
        raise HTTPException(status_code=403, detail="管理機能は無効です")
    if secret.strip() != settings.ADMIN_SECRET:
        return RedirectResponse(url="/admin/login?error=invalid", status_code=302)
    request.session["admin"] = True
    return RedirectResponse(url="/admin", status_code=302)


@router.get("/admin/logout", response_class=RedirectResponse)
async def admin_logout(request: Request):
    """管理者ログアウト"""
    request.session.pop("admin", None)
    return RedirectResponse(url="/", status_code=302)


@router.get("/admin", response_class=HTMLResponse)
async def admin_manual_article_page(request: Request):
    """手動記事追加フォーム（管理者のみ）"""
    if not getattr(settings, "ADMIN_SECRET", ""):
        raise HTTPException(status_code=403, detail="管理機能は無効です")
    if not request.session.get("admin"):
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse("admin_manual_article.html", {"request": request})


def _do_create_manual_article_sync(title: str, summary: str, link: str = "", source: str = "編集部") -> dict:
    """手動記事をAIで生成して保存（同期・スレッド実行用）。generate_all_explanations 内で save_cache 済み"""
    article_id = "manual-" + uuid.uuid4().hex[:16]
    content = sanitize_display_text(f"{title}\n\n{summary}")[:20000]
    data = generate_all_explanations(article_id, title, content)
    blocks = data.get("blocks", [])
    if not blocks:
        return {"status": "error", "article_id": None, "message": "AIによる記事生成に失敗しました"}
    item = NewsItem(
        id=article_id,
        title=title,
        link=link or "#",
        summary=summary[:4000],
        published=datetime.now(),
        source=source or "編集部",
        category="総合",
        image_url=None,
    )
    if not save_article(item):
        return {"status": "error", "article_id": None, "message": "記事の保存に失敗しました"}
    NewsAggregator.get_news(force_refresh=not is_rss_and_ai_disabled())
    return {"status": "ok", "article_id": article_id}


@router.post("/api/admin/article/manual")
async def api_admin_article_manual(
    request: Request,
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
):
    """手動で概要を送り、AIが理解ナビゲーター形式の記事を生成して追加（管理者のみ）"""
    if not _is_admin(request, x_admin_secret):
        raise HTTPException(status_code=403, detail="管理者のみ利用できます")
    if is_rss_and_ai_disabled():
        raise HTTPException(status_code=503, detail="この環境ではRSS取得・AI要約は無効です。手動記事追加はローカル等で実行してください。")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON で title, summary を送ってください")
    title = (body.get("title") or "").strip()
    summary = (body.get("summary") or "").strip()
    if not title or not summary:
        raise HTTPException(status_code=400, detail="タイトルと概要は必須です")
    link = (body.get("link") or "").strip()
    source = (body.get("source") or "編集部").strip() or "編集部"
    import asyncio
    loop = asyncio.get_event_loop()
    result = await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: _do_create_manual_article_sync(title, summary, link, source),
        ),
        timeout=180.0,
    )
    return result


@router.post("/api/admin/article/{article_id}/clear-cache")
async def api_clear_article_cache(article_id: str):
    """記事の解説キャッシュを削除（再生成させる）"""
    from app.services.explanation_cache import delete_cache
    deleted = delete_cache(article_id)
    return {"status": "ok", "deleted": deleted, "message": "キャッシュを削除しました。次回アクセスで再生成されます。"}


@router.post("/api/admin/article/{article_id}/delete")
async def api_delete_article(article_id: str):
    """記事を完全に削除（解説キャッシュ＋記事DBから削除）"""
    from app.services.explanation_cache import delete_cache
    from app.services.article_cache import delete_article as delete_article_from_db
    deleted_cache = delete_cache(article_id)
    deleted_article = delete_article_from_db(article_id)
    NewsAggregator.get_news(force_refresh=not is_rss_and_ai_disabled())
    return {
        "status": "ok",
        "deleted": deleted_cache or deleted_article,
        "message": "記事を削除しました。",
    }


@router.get("/api/admin/seed-articles")
async def api_seed_articles():
    """RSSからミドルマンAI解説付きで記事を投入（新着5件）"""
    if is_rss_and_ai_disabled():
        raise HTTPException(status_code=503, detail="この環境ではRSS取得・AI要約は無効です。ローカル等で実行してください。")
    from app.services.rss_service import fetch_rss_news
    from app.services.article_processor import process_new_rss_articles
    from app.services.explanation_cache import get_cached_article_ids

    news = fetch_rss_news()
    added = process_new_rss_articles(news, max_per_run=5)
    NewsAggregator.get_news(force_refresh=True)
    total = len(get_cached_article_ids())
    return {"status": "ok", "added": added, "total": total}


@router.post("/api/admin/sync-meta")
async def api_admin_sync_meta(
    request: Request,
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
):
    """
    Firestore の _meta/cache（表示対象の記事ID一覧）を explanations コレクションと同期する。
    「記事は8件あるが表示は3件」のとき、explanations に8件あれば同期後に8件表示される。
    管理者のみ。実行後に一覧キャッシュを強制更新する。
    """
    if not _is_admin(request, x_admin_secret):
        raise HTTPException(status_code=403, detail="管理者のみ利用できます")
    try:
        from app.services.firestore_store import use_firestore, firestore_sync_meta_from_explanations
        from app.services.explanation_cache import invalidate_ids_cache
    except ImportError:
        raise HTTPException(status_code=501, detail="Firestore 未使用のためこのAPIは利用できません")
    if not use_firestore():
        return {"status": "ok", "synced": 0, "message": "Firestore 未使用のためスキップしました"}
    synced = firestore_sync_meta_from_explanations()
    invalidate_ids_cache()
    NewsAggregator.get_news(force_refresh=True)
    return {"status": "ok", "synced": synced}


def _do_seed_one_sync():
    """RSS取得→1件だけ記事化（重い処理を同期的に実行・スレッドから呼ぶ用）"""
    from app.services.rss_service import fetch_rss_news
    from app.services.article_processor import process_new_rss_articles
    news = fetch_rss_news()
    if not news:
        return {"status": "error", "article_id": None, "message": "RSSから記事を取得できませんでした。フィードURLやネットワークを確認してください。"}
    added = process_new_rss_articles(news, max_per_run=1)
    if added <= 0:
        return {"status": "none", "article_id": None, "message": "取り込める記事がありません"}
    NewsAggregator.get_news(force_refresh=True)
    updated = NewsAggregator.get_news()
    new_id = updated[0].id if updated else None
    return {"status": "ok", "article_id": new_id}


@router.get("/api/article/seed-one")
async def api_seed_one_article():
    """RSSから1件読み込み、AI解説付きで記事を1件作る。作成した記事IDを返す（常にJSONで返す）"""
    if is_rss_and_ai_disabled():
        raise HTTPException(status_code=503, detail="この環境ではRSS取得・AI要約は無効です。")
    import asyncio
    import logging
    logger = logging.getLogger(__name__)
    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _do_seed_one_sync),
            timeout=180.0,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning("seed-one がタイムアウトしました")
        return {"status": "error", "article_id": None, "message": "処理がタイムアウトしました（3分）。RSSやOpenAIの応答が遅い可能性があります。もう一度お試しください。"}
    except Exception as e:
        logger.exception("seed-one でエラー")
        msg = str(e).strip() or "不明なエラー"
        return {"status": "error", "article_id": None, "message": f"処理に失敗しました: {msg}"}


def _do_force_add_one_sync():
    """RSS取得＋トレンド精査で1件選定＋AI処理（重い処理を同期的に実行・スレッドから呼ぶ用）"""
    from app.services.rss_service import fetch_rss_news
    from app.services.article_processor import process_rss_to_site_article, _rank_by_trending
    from app.services.explanation_cache import delete_cache
    from app.services.trends_service import fetch_trending_searches
    news = fetch_rss_news()
    if not news:
        return {"status": "error", "article_id": None, "message": "RSSから記事を取得できませんでした。フィードURLやネットワークを確認してください。"}
    trends = fetch_trending_searches()
    trend_keywords = [t.keyword for t in trends]
    ranked = _rank_by_trending(news, trend_keywords) if trend_keywords else news
    item = ranked[0]
    delete_cache(item.id)
    if process_rss_to_site_article(item, force=True):
        NewsAggregator.get_news(force_refresh=True)
        if NewsAggregator.get_article(item.id) is None:
            return {"status": "error", "article_id": None, "message": "記事の保存後に取得できませんでした。data フォルダの権限やDBを確認してください。"}
        return {"status": "ok", "article_id": item.id}
    return {"status": "error", "article_id": None, "message": "AI解説の生成に失敗しました。.env の OPENAI_API_KEY を確認し、利用可能なモデル（OPENAI_MODEL）を指定してください。"}


@router.get("/api/article/force-add-one")
async def api_force_add_one_article():
    """RSSの先頭1件を必ず1件取り込む。重い処理はスレッドで実行して固まらないようにする。"""
    if is_rss_and_ai_disabled():
        raise HTTPException(status_code=503, detail="この環境ではRSS取得・AI要約は無効です。")
    import asyncio
    import logging
    logger = logging.getLogger(__name__)
    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _do_force_add_one_sync),
            timeout=180.0,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning("force-add-one がタイムアウトしました")
        return {"status": "error", "article_id": None, "message": "処理がタイムアウトしました（3分）。RSSやOpenAIの応答が遅い可能性があります。もう一度お試しください。"}
    except Exception as e:
        logger.exception("force-add-one でエラー")
        msg = str(e).strip() or "不明なエラー"
        return {"status": "error", "article_id": None, "message": f"処理に失敗しました: {msg}"}
