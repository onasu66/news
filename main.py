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
from app.routers import metrics as metrics_router
from app.routers import consultation as consultation_router
from app.services.news_aggregator import NewsAggregator

try:
    from app.config import settings, is_rss_and_ai_disabled
    INTERVAL_MIN = settings.NEWS_REFRESH_INTERVAL
    # 一覧キャッシュ同期間隔（分）。0 で無効化できる（無料枠向け）。
    LIST_CACHE_SYNC_MIN = max(0, int(getattr(settings, "NEWS_LIST_CACHE_SYNC_MINUTES", 180)))
    _SEED_MAX_PER_RUN = max(7, min(14, int(getattr(settings, "RSS_PROCESS_MAX_PER_BATCH", 22))))
except Exception:
    INTERVAL_MIN = 240
    LIST_CACHE_SYNC_MIN = 0
    _SEED_MAX_PER_RUN = 12
    is_rss_and_ai_disabled = lambda: False

JST = ZoneInfo("Asia/Tokyo")


def _scheduled_rss_fetch_and_article():
    """指定時刻: RSS取得→新しい記事だけ良い記事を記事化（13:00/19:00）"""
    NewsAggregator._db_backoff_until = None  # スケジュール実行は必ず試みる
    NewsAggregator.get_news(force_refresh=True)
    _scheduled_refresh_vote_cache()


def _scheduled_1900_rss_and_ai_daily():
    """19:00 JST: (1) RSS取得→記事化 (2) その後にAI日次コンテンツを1日1回だけ更新"""
    NewsAggregator._db_backoff_until = None  # スケジュール実行は必ず試みる
    NewsAggregator.get_news(force_refresh=True)
    try:
        from app.services.ai_daily import generate_daily_ai_content
        generate_daily_ai_content()
    except Exception as e:
        logger.warning("AI日次コンテンツ生成に失敗: %s", e)
    _scheduled_refresh_vote_cache()


def _run_claude_research_and_seed(slot: str, n_news: int, n_papers: int):
    """Claude Code CLI でウェブリサーチ → curated_articles.json 更新 → 記事化パイプライン。
    slot: "morning" / "afternoon" / "night"
    claude CLI がない環境（Render 本番など）では自動スキップする。"""
    try:
        # アイドル後の closed connection 対策:
        # 先にDBへ1回触れてプール/接続を起こしてから Claude 処理へ進む。
        warm_ok = False
        for i in range(2):
            try:
                NewsAggregator.sync_list_cache_from_db(force=True)
                warm_ok = True
                break
            except Exception as e:
                logger.warning("Claude 事前DBウォームアップ失敗(%d/2): %s", i + 1, e)
                try:
                    import time as _time
                    _time.sleep(1.0)
                except Exception:
                    pass
        if not warm_ok:
            logger.warning("Claude タスク中止: DBウォームアップに失敗したためスキップ")
            return

        from app.services.claude_researcher import is_claude_available, run_claude_research
        if not is_claude_available():
            logger.debug("claude CLI が未インストールのためリサーチをスキップ")
            return

        n_total = n_news + n_papers
        ok = run_claude_research(n=n_total, n_news=n_news, n_papers=n_papers, timeout=1200, slot=slot)
        if not ok:
            return
        # Claude が数分〜十数分ブロックするあいだ DB 接続はアイドルになり、Neon 側で切断されがち。
        # 古い接続がプールに残ったまま get_cached_article_ids へ進むと SSL 切断エラーになるため、ここでプールを捨てる。
        try:
            from app.services.neon_store import reset_neon_connection_pool, use_neon

            if use_neon():
                reset_neon_connection_pool()
        except Exception:
            pass
        from app.services.article_seed_from_curated import process_curated_articles
        count = process_curated_articles(max_per_run=30)
        if count > 0:
            NewsAggregator.sync_list_cache_from_db(force=True)  # DB 書き込み直後ならバックオフ無視
            NewsAggregator._invalidate_papers_cache()
            logger.info("Claude リサーチ[%s]→記事化完了: %d 件追加", slot, count)
        else:
            logger.info("Claude リサーチ[%s]: 新規記事なし（重複または生成失敗）", slot)
        _scheduled_refresh_vote_cache()
        # Notion 相談は朝の更新（8:30）のみ掲載
        if slot == "morning":
            try:
                from app.services.notion_consultation import process_notion_consultation
                process_notion_consultation()
            except Exception as _e:
                logger.warning("Notion 相談処理でエラー: %s", _e)
    except Exception as e:
        logger.warning("Claude リサーチタスク[%s]でエラー: %s", slot, e)


# スロット別ラッパー（APScheduler は引数なしの callable のみ受け付けるため）
def _scheduled_claude_morning():
    """8:30 朝: ニュース5 + 論文4（バズ3件必須）"""
    _run_claude_research_and_seed(slot="morning", n_news=5, n_papers=4)


def _scheduled_claude_afternoon():
    """16:30 夕方: ニュース5 + 論文4（バズ3件必須）"""
    _run_claude_research_and_seed(slot="afternoon", n_news=5, n_papers=4)


def _scheduled_claude_night():
    """22:00 夜: ニュース5 + 論文4（バズ3件必須）"""
    _run_claude_research_and_seed(slot="night", n_news=5, n_papers=4)


def _scheduled_trend_comment():
    """Xの急上昇ポストに偉人コメントを生成し、Notionにドラフト追加する（人が確認してから投稿）。"""
    try:
        from app.services.consultation_service import run_trend_comment_once
        ok = run_trend_comment_once()
        logger.info("Xトレンドコメント生成: %s", "成功" if ok else "スキップ")
    except Exception as e:
        logger.warning("Xトレンドコメント生成でエラー: %s", e)


# 後方互換（scripts/ 等から呼ぶ場合）
def _scheduled_claude_research_and_seed():
    """旧シグネチャ互換。現在時刻からスロットを自動判定して実行。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    hour = datetime.now(ZoneInfo("Asia/Tokyo")).hour
    if 5 <= hour < 12:
        _scheduled_claude_morning()
    elif 12 <= hour < 19:
        _scheduled_claude_afternoon()
    else:
        _scheduled_claude_night()


def _scheduled_sync_list_cache_from_db():
    """一覧メモリキャッシュを DB から再構築（再起動なしで新着を表示させる）"""
    try:
        NewsAggregator.sync_list_cache_from_db()
    except Exception as e:
        logger.warning("一覧キャッシュ同期に失敗: %s", e)


def _scheduled_refresh_vote_cache():
    """投票キャッシュを DB から再読み込みする（記事更新スケジューラと同タイミングで呼ぶ）。"""
    try:
        from app.services.vote_service import refresh_vote_cache
        refresh_vote_cache()
    except Exception as e:
        logger.warning("投票キャッシュ更新に失敗: %s", e)


def _scheduled_generate_policy():
    """月2回（1日・15日 9:00 JST）: 少子化対策の政策提案を Claude CLI で生成して保存。"""
    try:
        from app.services.policy_ai_service import run_generate_and_save
        logger.info("政策提案生成 開始（少子化対策）")
        ok = run_generate_and_save("shoushika")
        if ok:
            logger.info("政策提案生成 完了")
        else:
            logger.warning("政策提案生成 失敗または Claude CLI 未設定")
    except Exception as e:
        logger.warning("政策提案生成でエラー: %s", e)


def _scheduled_collect_metrics():
    """週1回（月曜 0:00 JST）: 各省庁統計データを収集して metrics テーブルに保存。"""
    try:
        from app.services.metrics_service import collect_all_metrics
        logger.info("メトリクス収集 開始")
        total = collect_all_metrics()
        logger.info("メトリクス収集 完了: %d 件", total)
    except Exception as e:
        logger.warning("メトリクス収集でエラー: %s", e)


def _seed_if_needed():
    """キャッシュが少ないときだけRSS取得→記事化。"""
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
        added = process_new_rss_articles(news_list, max_per_run=_SEED_MAX_PER_RUN, existing_articles=all_items)
        if added > 0:
            NewsAggregator.sync_list_cache_from_db(force=True)


def _startup_add_one_each():
    """起動時に日本関連記事1本＋海外記事1本をDBに追加（バックグラウンド実行）。
    1本あたりRSS取得・翻訳・本文取得・解説生成のため目安2〜5分かかることがあります。"""
    try:
        from app.services.article_processor import process_startup_articles
        from app.services.news_aggregator import NewsAggregator
        added = process_startup_articles(rss_items=None, trend_keywords=None)
        if added > 0:
            NewsAggregator.get_news(force_refresh=True)
            try:
                from app.services.render_notifier import notify_render_cache_refresh

                notify_render_cache_refresh(reason=f"startup_added:{added}")
            except Exception:
                pass
            logger.info("起動時記事追加: %d 件（日本1＋海外1）", added)
    except Exception as e:
        logger.warning("起動時記事追加でエラー: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時にスケジューラ開始。"""
    import os
    import threading
    rss_ai_disabled = is_rss_and_ai_disabled()
    if rss_ai_disabled:
        logger.info("RSS取得・AI要約は無効です（DISABLE_RSS_AND_AI=true）。表示はキャッシュのみ。")

    # ストレージ: Neon があれば Postgres、なければローカル SQLite
    try:
        from app.services.neon_store import use_neon, neon_init_schema

        db_url = os.environ.get("DATABASE_URL", "").strip()
        if use_neon():
            try:
                neon_init_schema()
            except Exception as e:
                logger.warning("Neon スキーマ初期化でエラー: %s", e)
            logger.info("ストレージ: Neon Postgres（DATABASE_URL 先頭40文字: %s...）", db_url[:40])
        else:
            if db_url:
                logger.warning("ストレージ: DATABASE_URL はありますが psycopg2 が使えないため SQLite にフォールバックしています")
            else:
                logger.info("ストレージ: DATABASE_URL 未設定 → ローカル SQLite（data/articles.db）を使用します")
    except Exception as e:
        logger.warning("ストレージ確認でエラー: %s", e)

    if os.environ.get("RENDER", "").strip().lower() == "true":
        try:
            from app.services.neon_store import use_neon as _un

            if not _un():
                logger.error(
                    "Render 本番では DATABASE_URL（Neon の接続文字列）と psycopg2-binary の利用を推奨します。"
                )
        except Exception as e:
            logger.warning("Render ストレージ確認でエラー: %s", e)

    startup_seed_enabled = str(getattr(settings, "STARTUP_SEED_ENABLED", "false")).strip().lower() in ("1", "true", "yes")

    if not rss_ai_disabled and startup_seed_enabled:
        # シードは _init 完了後に実行
        def _run_seed_delayed():
            import time
            time.sleep(90)
            _seed_if_needed()
        threading.Thread(target=_run_seed_delayed, daemon=True).start()
    elif not rss_ai_disabled:
        logger.info("起動時シードは無効化されています（STARTUP_SEED_ENABLED=false）。")

    def _init():
        # 起動直後は軽い処理だけ先に実行して、API応答を阻害しない
        NewsAggregator.get_trends(force_refresh=True)
        try:
            NewsAggregator.sync_list_cache_from_db(force=True)
            logger.info("起動時: 一覧キャッシュを DB から同期しました（%d 件）", len(NewsAggregator._news_cache or []))
        except Exception as e:
            logger.warning("起動時一覧キャッシュ同期に失敗: %s", e)
        # 投票キャッシュを起動時に読み込む
        try:
            from app.services.vote_service import refresh_vote_cache
            refresh_vote_cache()
            logger.info("起動時: 投票キャッシュを初期化しました")
        except Exception as e:
            logger.warning("起動時投票キャッシュ初期化に失敗: %s", e)
        try:
            from app.services.sitemap_service import sitemap_snapshot_path

            if not sitemap_snapshot_path().exists():
                if (getattr(settings, "SITE_URL", "") or "").strip():
                    NewsAggregator.sync_list_cache_from_db(force=True)
                else:
                    logger.info("SITE_URL 未設定のため起動時 sitemap 生成をスキップします")
        except Exception as e:
            logger.warning("起動時 sitemap 生成に失敗: %s", e)

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
        logger.info("一覧キャッシュを DB から %d 分ごとに同期します（新着記事の反映）。", LIST_CACHE_SYNC_MIN)
    if not rss_ai_disabled:
        # 13:00: RSS取得→記事化のみ
        scheduler.add_job(
            _scheduled_rss_fetch_and_article,
            CronTrigger(hour=13, minute=0, timezone=JST),
            id="rss_1300",
        )
        # 19:00: 記事更新のあと、AI日次コンテンツを1日1回だけ更新
        scheduler.add_job(
            _scheduled_1900_rss_and_ai_daily,
            CronTrigger(hour=19, minute=0, timezone=JST),
            id="rss_1900_and_ai_daily",
        )
        logger.info("RSS記事化: 13:00 / 19:00 JST に設定")
        # Claude ウェブリサーチ: 8:30(朝) / 16:30(夕) / 22:00(夜) の3スロット
        # スロットごとにカテゴリ比率・記事数・SEO指示が異なる
        # claude CLI がない環境（Render 本番）では各関数内で自動スキップ
        for cr_id, cr_hour, cr_minute, cr_func in [
            ("claude_research_0830", 8, 30, _scheduled_claude_morning),
            ("claude_research_1630", 16, 30, _scheduled_claude_afternoon),
            ("claude_research_2200", 22, 0, _scheduled_claude_night),
        ]:
            scheduler.add_job(
                cr_func,
                CronTrigger(hour=cr_hour, minute=cr_minute, timezone=JST),
                id=cr_id,
            )
        logger.info("Claude ウェブリサーチ: 8:30/16:30/22:00 JST（各 ニュース5+論文4・バズ3）")
        # Xトレンドへの偉人コメント生成（Notionドラフトのみ・自動投稿はしない）: 研究と同じ3スロット
        for tc_id, tc_hour, tc_minute in [
            ("trend_comment_0830", 8, 30),
            ("trend_comment_1630", 16, 30),
            ("trend_comment_2200", 22, 0),
        ]:
            scheduler.add_job(
                _scheduled_trend_comment,
                CronTrigger(hour=tc_hour, minute=tc_minute, timezone=JST),
                id=tc_id,
            )
        logger.info("Xトレンドコメント生成: 8:30/16:30/22:00 JST（Notionドラフトのみ）")
        # 政策提案生成: 一時停止中
        # scheduler.add_job(
        #     _scheduled_generate_policy,
        #     CronTrigger(day="1,15", hour=9, minute=0, timezone=JST),
        #     id="generate_policy",
        # )
        # 週1回（月曜 0:00）: 統計メトリクス収集
        scheduler.add_job(
            _scheduled_collect_metrics,
            CronTrigger(day_of_week="mon", hour=0, minute=0, timezone=JST),
            id="collect_metrics",
        )
        logger.info("統計メトリクス収集: 毎週月曜 0:00 JST に設定")
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

    # セッション Cookie 署名鍵。未設定なら ADMIN_SECRET（前後空白除去）を使用。鍵を固定したい場合は SESSION_SECRET を Render に設定。
    _session_key = (getattr(settings, "SESSION_SECRET", "") or "").strip()
    if not _session_key:
        _session_key = (getattr(settings, "ADMIN_SECRET", "") or "").strip() or "dev-secret-change-me"
    app.add_middleware(SessionMiddleware, secret_key=_session_key, session_cookie="newsite_admin")
except Exception:
    pass

from app.middleware.markdown_for_agents import MarkdownForAgentsMiddleware

app.add_middleware(MarkdownForAgentsMiddleware)

static_path = Path(__file__).resolve().parent / "app" / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

app.include_router(news.router)
app.include_router(metrics_router.router)
app.include_router(consultation_router.router)


@app.post("/api/admin/sync-cache")
def admin_sync_cache():
    """一覧キャッシュを即時 DB から再同期する。記事追加後に呼ぶ。"""
    from app.services.news_aggregator import NewsAggregator
    NewsAggregator.sync_list_cache_from_db(force=True)  # 管理者明示実行: バックオフ無視
    return {"status": "ok", "cached": len(NewsAggregator._news_cache or [])}


@app.get("/api/debug/neon-status")
async def debug_neon_status():
    """Neon Postgres の接続状態を診断する。"""
    import traceback
    from app.config import settings

    # os.environ ではなく settings と同じ値（.env 読み込み後）を表示する
    db_url = (getattr(settings, "DATABASE_URL", "") or "").strip()
    result = {
        "database_url_set": bool(db_url),
        "database_url_preview": (db_url[:40] + "...") if db_url else "",
        "psycopg2_importable": False,
        "use_neon": False,
        "connection_ok": False,
        "articles_count": None,
        "error": None,
        "hint": None,
    }
    try:
        import psycopg2  # noqa: F401
        result["psycopg2_importable"] = True
    except Exception as e:
        result["error"] = f"psycopg2 import 失敗: {type(e).__name__}: {e}"
        return result
    try:
        from app.services.neon_store import use_neon
        result["use_neon"] = use_neon()
    except Exception as e:
        result["error"] = f"use_neon() 呼び出し失敗: {e}"
        return result
    if not result["use_neon"]:
        result["error"] = "use_neon() が False（DATABASE_URL が空です）"
        result["hint"] = (
            "本番(Render)ではダッシュボードの Environment に Neon の接続文字列を "
            "DATABASE_URL という名前で設定してください（.env はサーバーに自動では乗りません）。"
            " ローカルではリポジトリ直下の .env に DATABASE_URL=… を書き、プロジェクトルートから起動してください。"
        )
        return result
    try:
        import psycopg2 as pg2
        conn = pg2.connect(dsn=db_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM articles")
            result["articles_count"] = cur.fetchone()[0]
        conn.close()
        result["connection_ok"] = True
    except Exception as e:
        result["error"] = f"接続テスト失敗: {type(e).__name__}: {e}\n{traceback.format_exc()}"
    return result


@app.get("/api/debug/storage")
async def debug_storage():
    """Neon Postgres または SQLite のどちらを使っているか確認する。"""
    from app.config import settings

    db_url = (getattr(settings, "DATABASE_URL", "") or "").strip()
    try:
        from app.services.neon_store import use_neon

        if use_neon():
            return {
                "storage": "neon",
                "database_url_preview": (db_url[:40] + "...") if db_url else "",
                "message": "Neon Postgres を使用しています。/api/debug/neon-status で詳細を確認できます。",
            }
    except Exception as e:
        return {"storage": "unknown", "message": str(e)}
    return {
        "storage": "sqlite",
        "database_url_set": bool(db_url),
        "message": "DATABASE_URL が未設定か Neon が無効のため SQLite（data/*.db）を使用しています。",
    }


@app.get("/api/debug/articles-status")
async def debug_articles_status():
    """記事が表示されない原因の確認用。保存記事数・AI解説済み数・表示対象数を返す。"""
    import traceback
    from app.services.article_cache import load_all
    from app.services.explanation_cache import get_cached_article_ids
    try:
        from app.services.neon_store import use_neon

        storage = "neon" if use_neon() else "sqlite"
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
        "message": "ストレージに保存された記事件数／解説付きフラグ情報です（一覧は load_all と同期）。",
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
<li><a href="/api/debug/neon-status" target="_blank">Neon Postgres 接続確認</a></li>
<li><a href="/api/debug/articles-status" target="_blank">記事ステータス（なぜ出ないか確認）</a></li>
<li><a href="/api/debug/storage" target="_blank">ストレージ確認（Neon / SQLite）</a></li>
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
