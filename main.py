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
    from app.config import settings, is_rss_and_ai_disabled
    INTERVAL_MIN = settings.NEWS_REFRESH_INTERVAL
    # 一覧キャッシュ同期間隔（分）: デフォルト60分
    LIST_CACHE_SYNC_MIN = max(60, int(getattr(settings, "NEWS_LIST_CACHE_SYNC_MINUTES", 60)))
    _SEED_MAX_PER_RUN = max(7, min(14, int(getattr(settings, "RSS_PROCESS_MAX_PER_BATCH", 22))))
except Exception:
    INTERVAL_MIN = 240
    LIST_CACHE_SYNC_MIN = 60
    _SEED_MAX_PER_RUN = 12
    is_rss_and_ai_disabled = lambda: False

JST = ZoneInfo("Asia/Tokyo")


def _scheduled_rss_fetch_and_article():
    """指定時刻: RSS取得→新しい記事だけ良い記事を記事化（0:00/9:30/12:30）"""
    NewsAggregator.get_news(force_refresh=True)


def _scheduled_2000_rss_and_ai_daily():
    """20:00 JST: (1) RSS取得→記事化 (2) その後にAI日次コンテンツを1日1回だけ更新"""
    NewsAggregator.get_news(force_refresh=True)
    try:
        from app.services.ai_daily import generate_daily_ai_content
        generate_daily_ai_content()
    except Exception as e:
        logger.warning("AI日次コンテンツ生成に失敗: %s", e)


def _scheduled_claude_research_and_seed():
    """Claude Code CLI でウェブリサーチ → curated_articles.json 更新 → 記事化パイプラインに投入。
    claude CLI がない環境（Render 本番など）では自動スキップする。"""
    try:
        from app.services.claude_researcher import is_claude_available, run_claude_research
        if not is_claude_available():
            logger.debug("claude CLI が未インストールのためリサーチをスキップ")
            return
        ok = run_claude_research(n=15, n_news=8, n_papers=7, timeout=900)
        if not ok:
            return
        from app.services.article_seed_from_curated import process_curated_articles
        count = process_curated_articles(max_per_run=30)
        if count > 0:
            NewsAggregator.get_news(force_refresh=True)
            logger.info("Claude リサーチ→記事化完了: %d 件追加", count)
        else:
            logger.info("Claude リサーチ: 新規記事なし（重複または生成失敗）")
    except Exception as e:
        logger.warning("Claude リサーチタスクでエラー: %s", e)


def _scheduled_sync_list_cache_from_db():
    """一覧メモリキャッシュを DB から再構築（再起動なしで新着を表示させる）"""
    try:
        NewsAggregator.sync_list_cache_from_db()
    except Exception as e:
        logger.warning("一覧キャッシュ同期に失敗: %s", e)


def _seed_if_needed():
    """キャッシュが少ないときだけRSS取得→記事化。Firestore 読取削減のため existing_articles を渡す。"""
    from app.services.explanation_cache import get_cached_article_ids
    from app.services.article_cache import load_all
    from app.services.rss_service import fetch_rss_news
    from app.services.article_processor import process_new_rss_articles
    cached_ids = get_cached_article_ids()
    if len(cached_ids) >= 20:
        return
    all_items = load_all()
    news_list = fetch_rss_news()
    if news_list:
        process_new_rss_articles(news_list, max_per_run=_SEED_MAX_PER_RUN, existing_articles=all_items)


def _startup_add_one_each():
    """起動時に日本関連記事1本＋海外記事1本をFirestoreに追加（バックグラウンド実行）。
    1本あたりRSS取得・翻訳・本文取得・解説生成のため目安2〜5分、2本で合計おおよそ4〜10分かかることがあります。"""
    try:
        from app.services.article_processor import process_startup_articles
        from app.services.news_aggregator import NewsAggregator
        added = process_startup_articles(rss_items=None, trend_keywords=None)
        if added > 0:
            NewsAggregator.get_news(force_refresh=True)
            logger.info("起動時記事追加: %d 件（日本1＋海外1）", added)
    except Exception as e:
        logger.warning("起動時記事追加でエラー: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時にスケジューラ開始。Firestore は初回アクセス時に遅延読み込み（512MB 制限で OOM にならないようにする）。"""
    import os
    import threading
    rss_ai_disabled = is_rss_and_ai_disabled()
    if rss_ai_disabled:
        logger.info("RSS取得・AI要約は無効です（DISABLE_RSS_AND_AI=true）。表示はキャッシュのみ。")

    # Render では credentials ファイルがデプロイされないため、Firestore を使うには FIREBASE_SERVICE_ACCOUNT_JSON が必須
    if os.environ.get("RENDER", "").strip().lower() == "true":
        try:
            from app.services.firestore_store import use_firestore
            if use_firestore():
                logger.info("ストレージ: Firestore を使用しています。")
            else:
                logger.error(
                    "Render で Firestore を使うには、ダッシュボードの Environment に "
                    "FIREBASE_SERVICE_ACCOUNT_JSON を設定してください。未設定のため SQLite（空）を使用しており、記事は表示されません。"
                )
        except Exception as e:
            logger.warning("ストレージ確認でエラー: %s", e)

    # Firestore は起動時に import しない（firebase-admin が重く 512MB で OOM になるため）。初回の記事取得時に読み込まれる。
    if not rss_ai_disabled:
        # シードは _init 完了後に実行（同時の load_all で Firestore 読取バーストを防ぐ）
        def _run_seed_delayed():
            import time
            time.sleep(90)
            _seed_if_needed()
        threading.Thread(target=_run_seed_delayed, daemon=True).start()

    def _init():
        # 起動直後は軽い処理だけ先に実行して、API応答を阻害しない
        NewsAggregator.get_trends(force_refresh=True)
    t = threading.Thread(target=_init, daemon=True)
    t.start()

    scheduler = BackgroundScheduler(timezone=JST)
    # 記事一覧は閲覧時TTL破棄をしない運用。RSS 取り込みは cron（force_refresh=True）のみ。
    # 別途 NEWS_LIST_CACHE_SYNC_MINUTES ごとに DB だけ再読みしてメモリ一覧を同期する。
    scheduler.add_job(
        lambda: NewsAggregator.get_trends(force_refresh=True),
        "interval",
        minutes=INTERVAL_MIN,
        id="refresh_trends",
    )
    if LIST_CACHE_SYNC_MIN > 0:
        scheduler.add_job(
            _scheduled_sync_list_cache_from_db,
            "interval",
            minutes=LIST_CACHE_SYNC_MIN,
            id="sync_news_list_cache",
        )
        logger.info("一覧キャッシュを DB から %d 分ごとに同期します。", LIST_CACHE_SYNC_MIN)
    if not rss_ai_disabled:
        # 13:00: RSS取得→記事化のみ
        scheduler.add_job(
            _scheduled_rss_fetch_and_article,
            CronTrigger(hour=13, minute=0, timezone=JST),
            id="rss_1300",
        )
        # 20:00: 記事更新のあと、AI日次コンテンツを1日1回だけ更新
        scheduler.add_job(
            _scheduled_2000_rss_and_ai_daily,
            CronTrigger(hour=20, minute=0, timezone=JST),
            id="rss_2000_and_ai_daily",
        )
        logger.info("RSS記事化: 13:00 / 20:00 JST に設定")
        # Claude ウェブリサーチ: 8:30 / 16:30 / 22:00 の3回
        # claude CLI がない環境（Render 本番）では _scheduled_claude_research_and_seed 内で自動スキップ
        for cr_id, cr_hour, cr_minute in [
            ("claude_research_0830", 8, 30),
            ("claude_research_1630", 16, 30),
            ("claude_research_2200", 22, 0),
        ]:
            scheduler.add_job(
                _scheduled_claude_research_and_seed,
                CronTrigger(hour=cr_hour, minute=cr_minute, timezone=JST),
                id=cr_id,
            )
        logger.info("Claude ウェブリサーチ: 8:30 / 16:30 / 22:00 JST に設定")
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
    import traceback
    from app.services.article_cache import load_all
    from app.services.explanation_cache import get_cached_article_ids
    try:
        from app.services.firestore_store import use_firestore
        storage = "firestore" if use_firestore() else "sqlite"
    except Exception:
        storage = "sqlite"
    all_articles = []
    processed_ids = set()
    load_all_error = None
    get_ids_error = None
    try:
        all_articles = load_all()
    except Exception as e:
        load_all_error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
    try:
        processed_ids = get_cached_article_ids()
    except Exception as e:
        get_ids_error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
    displayable = [a for a in all_articles if a.id in processed_ids]
    return {
        "storage": storage,
        "articles_total": len(all_articles),
        "with_ai_explanation": len(processed_ids),
        "displayable": len(displayable),
        "load_all_error": load_all_error,
        "get_ids_error": get_ids_error,
        "message": "表示されるのは「記事が保存されている」かつ「AI解説済み」のものだけです。",
    }


@app.get("/api/debug/save-history")
async def debug_save_history():
    """記事保存の成功・失敗履歴（起動中のシード／スケジュール分）。新しい順。"""
    from app.services.save_history import get_entries
    return {"entries": get_entries()}


@app.get("/debug/save-history", response_class=HTMLResponse)
async def debug_save_history_page():
    """記事保存履歴をブラウザで確認するページ"""
    import html
    from app.services.save_history import get_entries
    entries = get_entries()
    rows = []
    for e in entries:
        status = "保存OK" if e.get("success") else "保存なし・失敗"
        err = (e.get("error") or "").strip()
        err_safe = html.escape(err) if err else ""
        err_cell = f'<td style="color:#c00; font-size:0.9em;">{err_safe}</td>' if err_safe else "<td></td>"
        title_safe = html.escape(e.get("title", ""))
        rows.append(
            f"<tr><td>{html.escape(e.get('at', ''))}</td><td>{html.escape(e.get('source', ''))}</td>"
            f"<td>{status}</td><td>{html.escape(e.get('article_id', ''))}</td>"
            f"<td style=\"max-width:320px; overflow:hidden; text-overflow:ellipsis;\">{title_safe}</td>{err_cell}</tr>"
        )
    table_body = "\n".join(rows) if rows else "<tr><td colspan=\"5\">まだ履歴がありません。シード実行後やスケジュール実行後に表示されます。</td></tr>"
    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8"><title>記事保存履歴</title></head>
<body style="font-family: sans-serif; padding: 1.5rem; max-width: 960px;">
<h1>記事保存履歴</h1>
<p>python main.py 起動中のシード／スケジュールで「保存できた記事」「保存されなかった記事」を表示します（最大200件・再起動でクリア）。</p>
<p><a href="/debug">デバッグ情報に戻る</a></p>
<table border="1" cellpadding="6" style="border-collapse: collapse; width:100%;">
<thead><tr><th>日時</th><th>種別</th><th>結果</th><th>ID</th><th>タイトル</th><th>エラー等</th></tr></thead>
<tbody>
{table_body}
</tbody>
</table>
</body></html>"""
    return HTMLResponse(html)


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
<li><a href="/debug/save-history">記事保存履歴（保存できた・できなかった）</a></li>
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
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)
