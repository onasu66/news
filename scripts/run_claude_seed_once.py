"""Claude リサーチ + curated 記事化を1回実行。"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")


def main() -> int:
    from app.services.news_aggregator import NewsAggregator

    NewsAggregator.sync_list_cache_from_db(force=True)

    from app.services.claude_researcher import is_claude_available, run_claude_research

    research_ok = False
    if is_claude_available():
        print("=== Claude リサーチ開始 (timeout=900s) ===", flush=True)
        research_ok = run_claude_research(n=20, n_news=10, n_papers=10, timeout=900)
        status = "OK" if research_ok else "失敗/タイムアウト"
        print(f"=== Claude リサーチ: {status} ===", flush=True)
    else:
        print("=== Claude CLI なし: リサーチスキップ ===", flush=True)

    try:
        from app.services.neon_store import reset_neon_connection_pool, use_neon

        if use_neon():
            reset_neon_connection_pool()
    except Exception:
        pass

    from app.services.article_seed_from_curated import process_curated_articles

    print("=== curated 記事化開始 (max 2件) ===", flush=True)
    count = process_curated_articles(max_per_run=2)
    if count > 0:
        NewsAggregator.sync_list_cache_from_db(force=True)
        NewsAggregator._invalidate_papers_cache()
        try:
            from app.services.indexnow_service import flush_indexnow_queue

            flush_indexnow_queue()
        except Exception:
            pass
        try:
            from app.services.render_notifier import notify_render_cache_refresh

            notify_render_cache_refresh(reason=f"claude_curated_added:{count}")
        except Exception:
            pass

    print(f"=== 完了: 記事 {count} 件追加 (リサーチ={research_ok}) ===", flush=True)
    return 0 if count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
