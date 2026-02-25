"""ニュースサイト - FastAPI メインアプリケーション"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI

# 起動時メモリログ等を Render/ローカルで見るため
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")
logger = logging.getLogger(__name__)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.routers import news
from app.services.news_aggregator import NewsAggregator

try:
    from app.config import settings
    INTERVAL_MIN = settings.NEWS_REFRESH_INTERVAL
except Exception:
    INTERVAL_MIN = 240

JST = ZoneInfo("Asia/Tokyo")


def _scheduled_rss_fetch_and_article():
    """指定時刻にRSS取得→記事化（9:30/12:30/20:00/0:00 JST で実行）"""
    NewsAggregator.get_news(force_refresh=True)


def _seed_if_needed():
    """キャッシュが少ないときだけRSS取得→記事化（重いのでバックグラウンドで実行）"""
    from app.services.explanation_cache import get_cached_article_ids
    from app.services.rss_service import fetch_rss_news
    from app.services.article_processor import process_new_rss_articles
    cached_ids = get_cached_article_ids()
    if len(cached_ids) < 20:
        news_list = fetch_rss_news()
        if news_list:
            process_new_rss_articles(news_list, max_per_run=5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時にスケジューラ開始。Firebase 認証がある場合は起動時に Firestore を初期化（Render で SQLite に落ちないようにする）。"""
    import threading
    # Firebase 認証が設定されていれば起動時に Firestore を import して接続を確定（Render で記事が消えないようにする）
    try:
        from app.config import settings
        if getattr(settings, "FIREBASE_SERVICE_ACCOUNT_JSON", "").strip():
            from app.services.firestore_store import use_firestore, _get_client
            if use_firestore():
                _get_client()
                logger.info("ストレージ: Firestore を使用します（起動時に接続済み）")
            else:
                logger.warning("FIREBASE_SERVICE_ACCOUNT_JSON は設定されていますが Firestore が有効になりませんでした（firebase-admin 未インストールまたは JSON 不正の可能性）。SQLite を使用します。")
    except Exception as e:
        logger.warning("Firestore 起動時チェックでエラー: %s", e)
    # 初回シードはブロックせずバックグラウンドで実行（RSS+AIで数分かかるため）
    t_seed = threading.Thread(target=_seed_if_needed, daemon=True)
    t_seed.start()

    def _init():
        NewsAggregator.get_news(force_refresh=True)
        NewsAggregator.get_trends(force_refresh=True)
    t = threading.Thread(target=_init, daemon=True)
    t.start()

    scheduler = BackgroundScheduler(timezone=JST)
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
    # 朝9:30・昼12:30・夜20:00・夜中0:00（JST）にRSS取得→記事化
    for job_id, hour, minute in [
        ("rss_00", 0, 0),
        ("rss_0930", 9, 30),
        ("rss_1230", 12, 30),
        ("rss_2000", 20, 0),
    ]:
        scheduler.add_job(
            _scheduled_rss_fetch_and_article,
            CronTrigger(hour=hour, minute=minute, timezone=JST),
            id=job_id,
        )
    scheduler.start()
    # 起動直後のメモリをログ（Render 512MB 制限の確認用）
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss_kb = usage.ru_maxrss  # Linux では KB
        rss_mb = rss_kb / 1024
        pct = (rss_mb / 512) * 100
        logger.info("起動時メモリ: 約 %.1f MB (512MB の %.0f%%)", rss_mb, pct)
    except Exception:
        pass
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


@app.get("/api/debug/storage")
async def debug_storage():
    """Firebase/Firestore が有効か確認。認証設定の診断用。"""
    try:
        from app.services.firestore_store import use_firestore, _load_credential_dict, _FIREBASE_JSON, _CREDENTIALS_PATH
        cred_dict = _load_credential_dict()
        credentials_set = bool(_FIREBASE_JSON or _CREDENTIALS_PATH.exists())
        credentials_valid = cred_dict is not None
        try:
            import firebase_admin  # noqa: F401
            firebase_available = True
        except ModuleNotFoundError:
            firebase_available = False
        use_firestore_result = use_firestore()
        storage = "firestore" if use_firestore_result else "sqlite"
        if not use_firestore_result and credentials_set:
            if not credentials_valid:
                msg = "FIREBASE_SERVICE_ACCOUNT_JSON が不正です。JSON 形式を確認してください。"
            elif not firebase_available:
                msg = "firebase-admin がインストールされていません。pip install firebase-admin を実行してください。"
            else:
                msg = "Firestore が無効です。上記を確認してください。"
        else:
            msg = "Firestore を使用しています。" if use_firestore_result else "認証が未設定のため SQLite を使用しています。"
        return {
            "storage": storage,
            "credentials_set": credentials_set,
            "credentials_valid_json": credentials_valid,
            "firebase_admin_available": firebase_available,
            "message": msg,
        }
    except Exception as e:
        return {
            "storage": "sqlite",
            "credentials_set": False,
            "credentials_valid_json": False,
            "firebase_admin_available": False,
            "message": "確認中にエラー: " + str(e),
        }


@app.get("/api/debug/articles-status")
async def debug_articles_status():
    """記事が表示されない原因の確認用。保存記事数・AI解説済み数・表示対象数を返す。"""
    from app.services.article_cache import load_all
    from app.services.explanation_cache import get_cached_article_ids
    try:
        from app.services.firestore_store import use_firestore
        storage = "firestore" if use_firestore() else "sqlite"
    except Exception:
        storage = "sqlite"
    all_articles = load_all()
    processed_ids = get_cached_article_ids()
    displayable = [a for a in all_articles if a.id in processed_ids]
    return {
        "storage": storage,
        "articles_total": len(all_articles),
        "with_ai_explanation": len(processed_ids),
        "displayable": len(displayable),
        "message": "表示されるのは「記事が保存されている」かつ「AI解説済み」のものだけです。",
    }


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
<li><a href="/api/debug/articles-status" target="_blank">記事ステータス（なぜ出ないか確認）</a></li>
<li><a href="/api/debug/storage" target="_blank">ストレージ確認（Firebase / SQLite どちらか）</a></li>
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
