"""ニュース関連ルート"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, Form, Header
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import settings
from app.services.news_aggregator import NewsAggregator
from app.services.rss_service import NewsItem, sanitize_display_text
from app.services.article_cache import save_article
from app.services.ai_batch_service import generate_all_explanations
from app.services.ai_service import (
    explain_article_with_ai,
    get_image_url,
    PERSONAS,
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
    """SEO用 sitemap.xml"""
    from app.services.article_cache import load_all
    from app.services.explanation_cache import get_cached_article_ids

    site_url = _get_site_url(request)
    all_articles = load_all()
    processed = get_cached_article_ids()
    articles = [a for a in all_articles if a.id in processed]
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
async def index(request: Request, page: int = 1):
    """トップページ（ジャンル別表示・ページネーション対応）"""
    news_by_category, pagination = NewsAggregator.get_news_by_category(page=page)
    trends = NewsAggregator.get_trends()
    added_one = None
    if page == 1:
        all_news = NewsAggregator.get_news()
        if all_news:
            added_one = all_news[0]
            if added_one:
                if not added_one.image_url:
                    added_one.image_url = get_image_url(added_one.id, 400, 225)
                elif not added_one.image_url.startswith("http"):
                    added_one.image_url = get_image_url(added_one.image_url, 400, 225)
    for _, items in news_by_category:
        for item in items:
            if not item.image_url:
                item.image_url = get_image_url(item.id, 400, 225)
            elif item.image_url and not item.image_url.startswith("http"):
                item.image_url = get_image_url(item.image_url, 400, 225)
    site_url = _get_site_url(request)
    og_image = (added_one.image_url or "https://picsum.photos/1200/630") if added_one else "https://picsum.photos/1200/630"
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
        }
    )


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


def _get_site_url(request: Request) -> str:
    """サイトの絶対URL（末尾スラッシュなし）"""
    base = getattr(settings, "SITE_URL", "").strip().rstrip("/")
    if base:
        return base
    return str(request.base_url).rstrip("/")


def _meta_description_qa(title: str, summary: str | None, max_len: int = 160) -> str:
    """質問＋解答型のmeta description（SEO向け）"""
    t = (title or "").strip()
    s = (summary or "").replace("\n", " ").strip()[:200]
    if not t:
        return (s[: max_len - 3] + "...") if len(s) > max_len else s
    question = f"{t}とは？"
    if not s:
        return question[:max_len]
    answer = s[: max_len - len(question) - 4] + "..." if len(s) > max_len - len(question) - 2 else s
    return f"{question} {answer}"[:max_len]


def _blocks_to_html(blocks: list) -> str:
    """ブロックをHTMLに変換（SSR用・XSS対策済み）"""
    if not blocks:
        return ""
    import html
    out = []
    is_navigator = blocks and blocks[0].get("type") == "navigator_section"
    nav_labels = {"facts": "ニュース", "background": "背景", "impact": "影響範囲", "prediction": "予測", "caution": "注意"}
    if is_navigator:
        paras, asides = [], []
        for b in blocks:
            if b.get("type") != "navigator_section" or not b.get("section"):
                continue
            body = (b.get("content") or "").strip()
            body_safe = html.escape(body).replace("\n", "<br>") if body else ""
            if b.get("section") == "facts" and body:
                paras = body.split("\n\n")
                paras = [p.strip() for p in paras if p.strip()]
            elif b.get("section") != "facts" and body:
                asides.append({"section": b["section"], "label": nav_labels.get(b["section"], b["section"]), "body": body_safe})
        for i, p in enumerate(paras):
            out.append(f'<p class="article-text">{html.escape(p).replace("\n", "<br>")}</p>')
            if i < len(asides):
                a = asides[i]
                out.append(f'<div class="scroll-bubble-group"><div class="midorman-bubble-wrap is-visible"><div class="midorman-aside midorman-aside-{html.escape(a["section"])}"><span class="midorman-aside-label">{html.escape(a["label"])}</span><div class="midorman-aside-body">{a["body"]}</div></div></div></div>')
        for j in range(len(paras), len(asides)):
            a = asides[j]
            out.append(f'<div class="scroll-bubble-group"><div class="midorman-bubble-wrap is-visible"><div class="midorman-aside midorman-aside-{html.escape(a["section"])}"><span class="midorman-aside-label">{html.escape(a["label"])}</span><div class="midorman-aside-body">{a["body"]}</div></div></div></div>')
        return '<div class="article-readflow">' + "".join(out) + "</div>" if out else ""
    # text/explain 形式（H2/H3 疑問型＋回答型でSEO・ミドルマン解説も本文に残す）
    html_parts = ['<div class="article-readflow article-with-bubbles">']
    explain_index = 0
    for b in blocks:
        if b.get("type") == "explain":
            explain_index += 1
            c = html.escape(b.get("content") or "").replace("\n", "<br>")
            h3_id = f"explain-{explain_index}"
            html_parts.append(f'<h3 class="article-h3" id="{h3_id}">要点の解説</h3>')
            bubble = f'<div class="midorman-bubble-above"><span class="midorman-bubble-avatar" aria-hidden="true">🎙️</span><div class="midorman-bubble-inner"><p class="midorman-bubble-text">{c}</p></div></div>'
            html_parts.append(f'<div class="scroll-bubble-group"><div class="scroll-trigger" aria-hidden="true"></div><div class="midorman-bubble-wrap">{bubble}</div></div>')
        elif b.get("type") == "text":
            for p in (b.get("content") or "").strip().split("\n\n"):
                p = p.strip()
                if p:
                    html_parts.append(f'<p class="article-text">{html.escape(p).replace("\n", "<br>")}</p>')
    html_parts.append("</div>")
    return "".join(html_parts)


@router.get("/topic/{topic_id}", response_class=HTMLResponse)
async def topic_detail(request: Request, topic_id: str):
    """トピック詳細（URL: /topic/○○）・AI解説・SEO向け本文"""
    from app.services.explanation_cache import get_cached

    item = NewsAggregator.get_article(topic_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
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
    personas_data = cached.get("personas", []) if cached else []
    body_html = _blocks_to_html(blocks) if blocks else ""
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

    return templates.TemplateResponse(
        "article.html",
        {
            "request": request,
            "article": item,
            "image_url": image_url,
            "personas": PERSONAS,
            "site_url": site_url,
            "article_url": article_url,
            "og_image": og_image,
            "blocks": blocks,
            "personas_data": personas_data,
            "body_html": body_html,
            "meta_description": meta_desc,
            "next_article": next_article,
            "prev_article": prev_article,
            "related_articles": related,
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
    """ミドルマン解説＋5人格の意見を一括取得（キャッシュ優先・一括生成）"""
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    content = f"{item.title}\n\n{item.summary}"
    data = generate_all_explanations(article_id, item.title, content)
    return {"blocks": _sanitize_blocks(data["blocks"]), "personas": data["personas"]}


@router.get("/api/article/{article_id}/opinion/{persona_id}")
async def api_persona_opinion(article_id: str, persona_id: int):
    """5人格のAIのうち1人の意見を取得（従来API・キャッシュ経由）"""
    if persona_id < 0 or persona_id >= len(PERSONAS):
        raise HTTPException(status_code=404, detail="人格が見つかりません")
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    data = generate_all_explanations(article_id, item.title, f"{item.title}\n\n{item.summary}")
    opinion = data["personas"][persona_id] if persona_id < len(data["personas"]) else ""
    return {"persona": PERSONAS[persona_id], "opinion": opinion}


@router.get("/api/status")
async def api_status():
    """状態確認（記事数・DBパス等）"""
    from app.services.explanation_cache import get_cached_article_ids
    from app.services.article_cache import load_all

    processed = get_cached_article_ids()
    all_arts = load_all()
    displayable = [a for a in all_arts if a.id in processed]
    try:
        from app.config import settings
        has_key = bool(getattr(settings, "OPENAI_API_KEY", ""))
    except Exception:
        has_key = False

    return {
        "articles_in_db": len(all_arts),
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
        NewsAggregator.get_news(force_refresh=True)   # 記事はAI処理で遅い

    threading.Thread(target=_refresh, daemon=True).start()
    return {"status": "ok", "message": "更新を開始しました"}


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
    NewsAggregator.get_news(force_refresh=True)
    return {"status": "ok", "article_id": article_id}


@router.post("/api/admin/article/manual")
async def api_admin_article_manual(
    request: Request,
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
):
    """手動で概要を送り、AIが理解ナビゲーター形式の記事を生成して追加（管理者のみ）"""
    if not _is_admin(request, x_admin_secret):
        raise HTTPException(status_code=403, detail="管理者のみ利用できます")
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
    NewsAggregator.get_news(force_refresh=True)
    return {
        "status": "ok",
        "deleted": deleted_cache or deleted_article,
        "message": "記事を削除しました。",
    }


@router.get("/api/admin/seed-articles")
async def api_seed_articles():
    """RSSからミドルマンAI解説付きで記事を投入（新着5件）"""
    from app.services.rss_service import fetch_rss_news
    from app.services.article_processor import process_new_rss_articles
    from app.services.explanation_cache import get_cached_article_ids

    news = fetch_rss_news()
    added = process_new_rss_articles(news, max_per_run=5)
    NewsAggregator.get_news(force_refresh=True)
    total = len(get_cached_article_ids())
    return {"status": "ok", "added": added, "total": total}


@router.get("/api/article/seed-one")
async def api_seed_one_article():
    """RSSから1件読み込み、AI解説付きで記事を1件作る。作成した記事IDを返す"""
    from app.services.rss_service import fetch_rss_news
    from app.services.article_processor import process_new_rss_articles

    news = fetch_rss_news()
    added = process_new_rss_articles(news, max_per_run=1)
    if added <= 0:
        return {"status": "none", "article_id": None, "message": "取り込める記事がありません"}
    NewsAggregator.get_news(force_refresh=True)
    # 先頭＝今追加した1件
    updated = NewsAggregator.get_news()
    new_id = updated[0].id if updated else None
    return {"status": "ok", "article_id": new_id}


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
