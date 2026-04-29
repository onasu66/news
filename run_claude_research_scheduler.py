"""
Claude ウェブリサーチ スタンドアロンスケジューラ
8:00 / 13:00 / 19:00 JST に Claude CLI でウェブリサーチ → Firestore に記事追加

使い方:
  python run_claude_research_scheduler.py

終了: Ctrl+C
"""
import logging
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

from zoneinfo import ZoneInfo
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

JST = ZoneInfo("Asia/Tokyo")


def run_research():
    """Claude CLI でウェブリサーチ → curated_articles.json → Firestore 保存"""
    logger.info("=== Claude リサーチ開始 ===")
    try:
        from app.services.claude_researcher import is_claude_available, run_claude_research
        if not is_claude_available():
            logger.warning("Claude CLI が見つかりません。`npm i -g @anthropic-ai/claude-code` でインストールしてください。")
            return
        ok = run_claude_research(n=15, n_news=8, n_papers=7, timeout=900)
        if not ok:
            logger.warning("Claude リサーチ失敗（curated_articles.json が更新されませんでした）")
            return
    except Exception as e:
        logger.error("Claude リサーチでエラー: %s", e)
        return

    logger.info("記事化処理を開始...")
    try:
        from app.services.article_seed_from_curated import process_curated_articles
        count = process_curated_articles(max_per_run=30)
        logger.info("=== Claude リサーチ完了: %d 件追加 ===", count)
    except Exception as e:
        logger.error("記事化でエラー: %s", e)


if __name__ == "__main__":
    # 起動時に Claude CLI の確認
    try:
        from app.services.claude_researcher import is_claude_available
        if is_claude_available():
            logger.info("Claude CLI: 利用可能 ✓")
        else:
            logger.warning("Claude CLI が見つかりません。`npm i -g @anthropic-ai/claude-code` をインストールしてください。")
    except Exception as e:
        logger.error("確認エラー: %s", e)

    scheduler = BlockingScheduler(timezone=JST)

    # 8:00 / 13:00 / 19:00 JST に実行
    for job_id, hour in [("cr_0800", 8), ("cr_1300", 13), ("cr_1900", 19)]:
        scheduler.add_job(
            run_research,
            CronTrigger(hour=hour, minute=0, timezone=JST),
            id=job_id,
        )

    logger.info("スケジューラ起動: 8:00 / 13:00 / 19:00 JST に Claude リサーチを実行します")
    logger.info("終了するには Ctrl+C を押してください")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("スケジューラを停止しました")
