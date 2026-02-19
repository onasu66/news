"""ニュース関連ルート"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, Form, Header
from fastapi.responses import HTMLResponse, RedirectResponse
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
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "news_by_category": news_by_category,
            "trends": trends,
            "pagination": pagination,
            "added_one": added_one,
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


@router.get("/article/{article_id}", response_class=HTMLResponse)
async def article_detail(request: Request, article_id: str):
    """記事詳細（AI解説付き・5人格の意見）"""
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    image_url = item.image_url or get_image_url(item.id, 800, 450)
    if image_url and not image_url.startswith("http"):
        image_url = get_image_url(image_url, 800, 450)
    return templates.TemplateResponse(
        "article.html",
        {
            "request": request,
            "article": item,
            "image_url": image_url,
            "personas": PERSONAS,
        }
    )


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
