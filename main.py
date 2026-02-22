"""ニュースサイト - FastAPI メインアプリケーション"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler

from app.routers import news
from app.services.news_aggregator import NewsAggregator

try:
    from app.config import settings
    INTERVAL_MIN = settings.NEWS_REFRESH_INTERVAL
except Exception:
    INTERVAL_MIN = 240


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時にスケジューラ開始"""
    import threading
    from app.services.rss_service import fetch_rss_news
    from app.services.explanation_cache import get_cached_article_ids
    from app.services.article_processor import process_new_rss_articles

    cached_ids = get_cached_article_ids()
    if len(cached_ids) < 20:
        news_list = fetch_rss_news()
        process_new_rss_articles(news_list, max_per_run=5)

    def _init():
        NewsAggregator.get_news(force_refresh=True)
        NewsAggregator.get_trends(force_refresh=True)
    t = threading.Thread(target=_init, daemon=True)
    t.start()

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: NewsAggregator.get_news(force_refresh=True),
        "interval",
        minutes=INTERVAL_MIN,
        id="refresh_news",
    )
    scheduler.add_job(
        lambda: NewsAggregator.get_trends(force_refresh=True),
        "interval",
        minutes=INTERVAL_MIN,
        id="refresh_trends",
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(
    title="知リポAI",
    description="最新ニュースをAIが解説する知的ニュースレポート",
    lifespan=lifespan,
)

try:
    from app.config import settings
    from starlette.middleware.sessions import SessionMiddleware
    _secret = getattr(settings, "ADMIN_SECRET", "") or "dev-secret-change-me"
    app.add_middleware(SessionMiddleware, secret_key=_secret, session_cookie="newsite_admin")
except Exception:
    pass

static_path = Path(__file__).resolve().parent / "app" / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

app.include_router(news.router)


def _get_routes_info(app_obj: FastAPI) -> list:
    out = []
    for r in app_obj.routes:
        if hasattr(r, "path"):
            methods = getattr(r, "methods", None)
            out.append({"path": r.path, "methods": list(methods) if methods else []})
    return out


@app.get("/debug", response_class=HTMLResponse)
async def debug_page():
    import os
    base_dir = Path(__file__).resolve().parent
    data_dir = base_dir / "data"
    routes = _get_routes_info(app)
    routes_html = "".join(
        f"<li><code>{r['path']}</code> {r['methods']}</li>" for r in sorted(routes, key=lambda x: x["path"])
    )
    articles_db = data_dir / "articles.db"
    explanations_db = data_dir / "explanations.db"
    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8"><title>デバッグ</title></head>
<body style="font-family: sans-serif; padding: 1.5rem; max-width: 720px;">
<h1>デバッグ情報（ニュースサイト）</h1>
<p><strong>このページが表示されていれば、ニュースサイト（newsite）で起動しています。</strong></p>
<p style="background:#fff3cd; padding:0.5rem;">※ アドレスバーが <strong>http://localhost:8001/...</strong> になっているか確認してください。</p>
<hr>
<h2>アプリ</h2>
<ul>
<li>アプリ名: 知リポAI（newsite）</li>
<li>ベースディレクトリ: <code>{base_dir}</code></li>
<li>データフォルダ: <code>{data_dir}</code></li>
<li>articles.db 存在: {articles_db.exists()}</li>
<li>explanations.db 存在: {explanations_db.exists()}</li>
</ul>
<h2>登録されているルート</h2>
<ul>{routes_html}</ul>
<h2>リンク</h2>
<ul>
<li><a href="/">トップ</a></li>
<li><a href="/confirm">確認用</a></li>
<li><a href="/admin/login">ログイン</a></li>
</ul>
<h2>同じWi‑Fi内から</h2>
<p>サーバーは <code>0.0.0.0:8001</code> で待ち受けています。同じWi‑Fi／LAN内のスマホや別PCから、<br>
<strong>http://&lt;このPCのIPアドレス&gt;:8001</strong> で開けます。</p>
<h2>Wi‑Fiの外（インターネット）から開く</h2>
<p>ngrok などのトンネルを使います。プロジェクト内の <strong>WiFi外で開く.md</strong> を参照してください。</p>
<hr>
<p style="color:#666;">このアプリは <strong>port 8001</strong> で起動します。</p>
</body></html>"""
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
