"""ニュース記事を今すぐ追加（process_rss_to_site_article直接呼び出し、トレンドスコアリング無し）"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

from app.services.rss_service import fetch_rss_news
from app.services.article_processor import process_rss_to_site_article
from app.services.explanation_cache import get_cached_article_ids

MAX_ADD = 3  # 追加する最大件数

def main():
    logger.info("RSS取得中...")
    rss_items = fetch_rss_news()
    logger.info("RSS: %d件取得", len(rss_items))

    cached_ids = get_cached_article_ids()
    uncached = [x for x in rss_items if x.id not in cached_ids]
    logger.info("未処理: %d件", len(uncached))

    if not uncached:
        logger.info("追加できる新しい記事がありません")
        return

    added = 0
    for item in uncached:
        if added >= MAX_ADD:
            break
        logger.info("処理中: [%s] %s", item.category, item.title[:60])
        try:
            ok = process_rss_to_site_article(item)
            if ok:
                added += 1
                logger.info("追加完了 (%d/%d): %s", added, MAX_ADD, item.title[:60])
            else:
                logger.info("スキップ（重複or翻訳失敗）: %s", item.title[:60])
        except Exception as e:
            logger.warning("失敗: %s — %s", item.title[:60], e)

    logger.info("=== 完了: %d件追加 ===", added)

if __name__ == "__main__":
    main()
